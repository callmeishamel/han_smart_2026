# rag — 산업안전 지식베이스 구성

프로젝트 전체 배경과 실행 순서는 최상위 [`../README.md`](../README.md)를 참고하세요.
이 폴더는 `integration/response_agent.py`의 `RagResponseAgent`가 근거로 검색하는
산업안전 지식베이스(safety_documents/safety_chunks)를 구성/적재하는 스크립트를 담고
있습니다. 더 자세한 사용 흐름은 [`../docs/RAG파이프라인_사용가이드.md`](../docs/RAG파이프라인_사용가이드.md)를
참고하세요.

## 파일 구성

| 파일 | 설명 |
|---|---|
| `setup_knowledge_base.py` | 지식베이스 테이블 생성 + 시드 데이터 적재 (최초 1회 필수) |
| `kb_config.py` | 운영 DB와 분리 가능한 지식베이스 접속 설정 |
| `check_pgvector.py` | 서버 확장과 벡터 컬럼/인덱스 상태 점검 및 보완 |
| `embed_chunks.py` | (선택) 청크를 BGE-M3로 임베딩해 벡터 검색 활성화 |
| `vllm_client.py` | (선택) vLLM/Ollama 경량 LLM 클라이언트 — `--use-llm` 으로 켜면 `response_agent.py`가 호출 |
| `check_llm.py` | LLM 서버 연결 및 생성 지연시간 점검 |
| `requirements.txt` | RAG 실행용 필수·선택 Python 의존성 |
| `sql/01_knowledge_base.sql`, `sql/02_seed_data.sql` | DDL/시드 데이터 참고용 SQL (실행은 위 파이썬 스크립트로 함) |

## setup_knowledge_base.py

최초 1회 실행하면 되며, 두 번 실행해도 중복 적재되지 않습니다(`safety_chunks`에 이미
데이터가 있으면 건너뜀).

- `safety_documents`(문서 원문/출처), `safety_chunks`(절차 단위 청크, risk_type별) 두
  테이블을 생성하고, 하드코딩된 `DOCUMENTS`/`CHUNKS` 시드 데이터(PPE/fire/machinery/
  chemical/access/loto/fall 7개 위험유형, 총 19개 청크)를 적재합니다.
- **`has_pgvector(cur)`**: `pgvector` 확장 설치 여부를 확인하고, 없으면 `CREATE EXTENSION`을
  시도합니다. 실패해도(권한 부족 등) `embedding` 컬럼 없이 테이블을 생성하며, 이 경우
  `RagResponseAgent`는 위험유형 기준 조회(`keyword` 경로)로 동작합니다 — 통합에는 지장 없음.
- **`create_tables(cur, with_vector)`**: `with_vector=True`면 `embedding vector(1024)` 컬럼과
  ivfflat 인덱스까지 생성.
- **`load_seed(cur)`**: 이미 데이터가 있으면 건너뛰고, 없으면 `DOCUMENTS`/`CHUNKS`를 적재합니다.
- 기존 테이블(`patrol_zones`, `patrol_logs`, `response_plans`, `detection_events`)에는
  영향을 주지 않습니다. `response_plans`는 `common.schema.init_all_tables()`가 이미
  별도로 생성/관리합니다.
- **`CHUNKS`의 `risk_type` 값은 `integration/response_agent.py`의 `RISK_TYPE_MAP`과
  일치해야 합니다.** 한쪽만 바꾸면 근거가 검색되지 않습니다.

```bash
source ../set_env.sh
python3 setup_knowledge_base.py

# 변경 없이 현재 상태만 점검
python3 setup_knowledge_base.py --check
```

## embed_chunks.py (선택)

`safety_chunks`에 적재된 청크를 BGE-M3(1024차원)로 임베딩해 `embedding` 컬럼에 저장합니다.
**임베딩을 적재하지 않아도 대응안 생성은 동작합니다** — `RagResponseAgent`는 위험유형
기준으로 근거를 조회하는 `keyword` 경로가 기본값이기 때문입니다. Jetson처럼 자원이
제한된 환경에서는 적재만 PC에서 수행하고 조회는 그대로 두는 방식도 가능합니다.

- **선행 조건**: pgvector 확장 설치(`safety_chunks.embedding` 컬럼 존재), 
  `pip install sentence-transformers pgvector`.
- **`check_embedding_column(cur)`**: `embedding` 컬럼 존재 여부 확인. 없으면
  `setup_knowledge_base.py`를 pgvector가 설치된 상태로 다시 실행하라고 안내.
- **`embed_all(cur, conn, model)`**: `embedding IS NULL`인 청크만 골라 `BATCH_SIZE=8`
  단위로 임베딩 생성 후 UPDATE. 메모리가 부족하면 `BATCH_SIZE`를 4/2로 낮추면 됩니다.
- **`run_search(cur, model)`**: `TEST_QUERIES`(PPE/fire/machinery 3개 시나리오)로 코사인
  유사도 검색을 수행하고, 1순위 결과의 `risk_type`이 기대값과 일치하는지 확인합니다.

```bash
source ../set_env.sh
python3 embed_chunks.py              # 적재만
python3 embed_chunks.py --search     # 적재 후 검색 검증까지
python3 embed_chunks.py --search-only  # 적재 없이 검색만
```

## vllm_client.py (선택 — 기본은 꺼짐)

GEMMA 2B 등 경량 LLM을 OpenAI 호환 API(vLLM 또는 Ollama)로 호출해 대응 지침을 생성하는
클라이언트와 프롬프트 템플릿(`SYSTEM_PROMPT`, `USER_PROMPT_TEMPLATE`, `LlmClient`,
`build_messages`)입니다. 서버 연결이 안 되면 예외 없이 `None`을 반환하도록 설계되어 있습니다.

```bash
python3 vllm_client.py   # 서버 연결 및 생성 확인 (서버 없어도 프롬프트 구성만 출력하고 안전 종료)
```

**파이프라인에 연결되어 있습니다(v2).** 예전에는 이 파일이 완성돼 있는데도 아무도
호출하지 않는 죽은 코드였고, 그동안 "RAG 대응안"은 사실 검색 + 문장 템플릿이었습니다.

```bash
./run_pipeline_rag.sh --use-llm                       # vLLM (기본)
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=gemma2:2b \
  ./run_pipeline_rag.sh --use-llm                     # Ollama
./run_pipeline_rag.sh --use-llm --async-llm           # 수신 루프와 LLM 분리(권장)
```

- **기본값은 꺼짐.** 켜지 않으면 동작이 예전과 완전히 같습니다.
- 근거가 없을 때도 이벤트와 내장 지침을 바탕으로 호출하며, 서버가 없거나 실패하면
  **조용히 규칙 기반 문장으로 떨어집니다.**
- 생성에 성공하면 현재 검색 경로의 확신도에 0.05를 더하고 최대 0.95로 제한합니다.
- `BASE_URL`/`MODEL`은 `LLM_BASE_URL`/`LLM_MODEL` 환경변수를 읽습니다. **단독 실행과
  파이프라인이 같은 변수를 봅니다** — 한쪽만 바꿔서 서로 다른 서버를 보는 일이 없도록.
- `LLM_TIMEOUT`으로 생성 요청 제한 시간을 조정합니다. CPU Ollama는 180초를 권장합니다.

`--async-llm`은 `integration/response_plan_worker.py`에서 대응안 생성을 별도
스레드로 분리합니다. 메인 UDP 수신과 `patrol_logs` 적재는 계속되며, 큐가
가득 차면 오래된 대응안 요청부터 버립니다. 탐지 이벤트 자체는 버리지 않습니다.

자세한 동작은 [`../integration/README.md`](../integration/README.md)의 "죽은 코드였던
LLM 클라이언트 연결" 참고.

## sql/

`01_knowledge_base.sql`(DDL), `02_seed_data.sql`(시드 데이터) — 위 파이썬 스크립트가 실행하는
내용과 동일한 것을 SQL로도 참고할 수 있게 남겨둔 파일입니다. 실행은 파이썬 스크립트로
하세요(멱등성/pgvector 유무 분기가 파이썬 쪽에만 있습니다).

## 환경변수

`rag.kb_config.get_kb_db_config()`를 통해 기본적으로 `SFP_DB_NAME` / `SFP_DB_USER` /
`SFP_DB_PASSWORD` / `SFP_DB_HOST` / `SFP_DB_PORT`를 사용합니다. `RAG_KB_HOST`,
`RAG_KB_NAME` 등의 `RAG_KB_*` 변수를 지정하면 지식베이스만 별도 DB로 분리됩니다.
`setup_knowledge_base.py`, `embed_chunks.py` 모두 비밀번호가
비어 있으면 실행 전에 `source set_env.sh`(또는 PowerShell `set_env.ps1`)를 먼저 하라고
안내하고 종료합니다.

## 알려진 이슈

- `CHUNKS`의 시드 문장은 "검증용 문장"이라고 스크립트 자체에 명시되어 있습니다 — 제출
  단계에서 법령 원문으로 교체가 필요합니다.

## 다른 폴더와의 연동 지점

- `common/schema.py`: 운영 DB 기본 접속 설정을 `kb_config.py`가 폴백으로 사용합니다.
- `integration/response_agent.py`: 이 폴더가 만든 `safety_chunks`/`safety_documents`
  테이블을 조회해서 근거를 검색합니다. `risk_type` 값 체계를 공유합니다(`RISK_TYPE_MAP`).
- `pipeline/patrol_pipeline_rag.py`: `RagResponseAgent`를 통해 간접적으로 이 지식베이스를
  사용 — 실행 전 `setup_knowledge_base.py`를 최초 1회 돌려야 합니다.
