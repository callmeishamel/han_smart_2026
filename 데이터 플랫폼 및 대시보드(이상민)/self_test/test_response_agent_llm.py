"""
LLM 생성 경로 방어 시험 (self_test/test_response_agent_llm.py)

DB 도 LLM 서버도 필요 없습니다. 가짜 LLM 클라이언트를 주입해 응답이
비정상일 때 대응안 생성이 어떻게 동작하는지 확인합니다.

    python3 self_test/test_response_agent_llm.py

확인 항목
    1. 길이 제한        LLM 이 500자를 넘겨도 잘라서 저장 가능한 길이로 만드는가
    2. latency_ms 없음  키가 없어도 예외 없이 끝나는가
    3. text 없음        본문이 없으면 규칙 기반으로 넘어가는가
    4. 형식 불일치      dict 가 아닌 값이 와도 규칙 기반으로 넘어가는가
    5. 호출 예외        클라이언트가 예외를 올려도 ResponsePlan 을 돌려주는가
    6. 줄바꿈 정리      여러 줄 응답을 한 줄로 펴는가
    7. 규칙 기반 경로   기존 동작이 그대로인가

배경
    response_plans.recommended_action 은 VARCHAR(500) 입니다. 규칙 기반
    문장만 자르고 LLM 문장은 그대로 두면, 생성이 길게 나온 순간
    INSERT 가 실패해 대응안이 통째로 유실됩니다.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.schema import DetectionEvent                        # noqa: E402
from integration.response_agent import (                        # noqa: E402
    MAX_ACTION_LEN, RagResponseAgent,
)

BAR = "=" * 70
_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    mark = "[OK]  " if ok else "[실패]"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))


SAMPLE = DetectionEvent(
    zone="A",
    detected_object="안전모 미착용",
    distance=1.1,
    box_position="x:120,y:80",
    risk_level="위험",
    issue="안전모 미착용 작업자 근접 (1.1m)",
)


class FakeLlm:
    """generate_action 만 흉내 낸다. is_available 은 항상 True."""

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises
        self.called = 0

    def is_available(self):
        return True

    def generate_action(self, event, chunks):
        self.called += 1
        if self._raises:
            raise RuntimeError("서버 연결 끊김 재현")
        return self._result


def make_agent(fake):
    """가짜 LLM 을 주입한 에이전트를 만든다.

    _get_llm() 은 self._llm 이 이미 있으면 그대로 쓰므로, 실제
    rag/vllm_client.py 를 import 하지 않는다.
    """
    agent = RagResponseAgent(use_llm=True, verbose=False)
    agent._llm = fake
    return agent


# ------------------------------------------------------------------------------

def test_length_limit():
    print("\n [1] 길이 제한 (LLM 이 600자 반환)")
    print(" " + "-" * 68)

    long_text = "가" * 600
    agent = make_agent(FakeLlm({"text": long_text, "latency_ms": 120}))
    plan = agent.generate_response(SAMPLE)
    agent.close()

    n = len(plan.recommended_action)
    check(f"{MAX_ACTION_LEN}자 이하로 잘림", n <= MAX_ACTION_LEN,
          f"{n}자 (원본 {len(long_text)}자)")
    check("LLM 문장이 채택됨", plan.recommended_action.startswith("가"),
          plan.recommended_action[:20] + "...")
    check("실제 LLM 채택 경로가 기록됨",
          plan.generation_mode.endswith("+llm"), plan.generation_mode)
    check("말줄임 표시", plan.recommended_action.endswith("..."),
          repr(plan.recommended_action[-6:]))


def test_missing_latency():
    print("\n [2] latency_ms 키 없음")
    print(" " + "-" * 68)

    agent = make_agent(FakeLlm({"text": "즉시 작업을 중지시키고 보호구 착용을 확인한다."}))
    try:
        plan = agent.generate_response(SAMPLE)
        raised = None
    except Exception as exc:                      # noqa: BLE001
        plan, raised = None, exc
    finally:
        agent.close()

    check("예외 없이 완료", raised is None,
          f"{type(raised).__name__}: {raised}" if raised else "")
    check("LLM 문장 채택", plan is not None and "즉시 작업을" in plan.recommended_action,
          plan.recommended_action[:30] if plan else "")


def test_missing_text():
    print("\n [3] text 키 없음 → 규칙 기반 폴백")
    print(" " + "-" * 68)

    agent = make_agent(FakeLlm({"latency_ms": 90}))
    plan = agent.generate_response(SAMPLE)
    agent.close()

    check("대응안이 반환됨", plan is not None)
    check("규칙 기반 문장", plan.recommended_action.startswith("[A구역]"),
          plan.recommended_action[:30])
    check("LLM 실패는 +llm으로 오표시하지 않음",
          not plan.generation_mode.endswith("+llm"), plan.generation_mode)
    check("길이 준수", len(plan.recommended_action) <= MAX_ACTION_LEN,
          f"{len(plan.recommended_action)}자")


def test_bad_type():
    print("\n [4] dict 가 아닌 응답 → 규칙 기반 폴백")
    print(" " + "-" * 68)

    agent = make_agent(FakeLlm("그냥 문자열"))
    plan = agent.generate_response(SAMPLE)
    agent.close()

    check("대응안이 반환됨", plan is not None)
    check("규칙 기반 문장", plan.recommended_action.startswith("[A구역]"),
          plan.recommended_action[:30])


def test_raises():
    print("\n [5] 클라이언트가 예외를 올림 → 규칙 기반 폴백")
    print(" " + "-" * 68)

    fake = FakeLlm(raises=True)
    agent = make_agent(fake)
    try:
        plan = agent.generate_response(SAMPLE)
        raised = None
    except Exception as exc:                      # noqa: BLE001
        plan, raised = None, exc
    finally:
        agent.close()

    check("예외가 밖으로 새지 않음", raised is None,
          f"{type(raised).__name__}" if raised else "")
    check("호출은 실제로 있었음", fake.called == 1, f"called={fake.called}")
    check("규칙 기반 문장", plan is not None
          and plan.recommended_action.startswith("[A구역]"),
          plan.recommended_action[:30] if plan else "")


def test_newlines():
    print("\n [6] 줄바꿈 정리")
    print(" " + "-" * 68)

    agent = make_agent(FakeLlm({
        "text": "1. 작업 중지\n\n2. 보호구 확인\n3.  관리감독자 보고",
        "latency_ms": 77,
    }))
    plan = agent.generate_response(SAMPLE)
    agent.close()

    a = plan.recommended_action
    check("줄바꿈 없음", "\n" not in a, repr(a[:40]))
    check("연속 공백 없음", "  " not in a, repr(a[:40]))
    check("내용 보존", "관리감독자 보고" in a, a[:50])


def test_rule_based():
    print("\n [7] 규칙 기반 경로 (LLM 미사용)")
    print(" " + "-" * 68)

    agent = RagResponseAgent(use_llm=False, verbose=False)
    plan = agent.generate_response(SAMPLE)
    agent.close()

    check("대응안이 반환됨", plan is not None)
    check("길이 준수", len(plan.recommended_action) <= MAX_ACTION_LEN,
          f"{len(plan.recommended_action)}자")
    check("확신도 반환", plan.confidence is not None, str(plan.confidence))
    check("규칙 기반 생성 경로 기록",
          plan.generation_mode in {"builtin", "keyword", "vector"},
          plan.generation_mode)


def main():
    print(BAR)
    print(" LLM 생성 경로 방어 시험")
    print(BAR)

    test_length_limit()
    test_missing_latency()
    test_missing_text()
    test_bad_type()
    test_raises()
    test_newlines()
    test_rule_based()

    total, passed = len(_results), sum(1 for r in _results if r)
    print()
    print(BAR)
    print(f" 결과: {passed}/{total} 통과")
    print(BAR)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
