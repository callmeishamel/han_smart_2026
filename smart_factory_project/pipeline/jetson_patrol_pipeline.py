"""
Jetson 순찰 로봇 -> PostgreSQL 데이터 적재 파이프라인 (v2)

역할
----
Jetson 위에서 도는 NanoOWL 탐지 컨테이너(ai_inference_sender.py)가
UDP(127.0.0.1:9998, 대시보드/DB용 포트)로 보내는 JSON을 받아서,
위험도를 판정하고 patrol_logs 테이블에 적재합니다.

ai_inference_sender.py는 같은 탐지 결과를 두 포트로 동시에 보냅니다:
9999(ROS2 브릿지용, vision_inference_node.py) / 9998(대시보드·DB용, 이 스크립트).
이 스크립트는 반드시 9998을 리스닝해야 합니다.

v1 대비 변경 사항
------------------
- v1은 실제 비전 모듈이 도착하기 전이라 run_yolo_inference()가 무작위
  더미 데이터를 생성했습니다. 이제 손준영 팀장의 실제 코드를 받았으므로,
  더미 대신 진짜 UDP 리스너로 교체했습니다.
- 위험도 판정 로직과 DetectionEvent 정의가 team_integration_adapter.py와
  중복돼 있던 것을 common/schema.py 하나로 통일했습니다. (예: 사람과의
  위험 거리 임계값을 바꾸고 싶으면 common/schema.py 한 곳만 고치면 두
  스크립트 모두에 반영됩니다.)
- DB 접속 정보를 코드에 하드코딩하지 않고 환경변수로 분리했습니다
  (common.schema.get_db_config 참고).

이 스크립트는 team_integration_adapter.py의 NanoOwlUdpVisionAdapter와
달리 "큐 + 별도 스레드" 구조가 아니라 단순 블로킹 루프입니다. 이 파일은
독립 프로세스로 실행되어 그 자체가 리스너이기 때문에 스레드로 분리할
필요가 없습니다 (더 가볍고 디버깅하기 쉽습니다). 여러 컴포넌트 안에
리스너를 "끼워 넣어야" 하는 경우에는 team_integration_adapter.py의
NanoOwlUdpVisionAdapter를 사용하세요.

사용법
------
    export SFP_DB_PASSWORD='실제비밀번호'
    python3 jetson_patrol_pipeline.py --zone A --udp-port 9998
"""

import argparse
import json
import logging
import os
import socket
import sys
import time
from typing import Optional

# common 패키지를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (  # noqa: E402
    INSERT_PATROL_LOG_SQL,
    DetectionEvent,
    build_detection_event,
    get_db_config,
    init_all_tables,
    patrol_log_params,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("jetson_patrol_pipeline")

MAX_RETRY_BACKOFF_SECONDS = 30

# UDP 수신 바인딩 주소.
#
# 예전 기본값은 "127.0.0.1" 이었는데, 그러면 같은 PC에서 보낸 패킷만 받습니다.
# 젯슨(ai_inference_sender.py)은 다른 기기이므로 이 파이프라인에는 아무것도
# 도착하지 않고, UDP라 에러도 나지 않아서 "수신 대기 시작" 로그만 뜬 채
# 조용히 멈춰 있는 것처럼 보였습니다. 실행 스크립트(run_pipeline_rag.sh)와
# 문서 어디에서도 --udp-ip 를 바꾸지 않으므로, 실제 데모에서는 patrol_logs 에
# 한 건도 쌓이지 않는 상태였습니다.
#
# 모든 인터페이스에서 받되, 특정 랜카드로 좁히고 싶으면 그 IP를 주면 됩니다.
DEFAULT_UDP_BIND = os.environ.get("PIPELINE_UDP_BIND", "0.0.0.0")


class PatrolLogWriter:
    """DB 재연결/재시도를 담당하는 얇은 래퍼.

    Jetson은 네트워크가 불안정한 현장에 놓일 수 있으므로, DB 연결이
    끊기면 즉시 죽지 않고 지수 백오프(exponential backoff)로 재연결을
    시도합니다.
    """

    def __init__(self, db_config: dict):
        import psycopg2  # noqa: F401  (DB를 실제로 쓸 때만 필요하므로 지역 import)
        self._psycopg2 = psycopg2
        self._db_config = db_config
        self._conn = None
        self._backoff_seconds = 1

    def _connect(self):
        self._conn = self._psycopg2.connect(**self._db_config)
        self._conn.autocommit = False
        cursor = self._conn.cursor()
        try:
            init_all_tables(cursor)  # 테이블이 없으면 생성 (patrol_zones/patrol_logs/response_plans)
            self._conn.commit()
        finally:
            cursor.close()
        logger.info("DB 연결 및 테이블 확인 완료")
        self._backoff_seconds = 1  # 연결 성공 시 백오프 초기화

    def _ensure_connected(self):
        if self._conn is not None and not self._conn.closed:
            return
        while True:
            try:
                self._connect()
                return
            except Exception as exc:
                logger.warning(
                    "DB 연결 실패 (%s). %d초 후 재시도합니다.",
                    exc, self._backoff_seconds,
                )
                time.sleep(self._backoff_seconds)
                self._backoff_seconds = min(
                    self._backoff_seconds * 2, MAX_RETRY_BACKOFF_SECONDS
                )

    def write(self, event: DetectionEvent) -> bool:
        """이벤트 1건을 적재. 성공하면 True, 실패(후 재연결 대기)하면 False."""
        self._ensure_connected()
        try:
            with self._conn.cursor() as cursor:
                # SQL 과 파라미터 조립은 common/schema.py 에 있습니다 — 컬럼이
                # 늘었을 때 여기와 team_integration_adapter.py 를 따로 고쳐야
                # 했고, 실제로 map_x/map_y 를 둘 다 빠뜨린 적이 있습니다.
                cursor.execute(INSERT_PATROL_LOG_SQL, patrol_log_params(event))
            self._conn.commit()
            return True
        except Exception as exc:
            logger.error("데이터 적재 실패: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            return False

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            logger.info("DB 연결 종료")


class ZoneResolver:
    """--zone auto 일 때, 로봇이 지금 있는 구역을 미니맵 서버에서 가져옵니다.

    구역은 원래 이 스크립트를 띄울 때 --zone A 로 못 박았습니다. 젯슨이 한 대이고
    구역이 고정이면 그걸로 충분했지만, 로봇이 A/B/C 를 돌아다니면 적재되는 구역이
    실제와 어긋납니다. 미니맵이 SLAM 지도에서 구역을 자동으로 나누고 로봇 위치로
    현재 구역을 알고 있으므로, 그 값을 가져다 씁니다.

    적재 루프 한가운데서 부르는 코드라 세 가지를 지킵니다.
      - 절대 예외를 올리지 않습니다. 구역 하나 못 가져왔다고 적재가 멈추면 안 됩니다.
      - 짧은 타임아웃 + 캐시. 탐지마다 HTTP 를 때리면 초당 수십 번이 됩니다.
      - requests 가 없으면 조용히 고정 구역으로 돌아갑니다. 젯슨에서 단독 실행할 때는
        psycopg2-binary 하나만 설치하므로 requests 가 없을 수 있습니다.
    """

    def __init__(self, fallback: str, url: str, token: str = "",
                 ttl_sec: float = 3.0, timeout_sec: float = 1.0):
        self.fallback = fallback
        self.url = url
        self.headers = {"X-Minimap-Token": token} if token else {}
        self.ttl_sec = ttl_sec
        self.timeout_sec = timeout_sec
        self._zone = None
        self._at = 0.0
        self._warned = False

    def __call__(self):
        now = time.monotonic()
        if self._zone is not None and now - self._at < self.ttl_sec:
            return self._zone

        zone = self._fetch()
        if zone:
            if self._zone != zone:
                logger.info("현재 구역: %s", zone)
            self._zone = zone
            self._at = now
            self._warned = False
            return zone

        # 못 가져왔으면 마지막으로 알던 구역을 유지하고, 그것도 없으면 고정값.
        # 여기서 경고를 매번 찍으면 로그가 탐지 기록을 덮습니다.
        if not self._warned:
            logger.warning(
                "현재 구역을 가져오지 못해 '%s' 로 적재합니다 (%s)",
                self._zone or self.fallback, self.url)
            self._warned = True
        self._at = now
        return self._zone or self.fallback

    def _fetch(self):
        try:
            import requests
        except ImportError:
            if not self._warned:
                logger.warning("requests 패키지가 없어 --zone auto 를 쓸 수 없습니다. "
                               "pip install requests 후 다시 실행하세요.")
            return None
        try:
            resp = requests.get(self.url, headers=self.headers, timeout=self.timeout_sec)
            if resp.status_code != 200:
                return None
            return resp.json().get("zone") or None
        except Exception:                                      # noqa: BLE001
            return None


def resolve_zone_argument(zone_arg: str, fallback: str):
    """--zone 값을 고정 문자열 또는 ZoneResolver 로 바꿉니다.

    미니맵 주소는 event_logger.py 와 같은 환경변수를 씁니다 — 같은 서버를 부르는
    또 하나의 클라이언트이므로, 주소와 토큰이 갈라지면 한쪽만 조용히 멈춥니다.
    """
    if str(zone_arg).strip().lower() != "auto":
        return zone_arg

    daeun_ip = os.environ.get("DAEUN_LAPTOP_IP", "203.0.113.20")
    minimap_port = os.environ.get("MINIMAP_PORT", "8091")
    token = os.environ.get("MINIMAP_TOKEN", "").strip()
    url = f"http://{daeun_ip}:{minimap_port}/current_zone"
    logger.info("구역 자동 판정: %s (실패 시 '%s')", url, fallback)
    return ZoneResolver(fallback=fallback, url=url, token=token)


def _zone_name(zone):
    """zone 은 고정 문자열이거나 ZoneResolver 같은 호출 가능한 객체입니다."""
    return zone() if callable(zone) else zone


def iter_udp_detections(zone, udp_ip: str, udp_port: int):
    """UDP 소켓에서 NanoOWL payload를 받아 DetectionEvent를 하나씩 yield하는 제너레이터.

    ai_inference_sender.py가 한 프레임에서 여러 객체를 한꺼번에 보내므로
    (payload["detections"] 리스트), 패킷 하나당 이벤트 여러 개가 나올 수 있습니다.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((udp_ip, udp_port))
    logger.info("UDP 수신 대기 시작 (%s:%d, 구역: %s)", udp_ip, udp_port,
                "auto (미니맵)" if callable(zone) else zone)

    # 127.0.0.1 에 바인딩하면 같은 PC에서 보낸 패킷만 받습니다. 젯슨은 다른
    # 기기이므로 그 경우 아무것도 도착하지 않는데, UDP라 에러도 안 납니다
    # ("수신 대기 시작" 로그만 뜬 채 조용히 멈춰 있는 것처럼 보입니다).
    if udp_ip in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "루프백(%s)에만 바인딩되어 있어 다른 기기(젯슨)에서 오는 패킷은 받지 못합니다. "
            "실제 연동 시에는 --udp-ip 0.0.0.0 또는 PIPELINE_UDP_BIND=0.0.0.0 을 쓰세요.",
            udp_ip,
        )

    while True:
        data, _addr = sock.recvfrom(65536)
        try:
            payload = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            logger.warning("손상된 UDP 패킷 무시")
            continue

        detections = payload.get("detections", [])
        if not detections:
            continue
        # 구역은 패킷마다 한 번만 확인합니다. 한 패킷 안의 탐지들은 같은 순간,
        # 같은 자리에서 나온 것이라 구역이 다를 수 없습니다.
        current = _zone_name(zone)
        for det in detections:
            yield build_detection_event(current, det)


def main():
    parser = argparse.ArgumentParser(description="Jetson 순찰 로봇 -> DB 적재 파이프라인")
    parser.add_argument("--zone", default="A",
                        help=("이 Jetson이 담당하는 구역 (예: A, B, C). "
                              "'auto' 를 주면 미니맵 서버에서 로봇이 지금 있는 구역을 "
                              "가져옵니다 (requests 패키지 필요)."))
    parser.add_argument("--zone-fallback", default="A",
                        help="--zone auto 인데 미니맵을 못 부를 때 쓸 구역 (기본 %(default)s)")
    parser.add_argument("--udp-ip", default=DEFAULT_UDP_BIND,
                        help=("UDP 수신 바인딩 주소 (기본 %(default)s). 젯슨은 다른 기기이므로 "
                              "127.0.0.1 로 두면 패킷이 하나도 도착하지 않습니다. "
                              "특정 랜카드로 제한하려면 그 인터페이스의 IP를 직접 주세요."))
    parser.add_argument("--udp-port", type=int, default=9998,
                         help="ai_inference_sender.py가 보내는 UDP 포트 (기본 9998, 대시보드/DB용)")
    args = parser.parse_args()

    zone = resolve_zone_argument(args.zone, args.zone_fallback)

    writer = PatrolLogWriter(get_db_config())
    logger.info("Jetson 순찰 파이프라인 시작 (구역: %s)", args.zone)

    try:
        for event in iter_udp_detections(zone, args.udp_ip, args.udp_port):
            success = writer.write(event)
            if success:
                logger.info(
                    "적재 완료 [%s] %s (위험도: %s, 거리: %sm)",
                    event.zone, event.detected_object, event.risk_level, event.distance,
                )
    except KeyboardInterrupt:
        logger.info("종료 신호 감지, 파이프라인을 정리합니다.")
    finally:
        writer.close()


if __name__ == "__main__":
    main()
