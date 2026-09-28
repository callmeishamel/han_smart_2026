"""
================================================================================
파일명   : integration/response_agent.py
목적     : 위험 이벤트에 대한 대응 지침 생성 (RAG 기반)
버전     : v1.0
담당     : 대응안 생성 파트
--------------------------------------------------------------------------------
역할
  오케스트레이터가 risk_level == '위험'인 DetectionEvent를 전달하면,
  산업안전 지식베이스에서 근거를 검색해 대응 지침을 생성하고
  ResponsePlan으로 반환한다.

인터페이스
  team_integration_adapter.py의 ResponseAgentAdapter Protocol을 만족한다.

      generate_response(self, event: DetectionEvent) -> ResponsePlan

연결 방법
      from integration.response_agent import RagResponseAgent

      orchestrator = PatrolIntegrationOrchestrator(
          vision_adapter=vision_adapter,
          data_platform=_build_data_platform(),
          response_agent=RagResponseAgent(),
      )

  기존 _MockResponseAgent와 입출력 형태가 동일하므로 다른 코드는 수정하지
  않아도 된다.

동작 경로
  환경에 따라 세 경로 중 하나로 동작하며, 어떤 상황에서도 ResponsePlan을
  반환한다. DB나 모델이 준비되지 않아도 예외를 올리지 않는다.

      keyword  지식베이스에서 위험유형으로 근거 조회      confidence 0.60
      vector   지식베이스 + 임베딩 모델로 유사도 검색     confidence 0.70~0.95
      builtin  지식베이스 없이 내장 표준 지침만 사용      confidence 0.40

  기본값은 keyword 경로다. 엣지 보드에서 임베딩 모델을 상주시키면 비전
  추론과 메모리를 다투므로, 기본 구성에서는 사용하지 않는다.
  성능 여유가 있는 환경에서는 use_embedding=True로 전환할 수 있다.

주의
  recommended_action은 response_plans 테이블의 VARCHAR(500) 제약에 맞춰
  자동으로 잘린다. 컬럼 타입이 변경되면 MAX_ACTION_LEN만 조정하면 된다.

스레드 사용 규약
  이 클래스는 스레드 안전하지 않다. 내부에 psycopg2 커넥션과 임베딩 모델을
  들고 있으며 둘 다 잠금 없이 접근한다. 비동기 구성에서 사용할 때는
  인스턴스 하나를 한 스레드에서만 호출해야 한다.
  integration/response_plan_worker.py가 이 규약을 지키는 형태로 되어 있다.
================================================================================
"""

from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional, Tuple

# 프로젝트 루트를 import 경로에 추가한다.
# 이 파일이 integration/ 아래에 있으므로 상위 디렉터리가 루트가 된다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import DetectionEvent                      # noqa: E402
from integration.team_integration_adapter import ResponsePlan  # noqa: E402


# ==============================================================================
# 설정 상수
# ==============================================================================

# response_plans.recommended_action 컬럼 길이 제약.
# 컬럼 타입을 TEXT로 변경하면 이 값을 올리면 된다.
MAX_ACTION_LEN = 500

# 검색할 근거 청크 수
TOP_K = 3

# 유사도 검색 시 이 값 미만은 근거로 채택하지 않는다.
# 관련 없는 문서가 근거로 붙는 것을 막기 위한 하한선이다.
MIN_SIMILARITY = 0.35

# 임베딩 모델. use_embedding=True인 경우에만 로딩된다.
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024

# 지식베이스 확인에 실패했을 때 다시 시도하기까지의 간격(초).
# 실패를 영구 캐싱하면 DB가 복구되어도 내장 지침으로만 동작하고, 반대로
# 매 이벤트마다 재시도하면 DB 장애 중 연결을 과도하게 시도하므로 간격을 둔다.
KB_RETRY_INTERVAL_SEC = float(os.environ.get("KB_RETRY_INTERVAL_SEC", "60"))


# ==============================================================================
# 감지 객체명 → 위험유형 매핑
#
#   DetectionEvent.detected_object는 공통 스키마에서 한글로 변환되어 전달되나,
#   매핑에 없는 클래스는 원문 문자열이 그대로 들어온다.
#   따라서 한글명과 원문 문자열을 모두 키로 등록한다.
#
#   비전 모듈의 탐지 클래스가 변경되면 이 표만 갱신하면 된다.
# ==============================================================================
RISK_TYPE_MAP: Dict[str, str] = {
    # --- 현재 탐지 클래스 ---
    "person with no helmet": "PPE",
    "안전모 미착용":          "PPE",
    "fire":                  "fire",
    "화재":                   "fire",
    "vehicle":               "machinery",
    "차량":                   "machinery",
    "차량·중장비":            "machinery",

    # --- 이전 탐지 클래스 (과거 로그 조회 대응) ---
    "hazardous leak":        "chemical",
    "유해물질 누출":          "chemical",
    "obstacle":              "machinery",
    "장애물":                 "machinery",
    "human":                 "access",
    "person":                "access",
    "작업자":                 "access",
}


def _truncate(text: str, limit: int = MAX_ACTION_LEN) -> str:
    """대응안 문장을 정리하고 DB 컬럼 길이에 맞춘다."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + "..."


def resolve_risk_type(detected_object: str) -> str:
    """감지 객체명에서 위험유형을 결정한다.

    매핑에 없으면 'general'을 반환한다. 이 경우에도 대응안은 생성되며,
    관리자가 현장을 확인하도록 안내하는 내용이 된다.
    """
    if detected_object in RISK_TYPE_MAP:
        return RISK_TYPE_MAP[detected_object]

    # 대소문자나 앞뒤 공백 차이로 매핑이 실패하는 것을 방지한다
    lowered = (detected_object or "").lower().strip()
    for key, value in RISK_TYPE_MAP.items():
        if key.lower() == lowered:
            return value
    return "general"


# ==============================================================================
# 내장 표준 지침
#
#   지식베이스에 접근할 수 없을 때 사용하는 폴백 경로다.
#   DB가 준비되지 않은 환경에서도 대응안이 반환되므로,
#   통합 테스트가 환경 문제로 중단되지 않는다.
#
#   각 항목은 (대응 절차, 근거 조항 목록) 형태다.
# ==============================================================================
BUILTIN_GUIDELINES: Dict[str, Tuple[str, List[str]]] = {
    "PPE": (
        "해당 작업자의 작업을 즉시 중지시키고 안전모 착용을 확인한 후 재개한다. "
        "관리감독자에게 보고하고, 미착용이 반복되면 작업허가를 취소한다. "
        "구역 내 다른 작업자의 보호구 착용 상태도 함께 점검한다.",
        ["산업안전보건기준에 관한 규칙 보호구 관련 조항"],
    ),
    "fire": (
        "해당 구역에 경보를 발령하고 작업자를 대피시킨다. 소방서에 신고한 후 "
        "초기 소화가 가능한 규모인지 판단한다. 확산 우려가 있으면 소화보다 "
        "대피를 우선하고, 해당 구역 전원을 차단해 추가 확산을 막는다.",
        ["산업안전보건기준에 관한 규칙 화재예방 관련 조항"],
    ),
    "chemical": (
        "누출 구역의 출입을 통제하고 작업자를 대피시킨다. 강제 환기를 가동한 후 "
        "보호구를 착용한 인원이 누출원을 차단한다. 물질안전보건자료를 확인해 "
        "해당 물질의 성상에 맞는 조치를 수행한다.",
        ["산업안전보건기준에 관한 규칙 관리대상 유해물질 관련 조항"],
    ),
    "machinery": (
        "차량 운행 구역의 보행자 출입을 통제하고 안전거리를 확보한다. "
        "유도자를 배치해 접촉을 방지하고, 후진 시 경보 장치 작동을 확인한다. "
        "적재물이 있는 경우 붕괴 위험을 함께 점검한다.",
        ["산업안전보건기준에 관한 규칙 차량계 하역운반기계 관련 조항"],
    ),
    "access": (
        "해당 작업자에게 경고를 전달하고 위험 설비의 가동 상태를 확인한다. "
        "안전거리 확보를 유도하고 관리감독자에게 통보한다.",
        ["산업안전보건기준에 관한 규칙 위험기계 접근 제한 관련 조항"],
    ),
    "general": (
        "관리자가 현장 상황을 직접 확인하고 위험 요인을 판단한다. "
        "판단 전까지 해당 구역 작업을 보류한다.",
        ["표준 대응 지침 미등록 — 감지 클래스 확인 필요"],
    ),
}


# ==============================================================================
# 에이전트 본체
# ==============================================================================

class RagResponseAgent:
    """산업안전 지식베이스 기반 대응안 생성 에이전트.

    Parameters
    ----------
    db_config : dict, optional
        지식베이스가 있는 PostgreSQL 접속 정보.
        None이면 rag.kb_config.get_kb_db_config()를 사용한다. RAG_KB_*
        환경변수가 없으면 운영 DB 설정을 그대로 사용한다.
        연결에 실패해도 예외를 던지지 않고 내장 지침으로 동작한다.
    use_embedding : bool, default False
        벡터 유사도 검색 사용 여부.
        엣지 보드에서는 임베딩 모델이 비전 추론과 메모리를 다투므로
        기본값을 False로 둔다. 위험유형 기준 조회만으로도 동작한다.
    use_llm : bool, default False
        GEMMA 2B로 대응 지침 문장을 생성할지 여부. 서버가 없거나 생성에
        실패하면 규칙 기반 문장으로 자동 대체한다.
    verbose : bool, default False
        생성 과정을 콘솔에 출력할지 여부. 통합 테스트 시 True로 두면
        어느 경로로 생성되었는지 확인할 수 있다.
    """

    def __init__(self, db_config: Optional[dict] = None,
                 use_embedding: bool = False, verbose: bool = False,
                 use_llm: bool = False):
        self._db_config = db_config
        self._use_embedding = use_embedding
        self._use_llm = use_llm
        self._verbose = verbose

        self._conn = None
        self._embedder = None
        self._llm = None
        self._kb_ready: Optional[bool] = None   # None이면 아직 확인 전
        self._kb_checked_at = 0.0               # 마지막 확인 시각 (monotonic)

    # --------------------------------------------------------------------------
    # 내부 유틸
    # --------------------------------------------------------------------------

    def _log(self, msg: str):
        if self._verbose:
            print(f"[RagResponseAgent] {msg}")

    def _get_conn(self):
        """지식베이스 연결을 확보한다.

        연결 실패는 정상 동작 범위로 취급하며 예외를 올리지 않는다.
        상위 호출부가 대응안 생성 자체를 실패로 처리하지 않도록 하기 위함이다.
        """
        if self._conn is not None and not self._conn.closed:
            return self._conn
        try:
            import psycopg2
            from rag.kb_config import describe, get_kb_db_config
            cfg = self._db_config or get_kb_db_config()
            self._conn = psycopg2.connect(**cfg)
            # 이 연결은 지식베이스 SELECT만 사용한다. psycopg2 기본값은
            # SELECT도 트랜잭션을 열어 두므로, 장시간 파이프라인에서
            # safety_chunks DDL·임베딩 적재를 막는 idle in transaction이 된다.
            # autocommit으로 읽기 트랜잭션을 즉시 종료해 테이블 잠금을
            # 남기지 않도록 한다.
            self._conn.autocommit = True
            self._log(f"지식베이스 연결 성공 — {describe()}")
            return self._conn
        except Exception as exc:
            self._log(f"지식베이스 연결 실패 — 내장 지침 사용 ({exc})")
            self._conn = None
            return None

    def _kb_available(self) -> bool:
        """safety_chunks 테이블 존재 여부를 확인한다.

        성공(True)은 영구 캐시하지만 **실패(False)는 KB_RETRY_INTERVAL_SEC 뒤에
        다시 확인한다.** 예전에는 성공/실패를 똑같이 1회만 캐시했는데, 이
        파이프라인은 보통 PostgreSQL보다 먼저 뜨기 때문에 첫 확인이 거의 항상
        실패했다. 그러면 DB가 정상화된 뒤에도 프로세스가 살아 있는 내내
        builtin(확신도 0.4) 폴백으로만 동작했다 — 대시보드가 확신도를 화면에
        표시하게 되면서, 모든 대응안이 40%로 찍히는 형태로 드러난다.

        대시보드 v3가 `init_db()`에서 고쳤던 "실패 결과를 캐싱해서 영구히
        실패 상태로 남는" 버그와 같은 것이다.
        """
        if self._kb_ready:
            return True
        if (self._kb_ready is False
                and time.monotonic() - self._kb_checked_at < KB_RETRY_INTERVAL_SEC):
            return False

        self._kb_checked_at = time.monotonic()

        conn = self._get_conn()
        if conn is None:
            self._kb_ready = False
            return False

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'safety_chunks'
                    )
                """)
                self._kb_ready = bool(cur.fetchone()[0])
                self._log(f"지식베이스 사용 가능: {self._kb_ready}")
        except Exception as exc:
            self._log(f"지식베이스 확인 실패 ({exc})")
            self._kb_ready = False
            # 끊긴 커넥션을 버려 다음 재시도에서는 새 연결을 확보한다.
            try:
                if self._conn is not None and not self._conn.closed:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
        return self._kb_ready

    def _get_embedder(self):
        """임베딩 모델을 지연 로딩한다. 실패하면 None을 반환한다."""
        if self._embedder is not None:
            return self._embedder
        if not self._use_embedding:
            return None
        try:
            from sentence_transformers import SentenceTransformer
            t0 = time.time()
            self._embedder = SentenceTransformer(EMBED_MODEL)
            self._log(f"임베딩 모델 로딩 완료 ({time.time() - t0:.1f}s)")
            return self._embedder
        except Exception as exc:
            self._log(f"임베딩 모델 로딩 실패 — 위험유형 조회로 대체 ({exc})")
            self._use_embedding = False
            return None

    # --------------------------------------------------------------------------
    # 근거 검색
    # --------------------------------------------------------------------------

    def _search_vector(self, query: str, risk_type: str) -> List[tuple]:
        """벡터 유사도 검색.

        Returns
        -------
        list of tuple
            (chunk_id, 본문, 문서제목, 조항라벨, 유사도)
        """
        embedder = self._get_embedder()
        conn = self._get_conn()
        if embedder is None or conn is None:
            return []
        try:
            from pgvector.psycopg2 import register_vector
            register_vector(conn)
            qvec = embedder.encode([query], normalize_embeddings=True)[0]
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.chunk_id,
                           c.content_snippet,
                           COALESCE(d.title, '출처 미상'),
                           COALESCE(c.section_label, ''),
                           1 - (c.embedding <=> %s) AS similarity
                    FROM   safety_chunks c
                    LEFT   JOIN safety_documents d ON d.doc_id = c.doc_id
                    WHERE  c.embedding IS NOT NULL
                      AND  (%s = 'general' OR c.risk_type = %s)
                    ORDER  BY c.embedding <=> %s
                    LIMIT  %s
                """, (qvec, risk_type, risk_type, qvec, TOP_K))
                rows = cur.fetchall()
            # 유사도가 낮은 근거는 오히려 방해가 되므로 제외한다
            return [r for r in rows if r[4] >= MIN_SIMILARITY]
        except Exception as exc:
            self._log(f"벡터 검색 실패 ({exc})")
            return []

    def _search_keyword(self, risk_type: str) -> List[tuple]:
        """위험유형 기준 근거 조회.

        임베딩 없이 동작하며, 엣지 환경의 기본 경로다.
        같은 위험유형의 청크는 모두 해당 상황에 적용 가능한 절차이므로,
        유사도 없이도 근거로서 유효하다.
        """
        conn = self._get_conn()
        if conn is None:
            return []
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.chunk_id,
                           c.content_snippet,
                           COALESCE(d.title, '출처 미상'),
                           COALESCE(c.section_label, ''),
                           NULL::real
                    FROM   safety_chunks c
                    LEFT   JOIN safety_documents d ON d.doc_id = c.doc_id
                    WHERE  c.risk_type = %s
                    ORDER  BY c.chunk_id
                    LIMIT  %s
                """, (risk_type, TOP_K))
                return cur.fetchall()
        except Exception as exc:
            self._log(f"위험유형 조회 실패 ({exc})")
            return []

    # --------------------------------------------------------------------------
    # 대응안 구성
    # --------------------------------------------------------------------------

    @staticmethod
    def _build_query(event: DetectionEvent) -> str:
        """이벤트를 검색 질의 문장으로 변환한다.

        issue 필드에 이미 사람이 읽을 수 있는 판정 사유가 들어 있으므로
        그대로 활용한다.
        """
        parts = [f"{event.zone}구역에서 {event.detected_object} 감지"]
        if event.issue:
            parts.append(event.issue)
        if event.distance is not None and 0 <= event.distance <= 15.0:
            parts.append(f"거리 {event.distance}m")
        else:
            parts.append("거리 측정 실패")
        return ", ".join(parts)

    @staticmethod
    def _compose(event: DetectionEvent, base_action: str,
                 chunks: List[tuple]) -> str:
        """표준 지침과 검색 근거를 하나의 대응 지침으로 결합한다.

        생성 모델을 사용하지 않고 규칙 기반으로 구성한다.
        근거 문장을 그대로 인용하므로 매뉴얼에 없는 내용이 섞이지 않는다.
        """
        head = f"[{event.zone}구역] {event.detected_object} {event.risk_level}. "
        if event.issue:
            head += f"{event.issue}. "

        body = base_action
        if chunks:
            # 첫 번째 근거를 인용해 판단 근거를 명시한다
            body += f" 근거: {chunks[0][1].strip()}"

        return _truncate(head + body)

    def _generate_with_llm(self, event: DetectionEvent, chunks: List[tuple]):
        """rag/vllm_client.py 로 대응 지침을 생성한다. 실패하면 None.

        이 모듈은 완성돼 있었는데 파이프라인 어디서도 호출되지 않아 죽은 코드로
        남아 있었습니다(vllm_client.py 상단 주석이 "호출하는 쪽
        (integration/response_agent.py)"이라고 이미 명시하고 있었습니다).
        그동안 "RAG 대응안"은 사실 검색 + 문장 템플릿이었고, 생성 단계가 없었습니다.

        서버가 없으면 None 을 돌려주고 호출부가 규칙 기반으로 넘어갑니다.
        기본값이 꺼짐(use_llm=False)이라, 켜지 않으면 동작이 예전과 같습니다.
        """
        client = self._get_llm()
        if client is None:
            return None

        # chunks 는 (chunk_id, text, title, label, similarity) 튜플입니다.
        # vllm_client 는 dict 를 기대하므로 여기서 변환합니다.
        payload_chunks = [
            {"chunk_id": cid, "content": text, "title": title, "section": label}
            for cid, text, title, label, _sim in chunks
        ]
        payload_event = {
            "zone": event.zone,
            "detected_object": event.detected_object,
            "risk_level": event.risk_level,
            "distance": event.distance,
            "issue": event.issue,
        }

        try:
            result = client.generate_action(payload_event, payload_chunks)
        except Exception as exc:
            # 안전 파이프라인이 LLM 때문에 멈추면 안 됩니다.
            self._log(f"LLM 생성 중 예외 — 규칙 기반으로 대체 ({exc})")
            return None

        if not isinstance(result, dict):
            if result is not None:
                self._log(f"LLM 응답 형식이 예상과 다르다 "
                          f"({type(result).__name__}) — 규칙 기반으로 대체")
            return None

        text = _truncate(str(result.get("text", "")))
        if not text:
            self._log("LLM 응답에 본문이 없다 — 규칙 기반으로 대체")
            return None

        self._log(f"LLM 생성 완료 ({result.get('latency_ms', 0)}ms, "
                  f"{len(text)}자 / {MAX_ACTION_LEN})")
        return text

    def _get_llm(self):
        """LLM 클라이언트를 지연 로딩하고 환경설정 타임아웃을 반영한다."""
        if self._llm is not None:
            return self._llm
        if not self._use_llm:
            return None
        try:
            from rag.vllm_client import LlmClient
            base_url = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
            model = os.environ.get("LLM_MODEL", "google/gemma-2-2b-it")
            try:
                timeout = float(os.environ.get("LLM_TIMEOUT", "60"))
            except ValueError:
                timeout = 60.0
                self._log("LLM_TIMEOUT 값을 숫자로 읽을 수 없어 60초를 사용한다")
            try:
                self._llm = LlmClient(base_url=base_url, model=model,
                                      timeout=timeout, verbose=self._verbose)
            except TypeError:
                # 구버전 클라이언트와도 호환한다.
                self._llm = LlmClient(base_url=base_url, model=model,
                                      verbose=self._verbose)
                self._log("LlmClient가 timeout 인자를 지원하지 않아 기본값을 사용한다")
        except Exception as exc:
            self._log(f"LLM 클라이언트 로딩 실패 ({exc})")
            self._use_llm = False
            return None
        return self._llm

    # --------------------------------------------------------------------------
    # 공개 인터페이스
    # --------------------------------------------------------------------------

    def generate_response(self, event: DetectionEvent) -> ResponsePlan:
        """위험 이벤트 1건에 대한 대응안을 생성한다.

        어떤 상황에서도 ResponsePlan을 반환한다.
        지식베이스나 모델을 사용할 수 없으면 내장 지침으로 대체한다.
        """
        t0 = time.time()
        risk_type = resolve_risk_type(event.detected_object)
        query = self._build_query(event)
        self._log(f"질의: {query} | risk_type={risk_type}")

        base_action, builtin_docs = BUILTIN_GUIDELINES.get(
            risk_type, BUILTIN_GUIDELINES["general"])

        chunks: List[tuple] = []
        mode = "builtin"

        if self._kb_available():
            if self._use_embedding:
                chunks = self._search_vector(query, risk_type)
                if chunks:
                    mode = "vector"
            if not chunks:
                chunks = self._search_keyword(risk_type)
                if chunks:
                    mode = "keyword"

        # 참조 문서 목록 구성. 중복은 제거한다.
        if chunks:
            refs: List[str] = []
            for _cid, _text, title, label, _sim in chunks:
                ref = f"{title} {label}".strip()
                if ref not in refs:
                    refs.append(ref)
        else:
            refs = list(builtin_docs)

        # 확신도 산정
        if mode == "vector":
            top_sim = float(chunks[0][4])
            confidence = round(min(0.95, 0.5 + top_sim * 0.5), 2)
        elif mode == "keyword":
            confidence = 0.6
        else:
            confidence = 0.4

        # 거리 측정에 실패한 이벤트는 판단 근거가 약하므로 확신도를 낮춘다.
        # 시차 계산 실패 시 -1.0 또는 비정상적으로 큰 값이 들어온다.
        if event.distance is not None and (
                event.distance < 0 or event.distance > 15.0):
            confidence = round(max(0.3, confidence - 0.15), 2)
            refs.append("거리 측정 실패 — 현장 확인 필요")

        # LLM 생성은 선택 사항이다. 검색 근거가 아직 없더라도 내장 지침과
        # 이벤트 정보로 생성할 수 있으며, 실패하면 규칙 기반으로 대체한다.
        action = None
        if self._use_llm:
            action = self._generate_with_llm(event, chunks)
            if action:
                mode += "+llm"
                confidence = round(min(0.95, confidence + 0.05), 2)

        if not action:
            action = self._compose(event, base_action, chunks)

        self._log(f"생성 완료 mode={mode} conf={confidence} "
                  f"len={len(action)} ({(time.time() - t0) * 1000:.0f}ms)")

        return ResponsePlan(
            zone=event.zone,
            source_event=event,
            recommended_action=action,
            reference_docs=refs,
            confidence=confidence,
            generation_mode=mode,
        )

    def close(self):
        """지식베이스 연결을 종료한다."""
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


# ==============================================================================
# 단독 실행 — 동작 확인
# ==============================================================================

if __name__ == "__main__":
    agent = RagResponseAgent(verbose=True)

    sample = DetectionEvent(
        zone="A",
        detected_object="안전모 미착용",
        distance=1.1,
        box_position="x:120,y:80",
        risk_level="위험",
        issue="안전모 미착용 작업자 근접 (1.1m)",
    )

    plan = agent.generate_response(sample)

    print()
    print("recommended_action :", plan.recommended_action)
    print("reference_docs     :", plan.reference_docs)
    print("confidence         :", plan.confidence)
    print("length             :", len(plan.recommended_action),
          f"/ {MAX_ACTION_LEN}")

    agent.close()
