# rag — 지식베이스 및 LLM 연동

대응안 생성에 필요한 모듈입니다. 설치는 `../docs/설치가이드_리눅스.md` 참고.

## 파일

| 파일 | 역할 | 실행 시점 |
|---|---|---|
| `setup_knowledge_base.py` | 지식베이스 테이블 생성 및 지침 적재 | 최초 1회 |
| `check_pgvector.py` | pgvector 확인 및 embedding 컬럼 준비 | 확장 설치 후 |
| `embed_chunks.py` | BGE-M3 임베딩 적재 및 검색 확인 | 컬럼 준비 후 |
| `vllm_client.py` | GEMMA 2B 호출 클라이언트 | 라이브러리 |
| `check_llm.py` | LLM 구동 확인, Fast/Slow 지연 측정 | 서버 구동 후 |
| `sql/*.sql` | 테이블 정의와 지침 데이터 (참고용) | 실행 불필요 |

## 실행 순서

```bash
cd rag_upload
pip3 install -r rag/requirements.txt
source set_env.sh

python3 rag/setup_knowledge_base.py    # 필수
python3 rag/check_pgvector.py          # pgvector 설치 후
python3 rag/embed_chunks.py --search   # 임베딩 적재
python3 rag/check_llm.py               # LLM 서버 구동 후
```

각 스크립트는 선행 조건이 없으면 오류 대신 무엇이 빠졌는지 안내합니다.

## 3단계 폴백

지식베이스가 없어도, 임베딩이 없어도, LLM 서버가 없어도 대응안이
생성됩니다.

| 경로 | 조건 | 확신도 |
|---|---|---|
| vector | 지식베이스 + 임베딩 | 0.70~0.95 |
| keyword | 지식베이스만 | 0.60 |
| builtin | 코드 내장 지침 | 0.40 |

LLM 생성에 성공하면 0.05 가산됩니다.

## LLM 을 켤 때

`--use-llm` 만 켜면 생성이 끝날 때까지 UDP 수신 루프가 멈춥니다. CPU
추론이면 `--async-llm` 을 함께 켜십시오. 생성이 별도 스레드로 분리되어
수신이 계속됩니다. 구현은 `../integration/response_plan_worker.py` 이고,
설명은 `../docs/설치가이드_리눅스.md` 4-1절에 있습니다.

## 참고

`common/schema.py` 는 이 폴더가 아니라 `데이터 플랫폼 및 대시보드(이상민)/`
에 있습니다. `run_pipeline_rag.sh` 가 PYTHONPATH 를 잡아 줍니다.
