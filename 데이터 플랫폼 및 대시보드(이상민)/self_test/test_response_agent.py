"""
대응안 생성기 단독 테스트

DB와 네트워크 없이 동작합니다. 가짜 DetectionEvent를 만들어
generate_response()가 올바른 ResponsePlan을 반환하는지 확인합니다.

기존 Mock 구현체와 입출력 형태가 같은지도 함께 검증하므로,
오케스트레이터에 끼워 넣기 전에 이 테스트만 통과하면 됩니다.

실행
----
    python3 self_test/test_response_agent.py
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import DetectionEvent, build_detection_event      # noqa: E402
from integration.team_integration_adapter import (                   # noqa: E402
    ResponsePlan, _MockResponseAgent,
)
from integration.response_agent import (                             # noqa: E402
    RagResponseAgent, MAX_ACTION_LEN, resolve_risk_type,
)

BAR = "=" * 72
passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [통과] {label}")
    else:
        failed += 1
        print(f"  [실패] {label} {detail}")


# 현재 탐지 클래스 기준 검증 시나리오
CASES = [
    DetectionEvent(zone="A", detected_object="안전모 미착용", distance=1.1,
                   box_position="x:120,y:80", risk_level="위험",
                   issue="안전모 미착용 작업자 근접 (1.1m)"),
    DetectionEvent(zone="B", detected_object="화재", distance=3.5,
                   box_position="x:400,y:200", risk_level="위험",
                   issue="화재 감지"),
    DetectionEvent(zone="C", detected_object="차량·중장비", distance=1.3,
                   box_position="x:300,y:150", risk_level="위험",
                   issue="차량 근접 위험 (1.3m)"),
    DetectionEvent(zone="A", detected_object="작업자", distance=-1.0,
                   box_position="x:200,y:100", risk_level="위험",
                   issue="작업자 감지 (거리 미상)"),
    # 매핑에 없는 객체가 들어와도 예외 없이 처리되어야 합니다
    DetectionEvent(zone="B", detected_object="unknown object", distance=2.0,
                   box_position="x:10,y:10", risk_level="위험",
                   issue="미분류 객체 감지: unknown object"),
]


def test_mapping():
    print("\n[1] 위험유형 매핑")
    print("-" * 72)
    expect = {
        "person with no helmet": "PPE",
        "안전모 미착용": "PPE",
        "fire": "fire",
        "화재": "fire",
        "vehicle": "machinery",
        "차량·중장비": "machinery",
        "작업자": "access",
        "정의되지않은객체": "general",
    }
    for obj, want in expect.items():
        got = resolve_risk_type(obj)
        check(f"{obj} -> {got}", got == want, f"(기대 {want})")


def test_generate():
    print("\n[2] 대응안 생성")
    print("-" * 72)
    agent = RagResponseAgent()

    for ev in CASES:
        plan = agent.generate_response(ev)
        tag = f"{ev.zone}/{ev.detected_object}"

        check(f"{tag} ResponsePlan 반환", isinstance(plan, ResponsePlan))
        check(f"{tag} zone 일치", plan.zone == ev.zone)
        check(f"{tag} source_event 보존", plan.source_event is ev)
        check(f"{tag} 대응안 비어있지 않음",
              bool(plan.recommended_action and plan.recommended_action.strip()))
        check(f"{tag} 길이 {len(plan.recommended_action)} <= {MAX_ACTION_LEN}",
              len(plan.recommended_action) <= MAX_ACTION_LEN)
        check(f"{tag} reference_docs 존재",
              isinstance(plan.reference_docs, list)
              and len(plan.reference_docs) > 0)
        check(f"{tag} confidence 범위",
              plan.confidence is None or 0.0 <= plan.confidence <= 1.0)

        print(f"     -> {plan.recommended_action[:66]}")
        print(f"        근거 {len(plan.reference_docs)}건 | "
              f"확신도 {plan.confidence}")

    agent.close()


def test_distance_penalty():
    print("\n[3] 거리 측정 실패 시 확신도 하향")
    print("-" * 72)
    agent = RagResponseAgent()

    ok = DetectionEvent(zone="A", detected_object="작업자", distance=1.0,
                        box_position="x:1,y:1", risk_level="위험",
                        issue="작업자 근접 위험 (1.0m)")
    bad = DetectionEvent(zone="A", detected_object="작업자", distance=-1.0,
                         box_position="x:1,y:1", risk_level="위험",
                         issue="작업자 감지 (거리 미상)")
    over = DetectionEvent(zone="A", detected_object="작업자", distance=300.0,
                          box_position="x:1,y:1", risk_level="위험",
                          issue="작업자 감지")

    p_ok = agent.generate_response(ok)
    p_bad = agent.generate_response(bad)
    p_over = agent.generate_response(over)

    check("거리 미상 확신도가 더 낮음", p_bad.confidence < p_ok.confidence,
          f"({p_bad.confidence} vs {p_ok.confidence})")
    check("비현실적 거리(300m)도 측정 실패로 처리",
          p_over.confidence < p_ok.confidence,
          f"({p_over.confidence} vs {p_ok.confidence})")
    check("거리 미상 근거에 현장 확인 안내 포함",
          any("거리 측정 실패" in r for r in p_bad.reference_docs))
    agent.close()


def test_mock_compatibility():
    print("\n[4] 기존 Mock 구현체와 입출력 형태 동일 여부")
    print("-" * 72)
    real = RagResponseAgent()
    mock = _MockResponseAgent()
    ev = CASES[0]

    r, m = real.generate_response(ev), mock.generate_response(ev)
    check("반환 타입 동일", type(r) is type(m))
    check("필드 구성 동일", set(vars(r).keys()) == set(vars(m).keys()))
    check("generate_response 메서드 보유", hasattr(real, "generate_response"))
    real.close()


def test_from_udp_payload():
    print("\n[5] 비전 모듈 payload 경로 검증")
    print("-" * 72)
    # ai_inference_sender.py가 실제로 보내는 형태
    payload_det = {"object": "person with no helmet",
                   "bbox": [120, 80, 350, 420],
                   "distance_meter": 1.85}
    ev = build_detection_event("A", payload_det)
    print(f"     변환 결과: object={ev.detected_object!r} "
          f"risk_level={ev.risk_level!r}")

    agent = RagResponseAgent()
    plan = agent.generate_response(ev)
    check("payload 경로에서도 대응안 생성", bool(plan.recommended_action))
    print(f"     -> {plan.recommended_action[:66]}")

    if ev.risk_level != "위험":
        print()
        print("     [확인 필요] 안전모 미착용이 '위험'으로 판정되지 않았습니다.")
        print("     파이프라인은 risk_level == '위험'일 때만 대응안을 생성하므로,")
        print("     현재 설정에서는 이 상황에 대응안이 만들어지지 않습니다.")
        print("     docs/판정정책_갱신제안.md를 참조하십시오.")
    agent.close()


def test_llm_optional():
    """--use-llm 을 켜도 서버가 없으면 규칙 기반으로 떨어지는지.

    rag/vllm_client.py 는 완성돼 있었는데 아무도 호출하지 않는 죽은 코드였고,
    그동안 "RAG 대응안"은 사실 검색 + 문장 템플릿이었습니다. 이제 연결했지만,
    **안전 파이프라인이 LLM 서버 때문에 멈추면 안 됩니다.** 서버가 없는 이
    환경에서 use_llm=True 로 돌려서 그 폴백이 실제로 도는지 확인합니다.
    """
    print("\n[6] LLM 생성 옵션 — 서버가 없어도 대응안이 나오는지")

    from integration.response_agent import RagResponseAgent

    event = DetectionEvent(
        zone="A", detected_object="화재", distance=2.0, box_position="unknown",
        risk_level="위험", issue="화재 감지",
    )

    off = RagResponseAgent(verbose=False)
    # 개발 PC에서 Ollama가 실행 중이어도 이 테스트의 의미가 바뀌지 않도록
    # 연결 불가 로컬 포트를 명시한다. 환경변수는 테스트 뒤 원상 복구한다.
    old_base_url = os.environ.get("LLM_BASE_URL")
    old_timeout = os.environ.get("LLM_TIMEOUT")
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:1/v1"
    os.environ["LLM_TIMEOUT"] = "0.2"
    on = RagResponseAgent(verbose=False, use_llm=True)
    try:
        plan_off = off.generate_response(event)
        plan_on = on.generate_response(event)
    finally:
        off.close()
        on.close()
        if old_base_url is None:
            os.environ.pop("LLM_BASE_URL", None)
        else:
            os.environ["LLM_BASE_URL"] = old_base_url
        if old_timeout is None:
            os.environ.pop("LLM_TIMEOUT", None)
        else:
            os.environ["LLM_TIMEOUT"] = old_timeout

    check("use_llm=True 여도 ResponsePlan 이 나옴", plan_on is not None)
    check("  권장 조치가 비어 있지 않음", bool(plan_on.recommended_action.strip()))
    check("  길이 제약을 지킴", len(plan_on.recommended_action) <= MAX_ACTION_LEN)
    check("서버가 없으면 규칙 기반과 같은 결과 (조용한 폴백)",
          plan_on.recommended_action == plan_off.recommended_action)
    check("확신도도 그대로 (근거 품질이 바뀐 게 아니므로)",
          plan_on.confidence == plan_off.confidence)


if __name__ == "__main__":
    print(BAR)
    print(" 대응안 생성기 단독 테스트 (DB·네트워크 불필요)")
    print(BAR)

    test_mapping()
    test_generate()
    test_distance_penalty()
    test_mock_compatibility()
    test_from_udp_payload()
    test_llm_optional()

    print("\n" + BAR)
    print(f" 통과 {passed} / 실패 {failed}")
    print(BAR)
    sys.exit(1 if failed else 0)
