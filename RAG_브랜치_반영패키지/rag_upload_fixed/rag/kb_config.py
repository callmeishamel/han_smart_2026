"""
================================================================================
파일명   : rag/kb_config.py
목적     : 지식베이스 DB 접속 설정 (운영 DB와 분리 가능)
버전     : v1.0
담당     : 대응안 생성 파트
--------------------------------------------------------------------------------
왜 나누는가

  지식베이스는 pgvector 확장이 필요하다. 확장은 서버 측 기능이라 postgres
  프로세스가 도는 PC 에 설치해야 하며, 파이프라인이 어디서 도는지와는
  상관이 없다.

  팀 공용 DB 가 윈도우에 있으면 리눅스 소스 빌드 절차(make / pgxs.mk /
  PG_CONFIG)가 통째로 맞지 않는다. 반면 지식베이스는 청크 수십 건짜리
  읽기 전용이고 대시보드가 조회하지도 않으므로, 리눅스 쪽 로컬 DB 에 두면
  설치와 운영이 모두 단순해진다.

      팀 공용 DB      patrol_logs, response_plans, event_logs
                      대시보드가 읽는다. 파이프라인이 쓴다.

      지식베이스 DB   safety_documents, safety_chunks,
                      response_guidelines
                      대응안 생성이 읽기만 한다. pgvector 는 여기만 필요하다.

  나눠도 안전한 이유는 response_plans.reference_docs 가 청크 외래키가 아니라
  문서 제목 TEXT 이기 때문이다. 대시보드는 근거 목록을 팀 공용 DB 에서
  그대로 받으며, 두 DB 를 조인할 일이 없다.

기본 동작

  RAG_KB_HOST 또는 RAG_KB_NAME 을 설정했을 때만 갈라진다.
  설정하지 않으면 지금까지처럼 SFP_DB_* 하나로 동작하므로, 단일 DB 구성을
  쓰는 환경은 이 파일을 넣어도 달라지는 것이 없다.

환경변수

    export RAG_KB_HOST=localhost          # 지식베이스가 있는 서버
    export RAG_KB_PORT=5432
    export RAG_KB_NAME=smart_factory_kb
    export RAG_KB_USER=postgres
    export RAG_KB_PASSWORD=...

  개별 항목을 생략하면 같은 이름의 SFP_DB_* 값을 그대로 쓴다. 같은 서버의
  다른 데이터베이스만 쓰고 싶다면 RAG_KB_NAME 하나만 설정하면 된다.

사용처

    integration/response_agent.py   근거 검색
    rag/check_pgvector.py           확장 확인 및 컬럼 준비
    rag/setup_knowledge_base.py     지침 적재      (병합가이드 5절)
    rag/embed_chunks.py             임베딩 적재    (병합가이드 5절)
================================================================================
"""

from __future__ import annotations

import os
import sys

# 이 파일이 rag/ 아래에 있으므로 상위 디렉터리가 프로젝트 루트가 된다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 공통 스키마는 데이터 플랫폼 파트에 있다. 두 파트가 형제 폴더로 나뉘어
# 있을 때도 단독 실행이 되도록 형제 경로를 추가한다.
_PLATFORM_ROOT = os.path.join(
    os.path.dirname(_PROJECT_ROOT), "데이터 플랫폼 및 대시보드(이상민)"
)
if os.path.isdir(_PLATFORM_ROOT) and _PLATFORM_ROOT not in sys.path:
    sys.path.insert(0, _PLATFORM_ROOT)

from common.schema import get_db_config  # noqa: E402


def is_split() -> bool:
    """지식베이스를 운영 DB와 분리해 쓰는 구성인지 확인한다."""
    return bool(os.environ.get("RAG_KB_HOST")
                or os.environ.get("RAG_KB_NAME"))


def get_kb_db_config() -> dict:
    """지식베이스 접속 정보를 반환한다.

    분리 구성이 아니면 운영 DB 설정을 그대로 돌려주므로, 호출부는
    구성을 신경 쓰지 않고 이 함수만 쓰면 된다.
    """
    base = get_db_config()
    if not is_split():
        return base

    try:
        port = int(os.environ.get("RAG_KB_PORT", base.get("port", 5432)))
    except (TypeError, ValueError):
        port = base.get("port", 5432)

    return {
        "host":     os.environ.get("RAG_KB_HOST", base.get("host")),
        "port":     port,
        "dbname":   os.environ.get("RAG_KB_NAME", base.get("dbname")),
        "user":     os.environ.get("RAG_KB_USER", base.get("user")),
        "password": os.environ.get("RAG_KB_PASSWORD", base.get("password")),
    }


def describe() -> str:
    """현재 구성을 한 줄로 설명한다. 시작 로그와 확인 스크립트에서 쓴다."""
    kb = get_kb_db_config()
    target = f"{kb.get('host')}:{kb.get('port')}/{kb.get('dbname')}"
    if not is_split():
        return f"지식베이스 = 운영 DB 공용 ({target})"
    ops = get_db_config()
    return (f"지식베이스 {target} / "
            f"운영 DB {ops.get('host')}:{ops.get('port')}/{ops.get('dbname')}")


if __name__ == "__main__":
    print(describe())
    print("분리 구성" if is_split() else "단일 DB 구성")
