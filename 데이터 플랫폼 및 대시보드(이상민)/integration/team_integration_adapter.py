"""
팀 통합 어댑터 (team_integration_adapter.py)

목적
----
각 팀원 파트가 서로 어떤 데이터를 주고받을지에 대한 "인터페이스(계약)"를
정의하고, 이 인터페이스로 직접 연동된 파트(비전 모듈, LLM 에이전트)는 진짜
구현체로, 아직 이 인터페이스로는 연동되지 않은 파트(디지털 트윈)는 Mock으로
연결해 전체 파이프라인이 지금 당장 끝까지 동작하도록 만든 파일입니다.

역할 분담 (PPT 기준)
--------------------
- 손준영 (팀장): 산업현장 작업자 안전상태 인식 비전 AI 탐지 모듈 (NanoOWL) — 코드 도착함
- 이다은        : 위험구역 디지털 트윈 모델링 및 순찰 커버리지 시각화 (ROS2/Gazebo) —
                  코드 도착, 연동 완료. 단 이 파일을 거치지 않고 별도 경로
                  (젯슨 각도 UDP -> minimap_renderer.py 레이캐스팅 ->
                  edge_video/event_logger.py -> detection_events)로 붙습니다.
                  그래서 여기 있던 DigitalTwinAdapter/ZoneLayout 은 끝내 쓰이지
                  않아 삭제했습니다 (아래 2번 항목에 경위)
- 이상민 (본인) : 이벤트 로그 기반 데이터 플랫폼 설계 및 관리자 대시보드
- 정진철        : 상황 맥락 기반 대응안 생성 LLM 에이전트 (RAG) — 코드 도착,
                  integration/response_agent.py(RagResponseAgent)로 연동 완료

데이터 흐름
-----------
    [NanoOWL 비전 모듈] --UDP:9998--> [NanoOwlUdpVisionAdapter] --DetectionEvent-->
        [PostgresDataPlatform] --저장--> [DB: patrol_logs] --조회--> [대시보드]
                    |
                    +--위험 등급일 때만--> [LLM 에이전트: 대응안 생성] --저장--> [DB: response_plans]

    [ROS2/Gazebo 디지털 트윈] <--위치/좌표 매핑-- [DB: patrol_zones]

공통 스키마
-----------
DetectionEvent, 위험도 판정 로직, DB 테이블 정의는 모두 common/schema.py
하나로 통일되어 있습니다. jetson_patrol_pipeline.py도 동일한 모듈을
사용하므로, 위험도 판정 기준을 바꾸고 싶으면 common/schema.py 한 곳만
고치면 됩니다.
"""

from __future__ import annotations

import os
import sys
import time
import json as _json
import queue as _queue
import socket as _socket
import threading as _threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol

# common 패키지를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가.
# (스크립트를 어느 위치에서 실행하든 동작하도록 __file__ 기준 상대 경로 사용)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (  # noqa: E402
    INSERT_PATROL_LOG_SQL,
    INSERT_RESPONSE_PLAN_SQL,
    DetectionEvent,
    ResponsePlanThrottle,
    build_detection_event,
    get_db_config,
    init_all_tables,
    patrol_log_params,
    response_plan_params,
)


# ==========================================
# 공통 데이터 계약 (Data Contracts) — DetectionEvent는 common.schema 것을 그대로 사용
# ==========================================
@dataclass
class ResponsePlan:
    """LLM 에이전트(정진철 파트)가 위험 이벤트에 대해 생성한 대응안."""

    zone: str
    source_event: DetectionEvent
    recommended_action: str
    reference_docs: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    # builtin / keyword / vector 뒤에 +llm 이 붙으면 실제 LLM 응답을 채택한 것.
    # 기본값은 예전 호출부와 DB 행을 깨지 않기 위한 unknown 입니다.
    generation_mode: str = "unknown"


# ==========================================
# 1. 비전 모듈 어댑터 (손준영 파트) — 코드 도착, 실제 구현체 있음
# ==========================================
class VisionModuleAdapter(Protocol):
    """비전 모듈(현재는 NanoOWL + Stereo Depth)의 탐지 결과를
    DetectionEvent로 변환하는 인터페이스.
    """

    def get_next_event(self) -> Optional[DetectionEvent]:
        ...


class NanoOwlUdpVisionAdapter:
    """ai_inference_sender.py(NanoOWL + StereoSGBM, Docker 컨테이너)가
    UDP로 보내는 JSON을 수신해 DetectionEvent 큐로 변환하는 어댑터.

    ai_inference_sender.py 쪽은 수정하지 않고, 손준영 팀장이 이미
    구현한 :9998(대시보드/DB용 포트) UDP 브릿지를 그대로 받습니다.

    바인딩 주소 기본값은 0.0.0.0 입니다. 예전 기본값(127.0.0.1)은 같은 PC에서
    보낸 패킷만 받는데, 젯슨은 다른 기기라 아무것도 도착하지 않았습니다
    (UDP라 에러도 안 나서 조용히 멈춰 있는 것처럼 보입니다).

    payload 형태: {"detections": [{"object": "human", "bbox": [...], "distance_meter": 1.2}, ...]}
    zone/risk_level/issue는 원본에 없으므로 common.schema.build_detection_event가 채웁니다.

    한 프레임에 탐지가 여러 개 나올 수 있어, get_next_event()가 한 번에
    하나씩 반환하는 인터페이스에 맞추기 위해 내부 큐를 사용합니다.
    """

    def __init__(self, zone: str, udp_ip: str = None, udp_port: int = 9998,
                 max_queue_size: int = 1000):
        udp_ip = udp_ip if udp_ip is not None else os.environ.get("PIPELINE_UDP_BIND", "0.0.0.0")
        self.zone = zone
        self._udp_ip = udp_ip
        self._udp_port = udp_port
        self._queue: "_queue.Queue[DetectionEvent]" = _queue.Queue(maxsize=max_queue_size)
        self._stop_event = _threading.Event()
        self._thread: Optional[_threading.Thread] = None

    def start(self):
        """백그라운드에서 UDP 수신을 시작합니다. 프로세스 시작 시 1회 호출."""
        if self._thread is not None:
            return
        self._thread = _threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _listen_loop(self):
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.bind((self._udp_ip, self._udp_port))
        sock.settimeout(1.0)  # stop_event를 주기적으로 확인하기 위한 타임아웃

        while not self._stop_event.is_set():
            try:
                data, _addr = sock.recvfrom(65536)
            except _socket.timeout:
                continue
            except OSError:
                break

            try:
                payload = _json.loads(data.decode())
            except (ValueError, UnicodeDecodeError):
                continue  # 손상된 패킷은 무시

            for det in payload.get("detections", []):
                event = build_detection_event(self.zone, det)
                try:
                    self._queue.put_nowait(event)
                except _queue.Full:
                    # 큐가 밀리면 오래된 항목부터 버리고 최신 항목을 넣음
                    # (실시간 안전 알림은 최신 상태가 중요하므로 backlog보다 fresh를 우선)
                    try:
                        self._queue.get_nowait()
                    except _queue.Empty:
                        pass
                    self._queue.put_nowait(event)

        sock.close()

    def get_next_event(self) -> Optional[DetectionEvent]:
        """큐에서 이벤트 하나를 꺼냅니다. 없으면 None (non-blocking)."""
        try:
            return self._queue.get_nowait()
        except _queue.Empty:
            return None


# ==========================================
# 2. 디지털 트윈 (이다은 파트) — 이 파일을 거치지 않습니다
#
# 여기에는 원래 DigitalTwinAdapter(Protocol)와 ZoneLayout 이 있었습니다.
# ROS2 토픽에서 구역 좌표/커버리지를 읽어오는 인터페이스로 설계했지만,
# 끝까지 구현체도 호출부도 생기지 않았습니다. 그 사이에 이다은 파트는 전혀
# 다른 경로로 이미 연동을 끝냈습니다.
#
#   젯슨 각도 UDP(:9091)
#     -> dashboard_link/minimap_renderer.py  (SLAM 지도에 레이캐스팅)
#     -> HTTP /detections
#     -> edge_video/event_logger.py
#     -> DB(detection_events)
#
#   지도와 로봇 위치는 minimap_renderer.py 의 /map_data, /state 가 이미
#   제공하고, 대시보드는 그 페이지를 iframe 으로 그대로 띄웁니다.
#
# 즉 ZoneLayout 이 채우려던 자리는 이미 채워져 있고, 어댑터를 구현하면
# /state 가 하는 일을 ROS2 로 한 번 더 하는 셈이 됩니다. 그래서 "언젠가 할 일"로
# 남겨두는 대신 지웠습니다 — 없는 계획을 있는 것처럼 보이게 하는 쪽이 더 비쌉니다.
#
# 나중에 "A 구역이 지도 어디인가"가 필요해지면, 이 어댑터가 아니라
# patrol_zones 에 구역 경계(폴리곤 또는 사각형)를 넣는 편이 맞습니다.
# 그건 팀이 구역을 어떻게 나눌지 정한 뒤에 할 일입니다.
# ==========================================


# ==========================================
# 3. LLM 에이전트 어댑터 (정진철 파트) — 코드 도착, 실제 구현체 있음
# ==========================================
class ResponseAgentAdapter(Protocol):
    """위험 이벤트를 받아 RAG 기반 대응안을 생성하는 인터페이스.

    실제 구현체: integration/response_agent.py의 RagResponseAgent (정진철 작성).

        from integration.response_agent import RagResponseAgent

        orchestrator = PatrolIntegrationOrchestrator(
            ...,
            response_agent=RagResponseAgent(),
        )
    """

    def generate_response(self, event: DetectionEvent) -> ResponsePlan:
        ...


# ==========================================
# 4. 데이터 플랫폼 어댑터 (이상민 = 본인 파트) — 실제 구현체 있음
# ==========================================
class DataPlatformPort(Protocol):
    """DB 적재/조회를 담당."""

    def save_event(self, event: DetectionEvent) -> bool:
        ...

    def save_response_plan(self, plan: ResponsePlan) -> bool:
        ...


class PostgresDataPlatform:
    """common.schema의 DDL(patrol_zones/patrol_logs/response_plans)을 그대로
    사용하는 실제 DB 적재 구현체.

    dashboard/smart_factory_dashboard_v3.py가 조회하는 것과 완전히 같은
    테이블에 씁니다. DB 접속 정보는 common.schema.get_db_config()에서
    환경변수로 읽으므로, 비밀번호를 코드에 하드코딩하지 않습니다.

        export SFP_DB_PASSWORD='실제비밀번호'
    """

    def __init__(self, db_config: Optional[dict] = None):
        import psycopg2  # 실제 DB 연동 시에만 필요하므로 지역 import
        self._psycopg2 = psycopg2
        self._db_config = db_config or get_db_config()
        self._conn = None

    def _ensure_connected(self):
        if self._conn is not None and not self._conn.closed:
            return
        self._conn = self._psycopg2.connect(**self._db_config)
        cursor = self._conn.cursor()
        try:
            init_all_tables(cursor)
            self._conn.commit()
        finally:
            cursor.close()

    def save_event(self, event: DetectionEvent) -> bool:
        self._ensure_connected()
        try:
            with self._conn.cursor() as cursor:
                # SQL 은 common/schema.py 하나에만 둡니다 (스키마 drift 방지).
                cursor.execute(INSERT_PATROL_LOG_SQL, patrol_log_params(event))
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[PostgresDataPlatform] 이벤트 저장 실패: {exc}")
            self._conn.rollback()
            return False

    def save_response_plan(self, plan: ResponsePlan) -> bool:
        self._ensure_connected()
        try:
            with self._conn.cursor() as cursor:
                # 예전에는 여기만 `"\n".join(plan.reference_docs)` 라 근거가 없는
                # (reference_docs=None) 대응안에서 TypeError 로 죽었습니다.
                cursor.execute(INSERT_RESPONSE_PLAN_SQL, response_plan_params(plan))
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[PostgresDataPlatform] 대응안 저장 실패: {exc}")
            self._conn.rollback()
            return False

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


# ==========================================
# 5. 통합 오케스트레이터
# ==========================================
class PatrolIntegrationOrchestrator:
    """
    비전 -> DB 적재 -> (위험 시) LLM 대응안 생성 -> DB 저장
    까지 이어지는 전체 흐름을 조율하는 클래스.

    대응안 중복 억제
    ----------------
    젯슨이 30fps로 추론하므로 위험 상황 하나가 10초만 지속돼도 같은 (구역, 객체)
    조합의 '위험' 이벤트가 수백 건 들어옵니다. 그대로 두면 같은 문장이 계속
    쌓여서 관제 화면에서 정작 다른 위험을 못 보게 되고, LLM 호출 비용도 그만큼
    낭비됩니다.

    예전에는 이 억제가 pipeline/patrol_pipeline_rag.py 에만 있었습니다. 그쪽은
    실제 RagResponseAgent 를 쓰고 여기는 Mock 만 쓰던 시절이라 당장 문제가 없었을
    뿐, 여기에 실제 에이전트를 꽂는 순간 억제가 통째로 빠지는 구조였습니다 —
    common/schema.py 를 만들어 없애려던 바로 그 "정책이 한쪽에만 반영되는" 패턴이라
    같은 ResponsePlanThrottle 을 여기서도 씁니다.

    억제를 끄려면 suppress_seconds=0 을 주세요 (모든 이벤트가 통과).
    """

    def __init__(
        self,
        vision_adapter: VisionModuleAdapter,
        data_platform: DataPlatformPort,
        response_agent: Optional[ResponseAgentAdapter] = None,
        on_event: Optional[Callable[[DetectionEvent], None]] = None,
        suppress_seconds: Optional[float] = None,
    ):
        self.vision_adapter = vision_adapter
        self.data_platform = data_platform
        self.response_agent = response_agent
        self.on_event = on_event
        # 기본 간격은 common/schema.py 의 RESPONSE_PLAN_SUPPRESS_SECONDS 입니다.
        # 정책을 바꾸려면 이 파일이 아니라 그쪽 상수를 고치세요.
        self.throttle = (ResponsePlanThrottle() if suppress_seconds is None
                         else ResponsePlanThrottle(suppress_seconds))
        # 억제된 건수. 조용히 버리면 "대응안이 왜 안 나오지?"를 디버깅할 수 없습니다.
        self.suppressed_count = 0

    def process_once(self) -> Optional[DetectionEvent]:
        event = self.vision_adapter.get_next_event()
        if event is None:
            return None

        # patrol_logs 는 시계열 기록이라 억제하지 않습니다 — 전부 남깁니다.
        self.data_platform.save_event(event)

        if self.on_event:
            self.on_event(event)

        # 위험 등급일 때만 LLM 에이전트를 호출 (불필요한 API 비용 방지)
        if event.risk_level == "위험" and self.response_agent is not None:
            # 같은 (구역, 객체) 조합이 억제 간격 안에 또 들어오면 건너뜁니다.
            # 단 위험도가 직전보다 올라갔으면 간격과 무관하게 통과합니다 —
            # 상황이 악화되는 순간을 놓치면 억제 자체가 사고 원인이 됩니다.
            if self.throttle.should_generate(event):
                plan = self.response_agent.generate_response(event)
                self.data_platform.save_response_plan(plan)
            else:
                self.suppressed_count += 1

        return event


# ==========================================
# 6. 테스트용 Mock 구현체 (실제 어댑터 없이 통합 로직만 확인하고 싶을 때 사용)
# ==========================================
class _MockVisionAdapter:
    """비전 모듈 없이 통합 로직만 테스트하고 싶을 때 사용하는 목업."""

    def __init__(self):
        self._count = 0

    def get_next_event(self) -> Optional[DetectionEvent]:
        self._count += 1
        if self._count % 2 == 0:
            return None
        return DetectionEvent(
            zone="A",
            detected_object="작업자",
            distance=1.2,
            box_position="x:120,y:80",
            risk_level="위험",
            issue="작업자 근접 위험 (mock)",
        )


class _MockDataPlatform:
    """실제 DB 대신 콘솔에 출력만 하는 목업. DB가 아직 준비 안 됐을 때 사용."""

    def save_event(self, event: DetectionEvent) -> bool:
        print(f"[DB 저장] {event}")
        return True

    def save_response_plan(self, plan: ResponsePlan) -> bool:
        print(f"[대응안 저장] {plan.recommended_action}")
        return True


class _MockResponseAgent:
    """실제 RagResponseAgent(정진철 파트) 없이 통합 로직만 테스트하고 싶을 때 사용하는 목업."""

    def generate_response(self, event: DetectionEvent) -> ResponsePlan:
        return ResponsePlan(
            zone=event.zone,
            source_event=event,
            recommended_action=f"{event.zone} 구역 관리자에게 즉시 알림 발송 (mock)",
            reference_docs=["산업안전보건법 시행규칙(mock)"],
            confidence=0.5,
            generation_mode="mock",
        )


def _build_data_platform() -> DataPlatformPort:
    """DB 접속 정보가 있으면 실제 Postgres에, 없으면 콘솔 출력 Mock에 저장.

    psycopg2 연결 자체가 실패해도(현장에 DB가 아직 없는 경우 등) 스크립트가
    죽지 않고 Mock으로 자동 대체되도록 했습니다.
    """
    try:
        platform = PostgresDataPlatform()
        platform._ensure_connected()  # 연결 가능 여부를 미리 확인
        print("[통합 어댑터] PostgreSQL에 연결됨 — 실제 DB에 저장합니다.")
        return platform
    except Exception as exc:
        print(f"[통합 어댑터] DB 연결 실패({exc}) — Mock 저장소로 대체합니다.")
        return _MockDataPlatform()


if __name__ == "__main__":
    # 비전 모듈(NanoOwlUdpVisionAdapter)은 실제 구현체를 사용하고,
    # 아직 도착하지 않은 디지털 트윈/LLM 에이전트는 Mock으로 대체합니다.
    # 데이터 플랫폼은 DB 연결이 가능하면 실제 Postgres, 안 되면 자동으로
    # Mock으로 대체되므로 DB가 아직 없어도 스모크 테스트가 가능합니다.
    vision_adapter = NanoOwlUdpVisionAdapter(zone="A")
    vision_adapter.start()

    orchestrator = PatrolIntegrationOrchestrator(
        vision_adapter=vision_adapter,
        data_platform=_build_data_platform(),
        response_agent=_MockResponseAgent(),
    )
    print(f"NanoOWL UDP({os.environ.get('PIPELINE_UDP_BIND', '0.0.0.0')}:9998) "
          "수신 대기 중... (Ctrl+C로 종료)")
    try:
        while True:
            event = orchestrator.process_once()
            if event is None:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        vision_adapter.stop()
