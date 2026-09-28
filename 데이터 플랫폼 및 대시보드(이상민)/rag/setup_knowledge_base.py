"""
산업안전 지식베이스 구성 스크립트

역할
----
대응안 생성에 사용할 지식베이스 테이블을 만들고 안전 지침을 적재합니다.
최초 1회만 실행하면 되며, 두 번 실행해도 중복 적재되지 않습니다.

    safety_documents   문서 원문과 출처
    safety_chunks      절차 단위 청크 (필요 시 임베딩 포함)

기존 테이블(patrol_zones, patrol_logs, response_plans, detection_events)에는
영향을 주지 않습니다. 대응안 저장은 기존 response_plans를 그대로 사용하며,
그 테이블은 파이프라인의 init_all_tables()가 이미 생성합니다.

pgvector 확장이 없는 환경에서는 embedding 컬럼 없이 생성됩니다.
이 경우 위험유형 기준 조회로 동작하며, 통합에는 지장이 없습니다.

사용법
------
    source set_env.sh
    python3 rag/setup_knowledge_base.py

    변경 없이 점검만:
    python3 rag/setup_knowledge_base.py --check
"""

import argparse
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from rag.kb_config import get_kb_db_config  # noqa: E402

BAR = "=" * 70


# ==============================================================================
# 안전 지침 데이터
#
#   위험유형 값은 integration/response_agent.py의 RISK_TYPE_MAP과
#   일치해야 합니다. 한쪽만 바꾸면 근거가 검색되지 않습니다.
#
#   현재 내용은 검증용 문장입니다. 제출 단계에서 법령 원문으로 교체합니다.
# ==============================================================================
DOCUMENTS = [
    (1, "산업안전보건기준에 관한 규칙 발췌", "국가법령정보센터", "법령"),
    (2, "정비작업 안전 절차 지침", "안전보건공단", "지침"),
    (3, "작업장 통로 및 차량 운행 관리 기준", "안전보건공단", "지침"),
]

CHUNKS = [
    # 보호구 — 안전모 미착용 대응
    (1, "PPE", "보호구-1",
     "작업자는 작업 성격에 맞는 보호구를 착용하여야 하며, 미착용이 확인된 경우 작업을 중지시킨다."),
    (1, "PPE", "보호구-2",
     "보호구 미착용 작업자를 발견한 경우 관리감독자에게 보고하고 착용을 확인한 후 작업을 재개한다."),
    (1, "PPE", "보호구-3",
     "머리 보호가 필요한 작업 구역에서는 안전모 착용 상태를 상시 확인하여야 한다."),
    # 화재
    (1, "fire", "화재예방-1",
     "작업장에서 화재가 발생한 경우 즉시 경보를 발령하고 작업자를 안전한 장소로 대피시켜야 한다."),
    (1, "fire", "화재예방-2",
     "초기 소화가 가능한 규모인 경우에 한해 소화기를 사용하고, 확산 시에는 대피를 우선한다."),
    (1, "fire", "화재예방-3",
     "화재 발생 구역의 전원을 차단하고 인화성 물질의 추가 유입을 막아야 한다."),
    # 차량·기계
    (3, "machinery", "차량운행-1",
     "차량계 하역운반기계 운행 구역에는 유도자를 배치하고 보행자와의 접촉을 방지하여야 한다."),
    (3, "machinery", "차량운행-2",
     "차량 후진 시 경보 장치를 작동시키고, 후방 확인이 어려운 경우 유도자의 신호에 따른다."),
    (3, "machinery", "통로관리-1",
     "작업장 통로에 적재물을 방치하여 통행을 방해해서는 아니 된다."),
    (3, "machinery", "통로관리-2",
     "적재물이 붕괴할 우려가 있는 경우 즉시 작업을 중지하고 안정화 조치를 한다."),
    # 작업자 접근
    (1, "access", "접근제한-1",
     "가동 중인 기계의 위험 부위에 작업자가 접근하지 못하도록 방호 조치를 하여야 한다."),
    (1, "access", "접근제한-2",
     "작업자가 위험 구역에 접근한 경우 경고 신호를 발하고 이격을 유도한다."),
    # 유해물질
    (1, "chemical", "유해물질-1",
     "유해물질이 누출된 구역은 즉시 출입을 통제하고 관계자 외 접근을 금지한다."),
    (1, "chemical", "유해물질-2",
     "누출 구역의 강제 환기를 가동하고 보호구를 착용한 후 누출원을 차단한다."),
    (1, "chemical", "유해물질-3",
     "해당 물질의 물질안전보건자료를 확인하여 성상에 맞는 대응 조치를 수행한다."),
    # 정비 절차 — 시각 탐지 대상은 아니며 관리자 질의 경로에서 사용
    (2, "loto", "정비절차-1",
     "설비 정비 전 전원을 차단하고 잠금 및 표지 조치를 완료한 후 작업을 시작한다."),
    (2, "loto", "정비절차-2",
     "에너지 차단이 확인되지 않은 상태에서 정비 작업을 수행해서는 아니 된다."),
    # 쓰러짐
    (1, "fall", "응급조치-1",
     "작업자가 쓰러진 것을 발견한 경우 즉시 현장을 확인하고 응급 구조를 요청한다."),
    (1, "fall", "응급조치-2",
     "중대재해가 발생한 경우 작업을 중지하고 사고 발생 구역을 보존하여야 한다."),
]


# ==============================================================================
# DDL
# ==============================================================================
DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS safety_documents (
    doc_id     SERIAL       PRIMARY KEY,
    title      VARCHAR(200) NOT NULL,
    source     VARCHAR(300),
    doc_type   VARCHAR(50),
    full_text  TEXT,
    created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);
"""

CHUNKS_DDL_TEMPLATE = """
CREATE TABLE IF NOT EXISTS safety_chunks (
    chunk_id        SERIAL       PRIMARY KEY,
    doc_id          INTEGER      REFERENCES safety_documents(doc_id),
    risk_type       VARCHAR(30)  NOT NULL,
    section_label   VARCHAR(100),
    content_snippet TEXT         NOT NULL,
    token_count     INTEGER,
    {embedding_col}
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);
"""


def has_pgvector(cur) -> bool:
    """pgvector 확장을 사용할 수 있는지 확인하고, 없으면 설치를 시도합니다."""
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    if row:
        print(f"  pgvector 확장: 버전 {row[0]}")
        return True
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        print("  pgvector 확장: 새로 설치했습니다")
        return True
    except Exception:
        print("  pgvector 확장: 미설치 — embedding 컬럼 없이 생성합니다")
        print("                 (위험유형 조회로 동작하므로 사용에는 지장 없음)")
        return False


def create_tables(cur, with_vector: bool):
    embedding_col = "embedding vector(1024)," if with_vector else ""
    cur.execute(DOCUMENTS_DDL)
    cur.execute(CHUNKS_DDL_TEMPLATE.format(embedding_col=embedding_col))
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_safety_chunks_risk_type
        ON safety_chunks (risk_type)
    """)
    if with_vector:
        try:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_safety_chunks_embedding
                ON safety_chunks USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 10)
            """)
        except Exception:
            # 데이터가 없으면 인덱스 생성이 실패할 수 있습니다.
            # 임베딩 적재 후 다시 만들면 되므로 진행합니다.
            pass
    print("  테이블 생성 완료: safety_documents, safety_chunks")


def load_seed(cur) -> int:
    """지침 데이터를 적재합니다. 이미 있으면 건너뜁니다."""
    cur.execute("SELECT COUNT(*) FROM safety_chunks")
    existing = cur.fetchone()[0]
    if existing > 0:
        print(f"  이미 {existing}건이 적재되어 있어 건너뜁니다")
        return existing

    for doc_id, title, source, doc_type in DOCUMENTS:
        cur.execute("""
            INSERT INTO safety_documents (doc_id, title, source, doc_type)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (doc_id) DO NOTHING
        """, (doc_id, title, source, doc_type))

    # 수동 삽입한 doc_id와 시퀀스를 맞춥니다.
    # 이후 자동 채번이 충돌하지 않도록 하기 위함입니다.
    cur.execute("""
        SELECT setval('safety_documents_doc_id_seq',
                      (SELECT COALESCE(MAX(doc_id), 1) FROM safety_documents))
    """)

    for doc_id, risk_type, label, content in CHUNKS:
        cur.execute("""
            INSERT INTO safety_chunks
                (doc_id, risk_type, section_label, content_snippet)
            VALUES (%s, %s, %s, %s)
        """, (doc_id, risk_type, label, content))

    print(f"  적재 완료: 문서 {len(DOCUMENTS)}건, 청크 {len(CHUNKS)}건")
    return len(CHUNKS)


def show_status(cur):
    cur.execute("""
        SELECT risk_type, COUNT(*) FROM safety_chunks
        GROUP BY risk_type ORDER BY COUNT(*) DESC, risk_type
    """)
    rows = cur.fetchall()
    if not rows:
        print("  적재된 청크가 없습니다")
        return
    print()
    print(f"  {'위험유형':<12} {'청크':>6}")
    print("  " + "-" * 20)
    for rt, n in rows:
        print(f"  {rt:<12} {n:>6}")
    cur.execute("SELECT COUNT(*) FROM safety_chunks")
    print("  " + "-" * 20)
    print(f"  {'합계':<12} {cur.fetchone()[0]:>6}")


def main():
    parser = argparse.ArgumentParser(description="산업안전 지식베이스 구성")
    parser.add_argument("--check", action="store_true",
                        help="변경 없이 현재 상태만 확인")
    args = parser.parse_args()

    print(BAR)
    print(" 산업안전 지식베이스 구성")
    print(BAR)

    cfg = get_kb_db_config()
    if not cfg.get("password"):
        print()
        print(" SFP_DB_PASSWORD가 비어 있습니다.")
        print(" 실행 전에 'source set_env.sh' 를 먼저 수행하세요.")
        sys.exit(1)

    print(f" 대상: {cfg['host']}:{cfg['port']} / {cfg['dbname']}")
    print()

    try:
        import psycopg2
    except ImportError:
        print(" psycopg2가 설치되어 있지 않습니다.")
        print(" pip3 install psycopg2-binary")
        sys.exit(1)

    try:
        conn = psycopg2.connect(**cfg)
    except Exception as exc:
        print(f" DB 연결 실패: {exc}")
        print(" PostgreSQL 구동 상태와 접속 정보를 확인하세요.")
        sys.exit(1)

    conn.autocommit = True
    cur = conn.cursor()

    if args.check:
        print(" 현재 상태 (변경하지 않음)")
        try:
            show_status(cur)
        except Exception:
            print("  지식베이스 테이블이 아직 없습니다")
    else:
        has_vec = has_pgvector(cur)
        create_tables(cur, has_vec)
        load_seed(cur)
        show_status(cur)

    print()
    print(BAR)
    print(" 완료 — 이제 ./run_pipeline_rag.sh 로 실행할 수 있습니다")
    print(BAR)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
