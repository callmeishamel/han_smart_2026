"""
Jetson 순찰 로봇 -> PostgreSQL 데이터 적재 + 대응안 생성 파이프라인

역할
----
jetson_patrol_pipeline.py 와 동일하게 UDP로 들어오는 NanoOWL 탐지 결과를
patrol_logs에 적재하되, 위험 등급 이벤트에 대해서는 산업안전 지침을 근거로
대응 지침을 생성해 response_plans에 함께 저장합니다.

수신 포트는 기본 0.0.0.0:9998 이며 환경변수로 바꿉니다.

    PIPELINE_UDP_BIND    바인드 주소 (기본 0.0.0.0)
    DASHBOARD_UDP_PORT   수신 포트   (기본 9998)

현재 배선은 9999 가 ROS2 브릿지, 9998 이 대시보드·DB·RAG 파이프라인,
9091 이 미니맵입니다. 9999 를 잡으면 ROS2 브릿지와 같은 포트를 두고
다투게 되어 한쪽만 패킷을 받습니다.

jetson_patrol_pipeline.py 와의 관계
------------------------------------
기존 파일을 수정하지 않고, PatrolLogWriter와 iter_udp_detections를 그대로
가져다 씁니다. 적재 로직과 UDP 수신 로직이 두 벌로 갈라지면 나중에 한쪽만
고쳐지는 문제가 생기므로(common/schema.py 도입 배경과 같은 이유),
이 파일은 "대응안 생성 단계"만 추가로 얹습니다.

    jetson_patrol_pipeline.py   UDP 수신 -> patrol_logs 적재
    patrol_pipeline_rag.py      UDP 수신 -> patrol_logs 적재
                                        -> (위험 등급) 대응안 생성
                                        -> response_plans 적재

둘 다 같은 UDP 포트를 쓰므로 동시에 실행할 수 없습니다. 하나만 실행하세요.
대응안 생성이 필요 없는 상황에서는 기존 run_pipeline.sh를 그대로 쓰면 됩니다.

LLM 생성과 수신 루프
--------------------
--use-llm 만 켜면 GEMMA 2B 응답이 끝날 때까지 UDP 수신 루프가 멈춥니다.
UDP 는 재전송이 없으므로 그 사이 도착한 탐지 패킷은 소켓 버퍼가 차는
순간 버려지고 patrol_logs 에도 남지 않습니다. CPU 추론(Ollama)에서는
생성 한 건에 수 초에서 수십 초가 걸려 실제로 누락이 발생합니다.

--async-llm 을 함께 켜면 생성 단계가 별도 스레드로 분리되어 수신 루프가
멈추지 않습니다. 기본값은 꺼져 있으므로 켜지 않으면 기존 동작 그대로입니다.

사용법
------
    source set_env.sh
    python3 pipeline/patrol_pipeline_rag.py --zone A
    python3 pipeline/patrol_pipeline_rag.py --zone A --use-llm --async-llm

    (권장) 루트의 실행 스크립트를 사용하세요.
    ./run_pipeline_rag.sh
"""

import argparse
import logging
import os
import sys

# common 패키지를 찾을 수 있도록 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 공통 스키마와 UDP 수신기는 데이터 플랫폼 파트에 있다. 저장소에서 두 파트를
# 분리해 둔 상태에서도 `rag_upload`를 단독 실행할 수 있도록 형제 경로를 추가한다.
_PLATFORM_ROOT = os.path.join(
    os.path.dirname(_PROJECT_ROOT), "데이터 플랫폼 및 대시보드(이상민)"
)
if os.path.isdir(_PLATFORM_ROOT) and _PLATFORM_ROOT not in sys.path:
    sys.path.insert(0, _PLATFORM_ROOT)

from common.schema import DetectionEvent, get_db_config          # noqa: E402
from pipeline.jetson_patrol_pipeline import (                    # noqa: E402
    PatrolLogWriter,
    iter_udp_detections,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("patrol_pipeline_rag")


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
                cursor.execute(
                    """
                    INSERT INTO response_plans
                        (zone, detected_object, risk_level,
                         recommended_action, reference_docs, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        plan.zone,
                        plan.source_event.detected_object,
                        plan.source_event.risk_level,
                        plan.recommended_action,
                        "\n".join(plan.reference_docs or []),
                        plan.confidence,
                    ),
                )
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
                        help="이 Jetson이 담당하는 구역 (예: A, B, C)")
    # 배선: 9999 ROS2 브릿지 / 9998 대시보드·DB·RAG / 9091 미니맵.
    # 비전 모듈이 엣지 보드에서 보내므로 기본 바인드는 0.0.0.0 이다.
    parser.add_argument(
        "--udp-ip",
        default=os.environ.get("PIPELINE_UDP_BIND", "0.0.0.0"),
        help="UDP 수신 바인드 주소 (환경변수 PIPELINE_UDP_BIND, 기본 0.0.0.0)")
    parser.add_argument(
        "--udp-port", type=int,
        default=int(os.environ.get("DASHBOARD_UDP_PORT", "9998")),
        help="비전 모듈이 보내는 UDP 포트 "
             "(환경변수 DASHBOARD_UDP_PORT, 기본 9998)")
    parser.add_argument("--use-embedding", action="store_true",
                        help="벡터 유사도 검색 사용 (메모리 여유가 있을 때만)")
    parser.add_argument("--use-llm", action="store_true",
                        help="GEMMA 2B로 대응 지침 문장 생성 "
                             "(서버가 없으면 규칙 기반으로 자동 대체)")
    parser.add_argument("--async-llm", action="store_true",
                        help="대응안 생성을 별도 스레드로 분리 "
                             "(LLM 생성 중에도 UDP 수신을 계속한다)")
    parser.add_argument("--plan-queue-size", type=int, default=32,
                        help="비동기 대응안 대기 큐 크기 (기본 32)")
    parser.add_argument("--verbose-rag", action="store_true",
                        help="대응안 생성 과정을 자세히 출력")
    args = parser.parse_args()

    # 대응안 생성기 준비.
    # 지식베이스나 임베딩 모델이 없어도 내장 지침으로 동작하므로
    # 이 단계에서 실패하지 않습니다.
    from integration.response_agent import RagResponseAgent
    agent = RagResponseAgent(
        use_embedding=args.use_embedding,
        verbose=args.verbose_rag,
        use_llm=args.use_llm,
    )

    writer = RagPatrolLogWriter(get_db_config())
    logger.info("순찰 파이프라인 시작 (구역: %s, 대응안 생성 포함)", args.zone)
    logger.info("벡터 검색 %s", "사용" if args.use_embedding else "미사용")
    if args.use_llm:
        logger.info(
            "LLM 생성 사용 -> %s (%s). 서버가 없으면 규칙 기반으로 대체합니다",
            os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1"),
            os.environ.get("LLM_MODEL", "google/gemma-2-2b-it"),
        )
    else:
        logger.info("LLM 생성 미사용 (--use-llm 으로 켤 수 있습니다)")

    # 대응안 적재용 writer.
    # 비동기일 때는 워커 스레드가 쓰므로 메인 루프와 커넥션을 나눈다.
    # psycopg2 커넥션을 두 스레드가 공유하면 한쪽 commit 이 다른 쪽의
    # 열린 트랜잭션까지 커밋한다.
    worker = None
    if args.async_llm:
        from integration.response_plan_worker import ResponsePlanWorker
        plan_writer = RagPatrolLogWriter(get_db_config())
    else:
        plan_writer = writer

    def handle_plan(plan) -> bool:
        """대응안 1건의 후처리를 한곳에 모은다.

        동기 경로와 비동기 경로가 같은 처리를 거치게 하기 위한 함수다.
        비동기일 때 이 함수는 워커 스레드에서 호출된다.

        음성 경보(TTS)를 붙일 때도 여기에 넣으면 두 경로 모두에 적용된다.
        화면용 전문(200자 안팎)을 그대로 보내면 재생이 길어져 큐가 밀리므로,
        짧은 방송 문장을 따로 만들어 보내야 한다.
        """
        return plan_writer.write_response_plan(plan)

    if args.async_llm:
        worker = ResponsePlanWorker(
            agent, handle_plan,
            queue_size=args.plan_queue_size,
            logger=logger,
        )
        worker.start()
        logger.info("비동기 대응안 생성 사용 (큐 %d건)", args.plan_queue_size)
    else:
        logger.info("동기 대응안 생성 (--async-llm 으로 분리할 수 있습니다)")

    n_event = 0
    n_plan = 0

    try:
        for event in iter_udp_detections(args.zone, args.udp_ip, args.udp_port):
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

            # 비동기일 때는 큐에 넣고 즉시 다음 패킷을 받는다.
            # 중복 억제를 넣게 되면 이 위치에서 submit 앞에 판정해야 한다.
            if worker is not None:
                worker.submit(event)
                continue

            plan = agent.generate_response(event)
            if handle_plan(plan):
                n_plan += 1
                logger.info(
                    "대응 지침 생성 [%s] 확신도 %.2f | 근거 %d건 (누적 %d건)",
                    event.zone, plan.confidence or 0.0,
                    len(plan.reference_docs or []), n_plan,
                )

    except KeyboardInterrupt:
        logger.info("종료 신호 감지, 파이프라인을 정리합니다.")
    finally:
        # 비동기에서는 워커를 정리해야 대응안 건수가 확정되므로,
        # 처리 결과 로그를 finally 맨 끝에 둔다.
        if worker is not None:
            worker.close()
            st = worker.stats()
            n_plan += st["written"]
            logger.info(
                "비동기 대응안: 생성 %d건 / 적재 %d건 / 드롭 %d건 / "
                "실패 %d건 / 최대대기 %dms",
                st["generated"], st["written"], st["dropped"],
                st["failed"], st["max_lag_ms"],
            )
            if st["dropped"]:
                logger.warning(
                    "큐 포화로 대응안 %d건을 건너뛰었습니다. 탐지 적재는 "
                    "정상이며, LLM 생성이 이벤트 속도를 따라가지 못한 "
                    "상태입니다", st["dropped"],
                )
            plan_writer.close()
        agent.close()
        writer.close()
        logger.info("처리 결과: 이벤트 %d건 / 대응안 %d건", n_event, n_plan)


if __name__ == "__main__":
    main()
