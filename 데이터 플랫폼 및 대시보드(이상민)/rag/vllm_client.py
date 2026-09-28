"""
경량 LLM 호출 클라이언트 및 대응 지침 프롬프트 템플릿

역할
----
GEMMA 2B 등 경량 LLM을 OpenAI 호환 API로 호출해 대응 지침을 생성합니다.
vLLM과 Ollama 모두 같은 형식의 엔드포인트를 제공하므로, 아래 상수 두 개만
바꾸면 서버 종류에 관계없이 동작합니다.

이 모듈은 선택 사항입니다
--------------------------
서버에 연결할 수 없으면 예외를 던지지 않고 None을 반환합니다.
호출하는 쪽(integration/response_agent.py)이 규칙 기반 생성으로
넘어가도록 하기 위함입니다. 서버가 없어도 파이프라인은 멈추지 않습니다.

서버 구동
---------
    vLLM (Linux/WSL + GPU 필요. Windows 미지원)
        pip install vllm
        python3 -m vllm.entrypoints.openai.api_server \
            --model google/gemma-2-2b-it --port 8000 --max-model-len 4096

    Ollama (Windows 포함, CPU도 가능)
        ollama pull gemma2:2b
        ollama serve
        -> BASE_URL을 http://localhost:11434/v1 로, MODEL을 gemma2:2b 로 변경

사용법
------
    python3 rag/vllm_client.py          서버 연결 및 생성 확인

프롬프트 설계 원칙
------------------
1. 근거 밖의 내용을 생성하지 않도록 명시적으로 제약합니다.
2. 검색된 근거에 번호를 붙여 어느 근거가 반영되었는지 대조할 수 있게 합니다.
3. 출력 길이를 제한합니다. response_plans.recommended_action이
   VARCHAR(500)이기 때문입니다.
4. 온도를 0으로 두어 같은 입력에 같은 출력이 나오게 합니다.
   안전 지침이 매번 달라지면 신뢰할 수 없기 때문입니다.
"""

import json
import os
import time
from typing import Dict, List, Optional

try:
    import requests
except ImportError:  # requests가 없어도 import 자체는 실패하지 않게 합니다
    requests = None


# ==============================================================================
# 서버 설정
# ==============================================================================
# integration/response_agent.py 와 같은 환경변수를 읽습니다. 한쪽만 바꿔서
# "단독 실행은 되는데 파이프라인에서는 다른 서버를 본다"가 되지 않도록.
#   Ollama 로 바꾸려면:
#     LLM_BASE_URL=http://localhost:11434/v1   LLM_MODEL=gemma2:2b
BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
MODEL = os.environ.get("LLM_MODEL", "google/gemma-2-2b-it")

try:
    REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
except ValueError:
    REQUEST_TIMEOUT = 60.0
MAX_NEW_TOKENS = 300
TEMPERATURE = 0.0           # 결정적 출력

MAX_ACTION_LEN = 500        # response_plans.recommended_action 컬럼 제약

# 실내 공장 기준 거리 신뢰 상한.
# 시차 계산 실패 시 300m 부근 값이 들어오므로 수치를 그대로 쓰지 않습니다.
MAX_VALID_DISTANCE_M = 15.0

BAR = "=" * 70


# ==============================================================================
# 프롬프트 템플릿
# ==============================================================================
SYSTEM_PROMPT = """당신은 산업안전 관리자를 보조하는 시스템입니다.
이 응답은 대시보드에서 관리자가 읽는 상세 대응 지침입니다. 현장 방송용
짧은 경보 문구는 다른 규칙 기반 경로에서 별도로 만들어지므로 작성하지 마십시오.
제공된 매뉴얼 근거에 있는 내용만 사용하여 대응 지침을 작성하십시오.

작성 규칙
1. 근거에 없는 조치를 추가하지 마십시오.
2. 즉시 수행할 순서대로 서술하십시오.
3. 300자 이내로 작성하십시오.
4. 관리자가 바로 판단·지시할 수 있게 조치 순서와 확인 대상을 분명히 쓰십시오.
5. 인사말이나 부연 설명 없이 대응 지침만 출력하십시오.
6. 제목 기호, 굵은 글씨 기호, 괄호 등 마크다운 서식을 쓰지 마십시오.
7. 조치는 1. 2. 3. 형식의 짧은 일반 문장으로 정리하십시오."""


USER_PROMPT_TEMPLATE = """[감지 상황]
구역: {zone}
감지 객체: {detected_object}
위험도: {risk_level}
거리: {distance}
판정 사유: {issue}

[매뉴얼 근거]
{context}

[대응 지침]"""


def format_distance(distance: Optional[float]) -> str:
    """거리값을 사람이 읽을 수 있는 형태로 변환합니다.

    측정 실패값(-1.0 또는 비정상적으로 큰 값)은 수치 대신 상태로 표기합니다.
    프롬프트에 300m 같은 값이 그대로 들어가면 모델이 안전한 상황으로
    잘못 해석할 수 있기 때문입니다.
    """
    if distance is None:
        return "측정값 없음"
    if distance < 0 or distance > MAX_VALID_DISTANCE_M:
        return "측정 실패 (현장 확인 필요)"
    return f"{distance}m"


def format_context(chunks: List[dict]) -> str:
    """검색된 근거를 프롬프트에 삽입할 형태로 변환합니다.

    각 근거에 번호를 붙여, 생성 결과를 검토할 때 어느 근거가 반영되었는지
    대조할 수 있게 합니다.
    """
    if not chunks:
        return "(검색된 근거 없음)"

    lines = []
    for i, c in enumerate(chunks, 1):
        source = f"{c.get('title', '출처 미상')} {c.get('section', '')}".strip()
        lines.append(f"{i}. {c['content'].strip()}")
        lines.append(f"   (출처: {source})")
    return "\n".join(lines)


def build_messages(event: dict, chunks: List[dict]) -> List[Dict[str, str]]:
    """감지 이벤트와 검색 근거를 대화 메시지 형식으로 조립합니다."""
    user_content = USER_PROMPT_TEMPLATE.format(
        zone=event.get("zone", "미지정"),
        detected_object=event.get("detected_object", "미상"),
        risk_level=event.get("risk_level", "미상"),
        distance=format_distance(event.get("distance")),
        issue=event.get("issue") or "(사유 없음)",
        context=format_context(chunks),
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ==============================================================================
# 서버 호출
# ==============================================================================
class LlmClient:
    """OpenAI 호환 LLM 서버 클라이언트.

    서버를 사용할 수 없는 상황에서도 예외를 올리지 않고 None을 반환합니다.
    호출하는 쪽이 규칙 기반 생성으로 넘어갈 수 있게 하기 위함입니다.
    """

    def __init__(self, base_url: str = BASE_URL, model: str = MODEL,
                 timeout: float = REQUEST_TIMEOUT, verbose: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.verbose = verbose
        self._available: Optional[bool] = None

    def _log(self, msg: str):
        if self.verbose:
            print(f"[LlmClient] {msg}")

    def is_available(self, force: bool = False) -> bool:
        """서버 연결 가능 여부를 확인합니다. 결과는 1회 캐시합니다."""
        if requests is None:
            self._log("requests 미설치 — 서버 호출을 사용할 수 없습니다")
            return False
        if self._available is not None and not force:
            return self._available
        try:
            res = requests.get(f"{self.base_url}/models", timeout=5)
            self._available = res.status_code == 200
            if self._available:
                models = [m.get("id") for m in res.json().get("data", [])]
                self._log(f"서버 연결 확인. 로딩된 모델: {models}")
        except Exception as exc:
            self._log(f"서버 연결 실패: {exc}")
            self._available = False
        return self._available

    def generate(self, messages: List[Dict[str, str]],
                 max_tokens: int = MAX_NEW_TOKENS) -> Optional[dict]:
        """대응 지침을 생성합니다.

        Returns
        -------
        dict or None
            성공 시 text, latency_ms, prompt_tokens, completion_tokens.
            실패 시 None.
        """
        if not self.is_available():
            return None

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
        }

        t0 = time.time()
        try:
            res = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=self.timeout,
            )
            res.raise_for_status()
        except Exception as exc:
            self._log(f"생성 요청 실패: {exc}")
            return None

        latency_ms = int((time.time() - t0) * 1000)
        body = res.json()

        try:
            text = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            self._log(f"응답 형식이 예상과 다릅니다: {body}")
            return None

        usage = body.get("usage", {})
        return {
            "text": text,
            "latency_ms": latency_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }

    def generate_action(self, event: dict,
                        chunks: List[dict]) -> Optional[dict]:
        """감지 이벤트에 대한 대응 지침을 생성합니다.

        저장 컬럼 제약에 맞춰 결과 길이를 조정합니다.
        """
        result = self.generate(build_messages(event, chunks))
        if result is None:
            return None

        text = result["text"]
        if len(text) > MAX_ACTION_LEN:
            text = text[:MAX_ACTION_LEN - 3].rstrip() + "..."
            self._log(f"생성 결과가 길어 {MAX_ACTION_LEN}자로 잘랐습니다")
        result["text"] = text
        return result


# ==============================================================================
# 단독 실행 — 서버 연결 및 생성 확인
# ==============================================================================
SAMPLE_EVENT = {
    "zone": "A",
    "detected_object": "안전모 미착용",
    "risk_level": "위험",
    "distance": 1.1,
    "issue": "안전모 미착용 작업자 근접 (1.1m)",
}

SAMPLE_CHUNKS = [
    {"chunk_id": 1, "title": "산업안전보건기준에 관한 규칙 발췌",
     "section": "보호구-1",
     "content": "작업자는 작업 성격에 맞는 보호구를 착용하여야 하며, "
                "미착용이 확인된 경우 작업을 중지시킨다."},
    {"chunk_id": 2, "title": "산업안전보건기준에 관한 규칙 발췌",
     "section": "보호구-2",
     "content": "보호구 미착용 작업자를 발견한 경우 관리감독자에게 보고하고 "
                "착용을 확인한 후 작업을 재개한다."},
    {"chunk_id": 3, "title": "산업안전보건기준에 관한 규칙 발췌",
     "section": "보호구-3",
     "content": "머리 보호가 필요한 작업 구역에서는 안전모 착용 상태를 "
                "상시 확인하여야 한다."},
]


def main():
    print(BAR)
    print(" LLM 서버 연결 및 대응 지침 생성 확인")
    print(BAR)
    print(f" 서버: {BASE_URL}")
    print(f" 모델: {MODEL}")
    print()

    client = LlmClient(verbose=True)

    if not client.is_available():
        print(" 서버에 연결할 수 없습니다.")
        print(" 서버를 구동하지 않아도 파이프라인은 규칙 기반 생성으로 동작합니다.")
        print()
        print(" 프롬프트 구성만 확인합니다.")
        print(BAR)
        for m in build_messages(SAMPLE_EVENT, SAMPLE_CHUNKS):
            print(f"\n--- {m['role']} ---")
            print(m["content"])
        print(BAR)
        return

    print(" 연결 정상")
    print()
    print(" 프롬프트")
    for m in build_messages(SAMPLE_EVENT, SAMPLE_CHUNKS):
        print(f"\n--- {m['role']} ---")
        print(m["content"])

    print()
    print(" 생성 결과")
    result = client.generate_action(SAMPLE_EVENT, SAMPLE_CHUNKS)
    if result is None:
        print(" 생성에 실패했습니다.")
        return

    print(BAR)
    print(result["text"])
    print(BAR)
    print(f" 소요 {result['latency_ms']} ms | "
          f"입력 {result['prompt_tokens']} 토큰 | "
          f"출력 {result['completion_tokens']} 토큰")
    print(f" 길이 {len(result['text'])} / {MAX_ACTION_LEN}자")
    print(BAR)


if __name__ == "__main__":
    main()
