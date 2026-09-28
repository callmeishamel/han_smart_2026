"""
BGE-M3 임베딩 생성 및 적재 스크립트

역할
----
safety_chunks에 적재된 청크를 BGE-M3(1024차원)로 임베딩해 embedding 컬럼에
저장합니다. 적재가 끝나면 코사인 유사도 검색 경로를 사용할 수 있습니다.

이 스크립트는 선택 사항입니다
------------------------------
임베딩을 적재하지 않아도 대응안 생성은 동작합니다. 위험유형 기준으로
근거를 조회하기 때문입니다. 임베딩은 검색 정확도를 높이는 단계이며,
Jetson처럼 자원이 제한된 환경에서는 적재만 PC에서 수행하고 조회는
그대로 두는 방식도 가능합니다.

선행 조건
---------
    pgvector 확장 설치 (safety_chunks.embedding 컬럼이 있어야 함)
    pip3 install sentence-transformers pgvector

사용법
------
    source set_env.sh
    python3 rag/embed_chunks.py

    검색 동작까지 확인:
    python3 rag/embed_chunks.py --search
"""

import argparse
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from rag.kb_config import get_kb_db_config  # noqa: E402

EMBED_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024        # safety_chunks.embedding의 vector(1024)와 일치해야 함
BATCH_SIZE = 8          # 메모리가 부족하면 4 또는 2로 낮춥니다
TOP_K = 3

BAR = "=" * 70

# 검색 검증용 시나리오. 현재 탐지 클래스 기준으로 구성했습니다.
TEST_QUERIES = [
    ("PPE", "작업자가 안전모를 착용하지 않고 작업 구역에 진입함"),
    ("fire", "작업 구역에서 화재가 발생함"),
    ("machinery", "차량 운행 동선 인근에 작업자가 접근함"),
]


def check_embedding_column(cur) -> bool:
    """embedding 컬럼 존재 여부를 확인합니다."""
    cur.execute("""
        SELECT udt_name FROM information_schema.columns
        WHERE table_name = 'safety_chunks' AND column_name = 'embedding'
    """)
    row = cur.fetchone()
    if not row:
        print(" safety_chunks에 embedding 컬럼이 없습니다.")
        print(" pgvector 확장을 설치한 뒤 아래를 실행하세요.")
        print("   python3 rag/setup_knowledge_base.py")
        return False
    print(f"  embedding 컬럼 확인: {row[0]}")
    return True


def embed_all(cur, conn, model):
    """임베딩이 비어 있는 청크를 배치 단위로 채웁니다."""
    cur.execute("""
        SELECT chunk_id, risk_type, content_snippet
        FROM   safety_chunks
        WHERE  embedding IS NULL
        ORDER  BY chunk_id
    """)
    rows = cur.fetchall()
    print(f"  임베딩 대상: {len(rows)}건")

    if not rows:
        print("  모든 청크가 이미 적재되어 있습니다")
        return

    t0 = time.time()
    done = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        vecs = model.encode([r[2] for r in batch], normalize_embeddings=True)
        for (chunk_id, risk_type, _), vec in zip(batch, vecs):
            cur.execute(
                "UPDATE safety_chunks SET embedding = %s WHERE chunk_id = %s",
                (vec, chunk_id),
            )
            done += 1
            print(f"    [{done:>3}/{len(rows)}] chunk_id={chunk_id:<4} "
                  f"risk_type={risk_type:<10} dim={len(vec)}")
        conn.commit()
    print(f"  생성 완료 ({time.time() - t0:.1f}s)")


def show_status(cur):
    cur.execute("""
        SELECT risk_type, COUNT(*), COUNT(embedding)
        FROM   safety_chunks GROUP BY risk_type ORDER BY COUNT(*) DESC
    """)
    print()
    print(f"  {'위험유형':<12} {'청크':>6} {'임베딩':>8}")
    print("  " + "-" * 28)
    for rt, total, emb in cur.fetchall():
        print(f"  {rt:<12} {total:>6} {emb:>8}")
    cur.execute("SELECT COUNT(*), COUNT(embedding) FROM safety_chunks")
    total, emb = cur.fetchone()
    print("  " + "-" * 28)
    print(f"  {'합계':<12} {total:>6} {emb:>8}")
    return emb


def run_search(cur, model):
    """시나리오별로 코사인 유사도 검색을 수행합니다.

    실제 파이프라인은 resolve_risk_type()으로 위험유형을 먼저
    결정한 뒤, 그 유형의 청크 안에서 벡터 검색합니다. 이 검증도
    response_agent.py와 같은 조건을 사용해 운영 경로를 그대로 검증합니다.
    """
    print()
    print(BAR)
    print(" 코사인 유사도 검색 확인")
    print(BAR)

    hit = 0
    for expect, query in TEST_QUERIES:
        qvec = model.encode([query], normalize_embeddings=True)[0]

        t0 = time.time()
        cur.execute("""
            SELECT c.chunk_id, c.risk_type,
                   LEFT(c.content_snippet, 40),
                   1 - (c.embedding <=> %s) AS similarity
            FROM   safety_chunks c
            WHERE  c.embedding IS NOT NULL
              AND  c.risk_type = %s
            ORDER  BY c.embedding <=> %s
            LIMIT  %s
        """, (qvec, expect, qvec, TOP_K))
        rows = cur.fetchall()
        ms = (time.time() - t0) * 1000

        print()
        print(f" 질의: {query}")
        print(" " + "-" * 68)
        print(f" {'순위':<5} {'chunk':<7} {'위험유형':<12} {'유사도':<9} 본문")
        for rank, (cid, rt, snippet, sim) in enumerate(rows, 1):
            print(f" {rank:<5} {cid:<7} {rt:<12} {sim:<9.4f} {snippet}")
        print(" " + "-" * 68)
        print(f" 소요 {ms:.1f} ms | 연산자 <=> (코사인 거리)")

        if rows and rows[0][1] == expect:
            hit += 1
            print(f" 1순위 위험유형 {rows[0][1]} — 기대값과 일치")
        else:
            got = rows[0][1] if rows else "없음"
            print(f" 1순위 위험유형 {got} — 기대값 {expect}과 다름")

    print()
    print(BAR)
    print(f" 검색 정확도: {hit}/{len(TEST_QUERIES)}")
    print(BAR)


def main():
    parser = argparse.ArgumentParser(description="BGE-M3 임베딩 생성 및 적재")
    parser.add_argument("--search", action="store_true",
                        help="적재 후 유사도 검색까지 확인")
    parser.add_argument("--search-only", action="store_true",
                        help="적재 없이 검색만 확인")
    args = parser.parse_args()

    print(BAR)
    print(" BGE-M3 임베딩 적재")
    print(BAR)

    cfg = get_kb_db_config()
    if not cfg.get("password"):
        print(" SFP_DB_PASSWORD가 비어 있습니다. 'source set_env.sh'를 먼저 실행하세요.")
        sys.exit(1)

    try:
        import psycopg2
        from pgvector.psycopg2 import register_vector
    except ImportError as exc:
        print(f" 패키지가 없습니다: {exc}")
        print(" pip3 install psycopg2-binary pgvector sentence-transformers")
        sys.exit(1)

    conn = psycopg2.connect(**cfg)
    register_vector(conn)
    cur = conn.cursor()

    if not check_embedding_column(cur):
        sys.exit(1)

    from sentence_transformers import SentenceTransformer
    print(f"  모델 로딩: {EMBED_MODEL}")
    print("  최초 실행 시 모델 다운로드로 수 분이 걸릴 수 있습니다")
    t0 = time.time()
    model = SentenceTransformer(EMBED_MODEL)
    if hasattr(model, "get_embedding_dimension"):
        dim = model.get_embedding_dimension()
    else:  # sentence-transformers 6 미만 호환
        dim = model.get_sentence_embedding_dimension()
    print(f"  로딩 완료 ({time.time() - t0:.1f}s) | 임베딩 차원 {dim}")

    if dim != EMBED_DIM:
        print(f" 차원 불일치: 스키마는 vector({EMBED_DIM})인데 모델 출력은 {dim}")
        print(" 스키마 또는 모델을 맞춰야 합니다.")
        sys.exit(1)

    if not args.search_only:
        embed_all(cur, conn, model)

    embedded = show_status(cur)

    if (args.search or args.search_only) and embedded > 0:
        run_search(cur, model)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
