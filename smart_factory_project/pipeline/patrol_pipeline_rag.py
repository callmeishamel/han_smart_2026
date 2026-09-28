"""
Jetson 순찰 로봇 -> PostgreSQL 데이터 적재 + 대응안 생성 파이프라인

역할
----
jetson_patrol_pipeline.py 와 동일하게 UDP(127.0.0.1:9998, 대시보드/DB용 포트)로
들어오는 NanoOWL 탐지 결과를 patrol_logs에 적재하되, 위험 등급 이벤트에 대해서는
산업안전 지침을 근거로 대응 지침을 생성해 response_plans에 함께 저장합니다.

jetson_patrol_pipeline.py 와의 관계
------------------------------------
기존 파일을 수정하지 않고, PatrolLogWriter와 iter_udp_detections를 그대로
가져다 씁니다. 적재 로직과 UDP 수신 로직이 두 벌로 갈라지면 나중에 한쪽만
고쳐지는 문제가 생기므로(common/schema.py 도입 배경과 같은 이유),
이 파일은 "대응안 생성 단계"만 추가로 얹습니다.

    jetson_patrol_pipeline.py   UDP 수신 -> patrol_logs 적재
    patrol_pipeline_rag.py      UDP 수신 -> patrol_logs 적재
                                        -> (위험 등급) 중복 억제 통과 시 대응안 생성
                                        -> response_plans 적재
                                        -> (--tts) 젯슨 스피커로 음성 방송

둘 다 같은 UDP 포트를 쓰므로 동시에 실행할 수 없습니다. 하나만 실행하세요.
대응안 생성이 필요 없는 상황에서는 기존 run_pipeline.sh를 그대로 쓰면 됩니다.

중복 억제
---------
젯슨이 30fps로 추론하므로 위험 상황 하나가 수백 건의 이벤트로 들어옵니다.
같은 (구역, 객체) 조합은 기본 30초에 한 번만 대응안을 만들되, 위험도가
직전보다 올라가면 간격과 무관하게 생성합니다. patrol_logs 적재는 억제하지
않으므로 시계열 기록은 그대로 남습니다.
간격 조정은 --suppress-seconds, 정책 자체는 common/schema.py를 보세요.

사용법
------
    source set_env.sh
    python3 pipeline/patrol_pipeline_rag.py --zone A

    # 억제 간격을 60초로 늘리거나(--suppress-seconds 60) 끄고 싶을 때(0)
    python3 pipeline/patrol_pipeline_rag.py --zone A --suppress-seconds 0

    # 현장 스피커 음성 방송까지 (젯슨에서 Rx_pipeline.py가 먼저 떠 있어야 함)
    python3 pipeline/patrol_pipeline_rag.py --zone A --tts

    (권장) 루트의 실행 스크립트를 사용하세요.
    ./run_pipeline_rag.sh
    ./run_pipeline_rag.sh --tts

음성 경보
---------
--tts를 주면 대응안을 젯슨 스피커로 방송합니다. 송신 모듈은 저장소 최상위의
"TTS Engine and pipeline/" 폴더에서 찾으며, 없으면 경고만 남기고 나머지는
그대로 동작합니다. 폴더 위치가 다르면 TTS_MODULE_DIR로 지정하세요.
"""

import argparse
import logging
import os
import sys
import time

# common 패키지를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 저장소의 통합본(smart_factory_project)은 edge 실행 파일만 모아 두므로,
# RAG 구현이 데이터 플랫폼 원본에 남아 있을 수 있습니다. 폴더를 복제하지 않고도
# 저장소 안에서 실행되게 하되, Jetson 배포본에서는 SFP_COMPONENT_ROOT 또는 같은
# 프로젝트 루트에 복사된 integration/을 우선 사용합니다.
_COMPONENT_ROOT = os.environ.get("SFP_COMPONENT_ROOT", "").strip()
_REPOSITORY_DATA_ROOT = os.path.join(
    os.path.dirname(_PROJECT_ROOT), "데이터 플랫폼 및 대시보드(이상민)")
for _candidate_root in (_COMPONENT_ROOT, _PROJECT_ROOT, _REPOSITORY_DATA_ROOT):
    if (_candidate_root
            and os.path.isfile(os.path.join(
                _candidate_root, "integration", "response_agent.py"))):
        if _candidate_root not in sys.path:
            sys.path.append(_candidate_root)
        break

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (                                      # noqa: E402
    INSERT_RESPONSE_PLAN_SQL,
    RESPONSE_PLAN_SUPPRESS_SECONDS,
    DetectionEvent,
    ResponsePlanThrottle,
    get_db_config,
    response_plan_params,
)
from pipeline.jetson_patrol_pipeline import (                    # noqa: E402
    DEFAULT_UDP_BIND,
    PatrolLogWriter,
    iter_udp_detections,
    resolve_zone_argument,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("patrol_pipeline_rag")

try:
    DEFAULT_TTS_COOLDOWN_SECONDS = max(
        0.0, float(os.environ.get("TTS_EMERGENCY_COOLDOWN_SEC", "30")))
except ValueError:
    DEFAULT_TTS_COOLDOWN_SECONDS = 30.0
try:
    DEFAULT_TTS_CONFIRM_HITS = max(1, int(os.environ.get("TTS_CONFIRM_HITS", "3")))
except ValueError:
    DEFAULT_TTS_CONFIRM_HITS = 3
try:
    DEFAULT_TTS_CONFIRM_WINDOW_SECONDS = max(
        0.1, float(os.environ.get("TTS_CONFIRM_WINDOW_SEC", "2")))
except ValueError:
    DEFAULT_TTS_CONFIRM_WINDOW_SECONDS = 2.0


class TtsAlertConfirmation:
    """단 한 번 잡힌 오탐을 음성 경보로 만들지 않는 확인기입니다."""

    def __init__(self, required_hits=DEFAULT_TTS_CONFIRM_HITS,
                 window_seconds=DEFAULT_TTS_CONFIRM_WINDOW_SECONDS):
        self.required_hits = max(1, int(required_hits))
        self.window_seconds = max(0.1, float(window_seconds))
        self._seen = {}

    def observe(self, event, now=None):
        now = time.monotonic() if now is None else float(now)
        key = (str(event.zone), str(event.detected_object), str(event.risk_level))
        count, first_at, last_at = self._seen.get(key, (0, now, now))
        if now - last_at > self.window_seconds:
            count, first_at = 0, now
        count += 1
        self._seen[key] = (count, first_at, now)
        expiry = self.window_seconds * 2
        self._seen = {k: state for k, state in self._seen.items()
                      if now - state[2] <= expiry}
        if count < self.required_hits or now - first_at > self.window_seconds:
            return False
        self._seen.pop(key, None)
        return True


# ==========================================
# TTS 음성 경보 (선택 기능)
# ==========================================
# TTS 파트는 저장소의 별도 최상위 폴더에 있고, 파이프라인 동작의 필수 요소가
# 아닙니다. 그래서 없으면 조용히 끄고 나머지는 그대로 돌아가게 합니다 —
# RagResponseAgent가 지식베이스 없이도 내장 지침으로 동작하고,
# team_integration_adapter가 Mock으로 폴백하는 것과 같은 방식입니다.
#
# 폴더를 옮겼다면 TTS_MODULE_DIR로 위치를 직접 지정할 수 있습니다.
#
# 후보를 순서대로 봅니다. **젯슨 배포본은 이 파일 바로 옆에 tts/ 를 둡니다**
# (~/smart_factory_project/{pipeline, tts}). 예전에는 저장소 레이아웃만 보고
# 상위 폴더의 "TTS Engine and pipeline" 하나만 찾았는데, 젯슨에는 그 폴더가
# 없으므로 **거기서는 항상 실패했습니다.** 실패해도 예외가 아니라 경고 한 줄로
# 끝나고(음성 경보만 조용히 꺼짐) 파이프라인은 정상으로 보이기 때문에,
# "스피커만 안 운다"는 증상으로만 나타나 원인을 찾기 어려웠습니다.
_TTS_DIR_CANDIDATES = (
    os.path.join(_PROJECT_ROOT, "tts"),                                      # 젯슨 배포본
    os.path.join(os.path.dirname(_PROJECT_ROOT), "TTS Engine and pipeline"),  # 저장소
)
_DEFAULT_TTS_DIR = _TTS_DIR_CANDIDATES[-1]


def _resolve_tts_dir() -> str:
    """TTS 송신 모듈이 있는 폴더. TTS_MODULE_DIR이 있으면 그것만 씁니다.

    후보 중에서 Rag_to_Jetson.py가 **실제로 있는** 곳을 고릅니다. 폴더 존재만
    보면 빈 tts/ 가 있을 때 그쪽을 골라 놓고 import에서 실패합니다.
    """
    override = os.environ.get("TTS_MODULE_DIR", "").strip()
    if override:
        return override
    for candidate in _TTS_DIR_CANDIDATES:
        if os.path.isfile(os.path.join(candidate, "Rag_to_Jetson.py")):
            return candidate
    return _DEFAULT_TTS_DIR   # 못 찾았을 때 경고에 보여줄 경로


def load_tts_sender():
    """(확정 문제 송신, 문제 판정, 종료, 통계, 경로)를 반환합니다."""
    tts_dir = _resolve_tts_dir()
    if tts_dir not in sys.path:
        sys.path.insert(0, tts_dir)
    try:
        from Rag_to_Jetson import (
            close_tts_sender,
            get_tts_sender_stats,
            is_tts_alert_event,
            send_detection_alert_async,
        )
    except ImportError as exc:
        logger.warning("TTS 모듈을 찾지 못해 음성 경보를 끕니다 — %s (%s)", tts_dir, exc)
        logger.warning("  폴더 위치가 다르면 TTS_MODULE_DIR 환경변수로 지정하세요.")
        return None, None, None, None, tts_dir
    return (send_detection_alert_async, is_tts_alert_event,
            close_tts_sender, get_tts_sender_stats, tts_dir)


class RagPatrolLogWriter(PatrolLogWriter):
    """PatrolLogWriter에 대응안 저장 기능을 추가한 확장 클래스.

    DB 재연결/백오프 로직은 부모 클래스 것을 그대로 사용합니다.
    response_plans 테이블은 부모의 _connect()가 호출하는 init_all_tables()에서
    이미 생성되므로 별도 DDL이 필요 없습니다.
    """

    def write_response_plan(self, plan) -> bool:
        """생성된 대응안 1건을 response_plans에 적재합니다.

        참조 문서 목록은 줄바꿈으로 이어붙여 TEXT 컬럼에 저장합니다.
        대시보드에서 줄 단위로 나누면 그대로 목록이 됩니다.
        """
        self._ensure_connected()
        try:
            with self._conn.cursor() as cursor:
                # SQL 은 common/schema.py 하나에만 둡니다 (스키마 drift 방지).
                cursor.execute(INSERT_RESPONSE_PLAN_SQL, response_plan_params(plan))
            self._conn.commit()
            return True
        except Exception as exc:
            logger.error("대응안 적재 실패: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Jetson 순찰 로봇 -> DB 적재 + 대응안 생성 파이프라인")
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
    parser.add_argument("--use-embedding", action="store_true",
                        help="벡터 유사도 검색 사용 (메모리 여유가 있을 때만)")
    parser.add_argument("--use-llm", action="store_true",
                        help=("검색한 근거로 경량 LLM이 대응 지침 문장을 생성합니다 "
                              "(rag/vllm_client.py). 서버가 없거나 실패하면 규칙 기반 "
                              "문장으로 그대로 떨어지므로 켜 두어도 안전합니다. "
                              "서버 주소는 LLM_BASE_URL / LLM_MODEL 환경변수"))
    parser.add_argument("--verbose-rag", action="store_true",
                        help="대응안 생성 과정을 자세히 출력")
    parser.add_argument("--suppress-seconds", type=float,
                        default=RESPONSE_PLAN_SUPPRESS_SECONDS,
                        help=(f"같은 (구역, 객체) 조합의 대응안 재생성 최소 간격(초). "
                              f"기본 {RESPONSE_PLAN_SUPPRESS_SECONDS:g}초, 0이면 억제 없음. "
                              f"위험도가 올라가면 간격과 무관하게 생성합니다"))
    parser.add_argument("--tts", action="store_true",
                        help=("생성되는 대응안을 **전부** 젯슨 스피커로 자동 방송합니다. "
                              "젯슨에서 TTS Engine and pipeline/Rx_pipeline.py가 "
                              "먼저 떠 있어야 합니다. 오탐까지 그대로 방송되므로, "
                              "관제자가 보고 고를 수 있게 하려면 이 옵션 대신 "
                              "대시보드의 '현장 음성 안내' 버튼을 쓰세요"))
    parser.add_argument("--tts-alert-cooldown", type=float,
                        default=DEFAULT_TTS_COOLDOWN_SECONDS)
    parser.add_argument("--tts-confirm-hits", type=int,
                        default=DEFAULT_TTS_CONFIRM_HITS)
    parser.add_argument("--tts-confirm-window", type=float,
                        default=DEFAULT_TTS_CONFIRM_WINDOW_SECONDS)
    args = parser.parse_args()

    # 대응안 생성기 준비.
    # 지식베이스나 임베딩 모델이 없어도 내장 지침으로 동작하므로
    # 이 단계에서 실패하지 않습니다.
    try:
        from integration.response_agent import RagResponseAgent
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("integration"):
            raise RuntimeError(
                "RAG 구성요소가 없습니다. integration/ 폴더를 함께 배포하거나 "
                "SFP_COMPONENT_ROOT에 데이터 플랫폼 경로를 지정하세요."
            ) from exc
        raise
    agent = RagResponseAgent(
        use_embedding=args.use_embedding,
        verbose=args.verbose_rag,
        use_llm=args.use_llm,
    )

    writer = RagPatrolLogWriter(get_db_config())

    # 같은 위험 상황이 30fps로 계속 잡혀도 대응안은 일정 간격에 한 번만
    # 만듭니다. 억제 정책 자체는 common/schema.py에 있습니다.
    throttle = ResponsePlanThrottle(args.suppress_seconds)
    tts_alert_throttle = ResponsePlanThrottle(max(0.0, args.tts_alert_cooldown))
    tts_confirmation = TtsAlertConfirmation(
        args.tts_confirm_hits, args.tts_confirm_window)

    # 음성 경보는 --tts를 준 경우에만 켭니다. 기본으로 켜두면 젯슨 TTS 수신기
    # 없이 개발용으로 돌릴 때(self_test/fake_jetson_sender.py 등) 매 경보마다
    # 연결 실패 로그가 쌓이기 때문입니다.
    send_tts_alert = None
    is_tts_alert = None
    close_tts = None
    tts_stats = None
    if args.tts:
        (send_tts_alert, is_tts_alert,
         close_tts, tts_stats, _) = load_tts_sender()

    zone = resolve_zone_argument(args.zone, args.zone_fallback)
    logger.info("순찰 파이프라인 시작 (구역: %s, 대응안 생성 포함)", args.zone)
    logger.info("벡터 검색 %s", "사용" if args.use_embedding else "미사용")
    if args.use_llm:
        logger.info("LLM 생성 사용 -> %s (%s). 서버가 없으면 규칙 기반으로 대체합니다",
                    os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1"),
                    os.environ.get("LLM_MODEL", "google/gemma-2-2b-it"))
    else:
        logger.info("LLM 생성 미사용 (--use-llm 으로 켤 수 있습니다)")
    if args.suppress_seconds > 0:
        logger.info("대응안 중복 억제 %g초 (구역+객체 기준, 위험도 상향 시 예외)",
                    args.suppress_seconds)
    else:
        logger.info("대응안 중복 억제 사용 안 함")
    if send_tts_alert is not None:
        logger.info("음성 경보 켜짐 -> %s:%s",
                    os.environ.get("JETSON_IP", "203.0.113.10"),
                    os.environ.get("TTS_TCP_PORT", "9997"))
    else:
        logger.info("음성 경보 꺼짐 (--tts 로 켤 수 있습니다)")

    n_event = 0
    n_plan = 0
    n_suppressed = 0

    try:
        for event in iter_udp_detections(zone, args.udp_ip, args.udp_port):
            if not writer.write(event):
                continue

            n_event += 1
            logger.info(
                "적재 완료 [%s] %s (위험도: %s, 거리: %sm)",
                event.zone, event.detected_object,
                event.risk_level, event.distance,
            )

            # 위험 등급에서만 대응안을 생성합니다.
            # 주의/정상 등급까지 생성하면 같은 내용이 반복 저장되고
            # 연산 부담도 커지므로 제외합니다.
            if event.risk_level != "위험":
                continue

            # 직전 대응안과 같은 (구역, 객체)이고 위험도도 그대로면 건너뜁니다.
            # patrol_logs에는 이미 적재된 뒤이므로 기록이 누락되지는 않습니다.
            if not throttle.should_generate(event):
                n_suppressed += 1
                logger.debug(
                    "대응안 억제 [%s] %s (최근 %g초 내 동일 조합, 누적 %d건)",
                    event.zone, event.detected_object,
                    args.suppress_seconds, n_suppressed,
                )
                continue

            plan = agent.generate_response(event)
            if writer.write_response_plan(plan):
                n_plan += 1
                logger.info(
                    "대응 지침 생성 [%s] 확신도 %.2f | 근거 %d건 (누적 %d건, 억제 %d건)",
                    event.zone, plan.confidence or 0.0,
                    len(plan.reference_docs or []), n_plan, n_suppressed,
                )

                if send_plan is not None:
                    # 구역·객체·대응안을 공통 형식으로 만들고 bounded sender
                    # worker에서 젯슨의 queued ACK까지 확인합니다.
                    send_plan(plan)

    except KeyboardInterrupt:
        logger.info("종료 신호 감지, 파이프라인을 정리합니다.")
    finally:
        final_tts_stats = None
        if close_tts is not None:
            final_tts_stats = close_tts()
        elif tts_stats is not None:
            final_tts_stats = tts_stats()
        agent.close()
        writer.close()
        if final_tts_stats is not None:
            logger.info(
                "TTS 송신: 제출 %d건 / 젯슨 큐 등록 %d건 / 실패 %d건 / "
                "드롭 %d건 / 종료 시 대기 %d건",
                final_tts_stats["submitted"], final_tts_stats["queued"],
                final_tts_stats["failed"], final_tts_stats["dropped"],
                final_tts_stats["pending"],
            )
        logger.info("처리 결과: 이벤트 %d건 / 대응안 %d건 / 억제 %d건",
                    n_event, n_plan, n_suppressed)


if __name__ == "__main__":
    main()
