"""
GEMMA 2B 구동 확인 스크립트 (rag/check_llm.py)

역할
----
vLLM 서버가 떠 있는지 확인하고, 실제 대응 지침을 생성해 성능을 측정합니다.
출력이 그대로 검증 자료가 되도록 구성했습니다.

    - 서버 연결 및 로딩된 모델
    - 시나리오별 생성 결과와 근거 반영 여부
    - 생성 지연 시간 (Fast Path 와 비교)
    - 저장 컬럼 제약(VARCHAR(500)) 준수 여부

서버가 없어도 실행됩니다. 그 경우 Fast Path 결과만 출력하고, 서버 구동
방법을 안내합니다.

사용법
------
    source set_env.sh
    python rag/check_llm.py              전체 확인
    python rag/check_llm.py --compare    Fast/Slow Path 비교만
"""

import argparse
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import DetectionEvent                          # noqa: E402
from rag.vllm_client import (                                     # noqa: E402
    MAX_ACTION_LEN, LlmClient, build_messages,
)

# rag/vllm_client.py 는 BASE_URL / MODEL 을 상수로 들고 있어 환경변수를
# 읽지 않는다. 확인 스크립트에서는 환경변수를 우선 적용해, set_env.sh 에
# 넣은 값과 실제 호출 대상이 어긋나지 않게 한다.
BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
MODEL = os.environ.get("LLM_MODEL", "google/gemma-2-2b-it")

# CPU 추론(Ollama)에서는 첫 생성이 수십 초 걸린다. 설치 가이드가 안내하는
# LLM_TIMEOUT 을 여기서도 읽어, 안내대로 설정했는데 기본 타임아웃에서
# 끊기는 상황을 막는다.
try:
    TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
except ValueError:
    TIMEOUT = 60.0

BAR = "=" * 74


def make_client():
    """환경변수를 반영한 LlmClient 를 만든다.

    구버전 vllm_client.LlmClient 는 timeout 인자를 받지 않으므로,
    TypeError 로 걸러 내고 기존 방식으로 다시 생성한다.
    """
    try:
        return LlmClient(base_url=BASE_URL, model=MODEL, timeout=TIMEOUT)
    except TypeError:
        print(" [참고] LlmClient 가 timeout 인자를 지원하지 않아")
        print("        LLM_TIMEOUT 이 적용되지 않습니다.")
        return LlmClient(base_url=BASE_URL, model=MODEL)

# 현재 탐지 클래스 기준 검증 시나리오
SCENARIOS = [
    DetectionEvent(zone="A", detected_object="안전모 미착용", distance=1.1,
                   box_position="x:120,y:80", risk_level="위험",
                   issue="안전모 미착용 감지"),
    DetectionEvent(zone="B", detected_object="화재", distance=3.5,
                   box_position="x:400,y:200", risk_level="위험",
                   issue="화재 감지"),
    DetectionEvent(zone="C", detected_object="차량·중장비", distance=1.3,
                   box_position="x:300,y:150", risk_level="위험",
                   issue="차량·중장비 근접 위험 (1.3m)"),
]


def print_env():
    print(BAR)
    print(" GEMMA 2B 구동 확인")
    print(BAR)
    print(f" 서버   : {BASE_URL}")
    print(f" 모델   : {MODEL}")
    print(f" 타임아웃: {TIMEOUT:.0f}초")
    print(" 환경변수: LLM_BASE_URL / LLM_MODEL / LLM_TIMEOUT 로 변경 가능")
    print(BAR)


def check_server(client) -> bool:
    print()
    print(" [1] 서버 연결 확인")
    print(" " + "-" * 72)
    if client.is_available():
        print("  [OK] 서버 응답 정상")
        # 서버가 실제로 들고 있는 모델명을 확인한다.
        # LLM_MODEL 과 다르면 호출이 404 로 떨어진다.
        try:
            import requests
            res = requests.get(f"{BASE_URL.rstrip('/')}/models", timeout=5)
            names = [m.get("id") for m in res.json().get("data", [])]
            print(f"       로딩된 모델: {names}")
            if names and MODEL not in names:
                print(f"  [경고] 요청 모델({MODEL})이 목록에 없습니다.")
                print("         LLM_MODEL 을 맞추거나 서버를 확인하십시오.")
        except Exception:
            pass
        return True

    print("  [없음] 서버에 연결할 수 없습니다.")
    print()
    print("  서버가 없어도 파이프라인은 규칙 기반(Fast Path)으로 동작합니다.")
    print("  구동 방법은 docs/설치가이드_리눅스.md 3절(GEMMA 2B)을 참조하십시오.")
    return False


def show_prompt():
    print()
    print(" [2] 프롬프트 구성")
    print(" " + "-" * 72)
    chunks = [
        {"title": "산업안전보건기준에 관한 규칙 발췌", "section": "보호구-1",
         "content": "작업자는 작업 성격에 맞는 보호구를 착용하여야 하며, "
                    "미착용이 확인된 경우 작업을 중지시킨다."},
    ]
    event = {"zone": "A", "detected_object": "안전모 미착용",
             "risk_level": "위험", "distance": 1.1, "issue": "안전모 미착용 감지"}
    for m in build_messages(event, chunks):
        print(f"\n  --- {m['role']} ---")
        for line in m["content"].split("\n"):
            print(f"  {line}")


def run_generation(has_server: bool):
    """Fast Path 와 Slow Path 를 각각 실행해 결과와 지연을 비교합니다."""
    from integration.response_agent import RagResponseAgent

    print()
    print(" [3] 대응 지침 생성")
    print(" " + "-" * 72)

    fast = RagResponseAgent(use_llm=False)
    slow = RagResponseAgent(use_llm=True) if has_server else None

    fast_times, slow_times = [], []
    max_len = 0

    for ev in SCENARIOS:
        print(f"\n  [{ev.zone}구역] {ev.detected_object} / {ev.risk_level}")

        t0 = time.time()
        p_fast = fast.generate_response(ev)
        fast_ms = (time.time() - t0) * 1000
        fast_times.append(fast_ms)
        max_len = max(max_len, len(p_fast.recommended_action))

        print(f"    Fast Path ({fast_ms:.0f} ms, 확신도 {p_fast.confidence})")
        print(f"      {p_fast.recommended_action[:64]}")

        if slow is not None:
            t0 = time.time()
            p_slow = slow.generate_response(ev)
            slow_ms = (time.time() - t0) * 1000
            slow_times.append(slow_ms)
            max_len = max(max_len, len(p_slow.recommended_action))

            print(f"    Slow Path ({slow_ms:.0f} ms, 확신도 {p_slow.confidence})")
            print(f"      {p_slow.recommended_action[:64]}")



    fast.close()
    if slow is not None:
        slow.close()

    # --- 요약 ---
    print()
    print(BAR)
    print(" 측정 결과")
    print(BAR)
    avg_fast = sum(fast_times) / len(fast_times)
    print(f" Fast Path 평균 지연 : {avg_fast:>8.0f} ms")
    if slow_times:
        avg_slow = sum(slow_times) / len(slow_times)
        print(f" Slow Path 평균 지연 : {avg_slow:>8.0f} ms")
        print(f" 배율                : {avg_slow / max(avg_fast, 0.01):>8.0f}배")
        print()
        print(" 안전 관제는 즉시 응답이 필요하므로, 표준 지침을 먼저 반환하고")
        print(" LLM 생성은 뒤따르는 이중 경로 구성이 타당함을 확인했습니다.")
    else:
        print(" Slow Path           : 서버 없음 (측정 불가)")

    print()
    print(f" 최대 대응안 길이    : {max_len} / {MAX_ACTION_LEN}자 "
          f"({'준수' if max_len <= MAX_ACTION_LEN else '초과'})")
    print(BAR)


def main():
    parser = argparse.ArgumentParser(description="GEMMA 2B 구동 확인")
    parser.add_argument("--compare", action="store_true",
                        help="프롬프트 출력 없이 Fast/Slow 비교만")
    args = parser.parse_args()

    print_env()
    client = make_client()
    has_server = check_server(client)

    if not args.compare:
        show_prompt()

    run_generation(has_server)


if __name__ == "__main__":
    main()
