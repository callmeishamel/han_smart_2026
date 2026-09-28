"""
pgvector 설치 확인 및 벡터 컬럼 준비 스크립트

역할
----
DB 서버에 pgvector 확장이 설치되어 있는지 확인하고, 설치되어 있으면
safety_chunks에 embedding 컬럼과 벡터 인덱스를 추가합니다.

어느 DB를 보는가
-----------------
지식베이스 DB 입니다. RAG_KB_* 환경변수가 설정되어 있으면 그쪽을 보고,
없으면 운영 DB(SFP_DB_*)를 그대로 봅니다. pgvector 는 지식베이스가 있는
서버에만 있으면 되며, 운영 DB 쪽에는 필요하지 않습니다.

이 스크립트를 실행하는 사람
---------------------------
지식베이스 DB 서버가 설치된 PC에서 실행하는 것을 권장합니다.
pgvector는 서버 측 확장이므로, 원격에서 CREATE EXTENSION을 실행해도
서버에 확장 파일이 없으면 실패합니다.

    확장 파일 설치   DB 서버 PC에서만 가능 (docs/설치가이드_리눅스.md 1절 참조)
    CREATE EXTENSION 원격에서도 가능 (파일이 설치되어 있어야 성공)
    컬럼·인덱스 추가  원격에서도 가능

이미 지식베이스가 구성된 상태에서 나중에 pgvector를 설치했다면,
이 스크립트만 다시 실행하면 컬럼이 추가됩니다.
테이블을 다시 만들 필요는 없습니다.

사용법
------
    source set_env.sh
    python3 rag/check_pgvector.py

    확인만 하고 변경하지 않으려면:
    python3 rag/check_pgvector.py --check
"""

import argparse
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from rag.kb_config import describe, get_kb_db_config, is_split  # noqa: E402

EMBED_DIM = 1024        # BGE-M3 출력 차원
BAR = "=" * 70


def check_extension(cur) -> bool:
    """확장 설치 여부를 확인합니다. 없으면 설치를 시도합니다."""
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    if row:
        print(f"  [OK] pgvector 확장 활성화됨 (버전 {row[0]})")
        return True

    # 확장 파일이 서버에 설치되어 있는지 확인
    cur.execute("SELECT name FROM pg_available_extensions WHERE name = 'vector'")
    if not cur.fetchone():
        print("  [없음] 서버에 pgvector 확장 파일이 설치되어 있지 않습니다.")
        print()
        print("         DB 서버가 설치된 PC에서 확장을 먼저 설치해야 합니다.")
        print("         Python 패키지(pip install pgvector)만으로는 활성화되지")
        print("         않습니다. 서버 측 확장 설치가 별도로 필요합니다.")
        print("         설치 방법은 docs/설치가이드_리눅스.md 1절을 참조하십시오.")
        print()
        print("         설치하지 않아도 대응안 생성은 동작합니다.")
        print("         위험유형 기준으로 근거를 찾는 방식이며,")
        print("         유사도 검색만 사용할 수 없습니다.")
        return False

    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        print(f"  [OK] pgvector 확장을 새로 활성화했습니다 (버전 {row[0]})")
        return True
    except Exception as exc:
        print(f"  [실패] 확장 활성화 실패: {exc}")
        print("         DB 사용자에게 확장 생성 권한이 필요합니다.")
        return False


def check_table(cur) -> bool:
    """지식베이스 테이블 존재 여부를 확인합니다."""
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'safety_chunks'
        )
    """)
    exists = bool(cur.fetchone()[0])
    if exists:
        cur.execute("SELECT COUNT(*) FROM safety_chunks")
        print(f"  [OK] safety_chunks 테이블 확인 ({cur.fetchone()[0]}건)")
    else:
        print("  [없음] safety_chunks 테이블이 없습니다.")
        print("         먼저 아래를 실행하십시오.")
        print("           python3 rag/setup_knowledge_base.py")
    return exists


def check_column(cur) -> bool:
    """embedding 컬럼 존재 여부를 확인합니다."""
    cur.execute("""
        SELECT udt_name FROM information_schema.columns
        WHERE table_name = 'safety_chunks' AND column_name = 'embedding'
    """)
    row = cur.fetchone()
    if row:
        print(f"  [OK] embedding 컬럼 확인 ({row[0]})")
        return True
    print("  [없음] embedding 컬럼이 아직 없습니다.")
    return False


def add_column(cur):
    """embedding 컬럼과 벡터 인덱스를 추가합니다.

    지식베이스가 pgvector 없이 만들어진 경우, 확장을 설치한 뒤
    이 함수로 컬럼만 덧붙이면 됩니다. 기존 데이터는 유지됩니다.
    """
    cur.execute(f"""
        ALTER TABLE safety_chunks
        ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})
    """)
    print(f"  [OK] embedding 컬럼 추가 (vector({EMBED_DIM}))")

    # 인덱스의 lists 값은 행 수의 제곱근 수준을 권장합니다.
    # 데이터가 적으면 작게 잡아야 생성이 실패하지 않습니다.
    cur.execute("SELECT COUNT(*) FROM safety_chunks")
    n = cur.fetchone()[0]
    lists = max(1, min(100, int(n ** 0.5)))

    try:
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_safety_chunks_embedding
            ON safety_chunks USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = {lists})
        """)
        print(f"  [OK] 벡터 인덱스 생성 (lists={lists}, 청크 {n}건 기준)")
    except Exception as exc:
        print(f"  [보류] 인덱스 생성 보류: {exc}")
        print("         임베딩 적재 후 다시 실행하면 생성됩니다.")


def show_status(cur):
    """임베딩 적재 현황을 출력합니다."""
    try:
        cur.execute("""
            SELECT risk_type, COUNT(*), COUNT(embedding)
            FROM   safety_chunks GROUP BY risk_type
            ORDER  BY COUNT(*) DESC, risk_type
        """)
        rows = cur.fetchall()
    except Exception:
        return

    if not rows:
        return

    print()
    print(f"  {'위험유형':<12} {'청크':>6} {'임베딩':>8}")
    print("  " + "-" * 28)
    for rt, total, emb in rows:
        print(f"  {rt:<12} {total:>6} {emb:>8}")

    cur.execute("SELECT COUNT(*), COUNT(embedding) FROM safety_chunks")
    total, emb = cur.fetchone()
    print("  " + "-" * 28)
    print(f"  {'합계':<12} {total:>6} {emb:>8}")
    return total, emb


def main():
    parser = argparse.ArgumentParser(
        description="pgvector 설치 확인 및 벡터 컬럼 준비")
    parser.add_argument("--check", action="store_true",
                        help="변경 없이 확인만 수행")
    args = parser.parse_args()

    print(BAR)
    print(" pgvector 설치 확인 및 벡터 컬럼 준비")
    print(BAR)

    cfg = get_kb_db_config()
    if not cfg.get("password"):
        var = "RAG_KB_PASSWORD" if is_split() else "SFP_DB_PASSWORD"
        print(f" {var}가 비어 있습니다.")
        print(" 실행 전에 'source set_env.sh'를 수행하십시오.")
        sys.exit(1)

    print(f" 대상: {cfg['host']}:{cfg['port']} / {cfg['dbname']}")
    if is_split():
        print(f" 구성: {describe()}")
        print("       pgvector 는 지식베이스 쪽에만 있으면 됩니다.")
    print()

    try:
        import psycopg2
    except ImportError:
        print(" psycopg2가 설치되어 있지 않습니다.")
        print(" pip install psycopg2-binary")
        sys.exit(1)

    try:
        conn = psycopg2.connect(**cfg)
    except Exception as exc:
        print(f" DB 연결 실패: {exc}")
        sys.exit(1)

    conn.autocommit = True
    cur = conn.cursor()

    has_ext = check_extension(cur)
    has_table = check_table(cur)

    if not has_table:
        cur.close()
        conn.close()
        sys.exit(1)

    has_col = check_column(cur)

    if has_ext and not has_col and not args.check:
        add_column(cur)
        has_col = True

    status = show_status(cur)

    print()
    print(BAR)
    if has_ext and has_col:
        if status and status[1] == 0:
            print(" 준비 완료 — 다음 단계로 임베딩을 적재하십시오.")
            print("   python3 rag/embed_chunks.py --search")
        else:
            print(" 임베딩까지 적재되어 있습니다.")
            print(" 파이프라인에서 벡터 검색을 사용하려면:")
            print("   ./run_pipeline_rag.sh --use-embedding")
    else:
        print(" pgvector 없이 동작 중입니다.")
        print(" 위험유형 기준 조회로 대응안이 생성되므로 사용에는 지장 없습니다.")
        print(" 유사도 검색이 필요하면 docs/설치가이드_리눅스.md 1절을 참조하십시오.")
    print(BAR)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
