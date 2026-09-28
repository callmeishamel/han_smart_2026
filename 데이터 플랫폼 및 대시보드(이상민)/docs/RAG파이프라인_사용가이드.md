# RAG 파이프라인 사용 가이드

위험 등급의 탐지 이벤트가 들어오면 산업안전 지식베이스를 조회하고 대응
지침을 생성해 `response_plans`에 저장하는 파이프라인입니다. 처음 설치하는
경우에는 한 장짜리 [`시작하기.md`](시작하기.md)를 먼저 보십시오.

## 처리 흐름

```text
UDP 수신 → patrol_logs 적재 → 위험 이벤트 중복 억제 → RAG 대응안 생성
                                                   ├─ response_plans 적재
                                                   └─ (--tts) 현장 음성 송신
```

`--async-llm`을 사용하면 대응안 생성만 별도 워커 스레드로 이동합니다.
LLM 응답을 기다리는 동안에도 UDP 수신과 `patrol_logs` 적재가 계속됩니다.
큐가 가득 차면 오래된 대응안 요청부터 버리지만 탐지 이벤트 자체는 이미
DB에 적재된 상태이므로 유실되지 않습니다.

기존 통합 기능인 TTS 송신, 구역·객체별 중복 억제, `--zone auto`, 공통 SQL은
비동기 모드에서도 그대로 유지됩니다.

## 1. 최초 준비

프로젝트 루트에서 환경설정 예시를 복사하고 실제 DB 정보를 입력합니다.

```bash
cd '<저장소>/데이터 플랫폼 및 대시보드(이상민)'
cp set_env.example.sh set_env.sh
nano set_env.sh
source set_env.sh
```

Windows PowerShell에서는 `set_env.example.ps1`을 `set_env.ps1`로 복사한 뒤
다음처럼 현재 셸에 로드합니다.

```powershell
. .\set_env.ps1
```

실제 비밀번호가 든 `set_env.sh`와 `set_env.ps1`은 커밋하지 마십시오.

### 자동 설치(Linux)

```bash
./setup_linux.sh --check
./setup_linux.sh
```

필요한 Python 패키지, Ollama, `gemma2:2b`, pgvector를 점검하고 빠진 항목만
설치합니다. 수동 절차는 [`설치가이드_리눅스.md`](설치가이드_리눅스.md)를
참고하십시오.

### 지식베이스 적재

```bash
python3 rag/setup_knowledge_base.py
```

두 번 실행해도 지침을 중복 적재하지 않습니다. pgvector가 없어도 위험유형
기준 검색으로 정상 동작합니다.

## 2. 실행

```bash
# 규칙 기반 문장 생성
./run_pipeline_rag.sh

# Ollama/vLLM 문장 생성: CPU 환경에서는 비동기 모드 권장
./run_pipeline_rag.sh --use-llm --async-llm

# 비동기 큐 크기 조정
./run_pipeline_rag.sh --use-llm --async-llm --plan-queue-size 64

# 벡터 검색과 LLM 함께 사용
./run_pipeline_rag.sh --use-embedding --use-llm --async-llm

# 현장 음성 송신도 사용
./run_pipeline_rag.sh --use-llm --async-llm --tts

# 미니맵에서 현재 구역 자동 판정
SFP_PATROL_ZONE=auto ./run_pipeline_rag.sh --use-llm --async-llm
```

`run_pipeline.sh`와 `run_pipeline_rag.sh`는 같은 UDP 포트를 사용하므로 둘 중
하나만 실행해야 합니다.

### 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--use-embedding` | BGE-M3 벡터 유사도 검색 사용 |
| `--use-llm` | Ollama/vLLM으로 대응 지침 문장 생성 |
| `--async-llm` | 대응안 생성을 워커로 분리해 UDP 수신 차단 방지 |
| `--plan-queue-size N` | 비동기 대응안 대기 큐 크기(기본 32) |
| `--suppress-seconds N` | 같은 구역·객체의 재생성 최소 간격(기본 30초) |
| `--tts` | 생성된 대응안을 젯슨 스피커로 비동기 송신 |
| `--zone auto` | 미니맵의 현재 구역 사용 |

## 3. 환경변수

| 변수 | 기본값 | 용도 |
|---|---|---|
| `PIPELINE_UDP_BIND` | `0.0.0.0` | UDP 수신 바인드 주소 |
| `DASHBOARD_UDP_PORT` | `9998` | 대시보드·DB·RAG 수신 포트 |
| `LLM_BASE_URL` | `http://localhost:8000/v1` | OpenAI 호환 LLM API |
| `LLM_MODEL` | `google/gemma-2-2b-it` | 호출 모델명 |
| `LLM_TIMEOUT` | `60` | 생성 요청 제한 시간(초), CPU Ollama 권장값 180 |
| `RAG_KB_HOST` 외 `RAG_KB_*` | 미설정 | 지식베이스 DB를 운영 DB와 분리 |

현재 포트 배선은 ROS2 브릿지 `9999`, 대시보드·DB·RAG `9998`, 미니맵 탐지
`9091`입니다. RAG 포트를 9999로 바꾸면 ROS2 브릿지와 충돌합니다.

### 지식베이스 DB 분리

기본값은 기존처럼 운영 DB와 지식베이스가 같은 DB를 사용합니다. pgvector를
리눅스 로컬 PostgreSQL에만 설치하려면 다음 항목을 설정합니다.

```bash
export RAG_KB_HOST=localhost
export RAG_KB_PORT=5432
export RAG_KB_NAME=smart_factory_kb
export RAG_KB_USER=postgres
export RAG_KB_PASSWORD='실제 비밀번호'
```

`response_agent.py`, `setup_knowledge_base.py`, `check_pgvector.py`,
`embed_chunks.py`가 모두 같은 `RAG_KB_*` 설정을 사용합니다. 확인 명령은
다음과 같습니다.

```bash
python3 rag/kb_config.py
python3 rag/check_pgvector.py --check
```

## 4. 선택 기능 점검

```bash
# pgvector 컬럼과 인덱스 준비
python3 rag/check_pgvector.py

# BGE-M3 임베딩 적재 및 검색 검증
python3 rag/embed_chunks.py --search

# LLM 서버와 실제 생성 지연시간 확인
python3 rag/check_llm.py
```

LLM 응답이 없거나 형식이 잘못되면 규칙 기반 문장으로 자동 대체합니다.
생성 결과는 공백과 줄바꿈을 정리하고 `VARCHAR(500)` 제약에 맞춰 저장합니다.

## 5. 확신도 해석

| 확신도 | 경로 |
|---|---|
| `0.75~0.95` | 벡터 검색 + LLM 생성 |
| `0.70~0.95` | 벡터 검색 + 규칙 기반 문장 |
| `0.65` | 위험유형 조회 + LLM 생성 |
| `0.60` | 위험유형 조회 + 규칙 기반 문장 |
| `0.45` | 내장 지침 + LLM 생성 |
| `0.40` | DB 연결 불가, 내장 규칙 기반 지침 |

거리 측정 실패 또는 15m를 넘는 비정상 값은 위 값에서 0.15를 뺍니다.
DB 장애 시에도 파이프라인은 내장 지침으로 계속 동작하고, 기본 60초 뒤
지식베이스 연결을 다시 확인합니다.

## 6. 자체 검증

DB와 LLM 서버 없이 실행할 수 있습니다.

```bash
python3 self_test/test_response_agent.py
python3 self_test/test_response_agent_llm.py
python3 self_test/test_response_plan_worker.py
python3 self_test/test_response_throttle.py
python3 self_test/test_integration_wiring.py
python3 pipeline/patrol_pipeline_rag.py --help
```

## 문제 해결

`적재 완료`만 보이고 대응안이 없으면 이벤트의 `risk_level`이 `위험`인지
확인하십시오. 대응안은 위험 등급에서만 생성합니다.

`Address already in use`가 나오면 `run_pipeline.sh`나 다른 RAG 파이프라인이
9998 포트를 이미 사용 중인지 확인하십시오.

LLM을 켠 뒤 탐지 로그가 띄엄띄엄 보이면 `--async-llm`을 함께 사용하십시오.
CPU Ollama의 첫 요청이 시간 초과되면 `LLM_TIMEOUT=180`으로 늘린 뒤 환경설정을
다시 로드하십시오.

확신도 0.40이 계속되면 `python3 rag/kb_config.py`로 대상 DB를 확인하고,
`python3 rag/setup_knowledge_base.py --check`로 테이블 상태를 점검하십시오.

더 자세한 설치 설명은 [`설치가이드_리눅스.md`](설치가이드_리눅스.md), 반영
내역은 [`변경요약.md`](변경요약.md)를 참고하십시오.
