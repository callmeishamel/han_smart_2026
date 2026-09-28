# pipeline — Jetson → PostgreSQL 데이터 적재 파이프라인

프로젝트 전체 배경과 실행 순서는 최상위 [`../README.md`](../README.md)를 참고하세요.
이 폴더는 Jetson에서 UDP로 들어오는 탐지 결과를 받아 DB에 적재하는 두 개의 실행 스크립트를
담고 있습니다.

## 파일 구성

| 파일 | 설명 |
|---|---|
| `jetson_patrol_pipeline.py` | UDP 수신 → 위험도 판정 → `patrol_logs` 적재만 하는 기본 파이프라인 |
| `patrol_pipeline_rag.py` | 위 파이프라인 + 위험 등급 이벤트에 대해 RAG 대응안 생성 → `response_plans` 적재까지 |

**둘 다 같은 UDP 포트(9998)를 리스닝하므로 동시에 실행할 수 없습니다. 하나만 실행하세요.**

## jetson_patrol_pipeline.py

`ai_inference_sender.py`(손준영 파트)가 UDP `:9998`(대시보드/DB용 포트)로 보내는
JSON을 받아 `common.schema.build_detection_event()`로 위험도를 판정하고 `patrol_logs`에
적재합니다.

- **`PatrolLogWriter`**: DB 재연결을 담당하는 얇은 래퍼. `_connect()`에서
  `common.schema.init_all_tables()`를 호출해 테이블이 없으면 생성하고, `write(event)`로
  1건씩 적재합니다. 연결이 끊기면 즉시 죽지 않고 지수 백오프(`MAX_RETRY_BACKOFF_SECONDS=30`
  상한)로 재연결을 시도합니다 — Jetson이 네트워크가 불안정한 현장에 놓일 수 있다는 전제입니다.
- **`iter_udp_detections(zone, udp_ip, udp_port)`**: UDP 소켓에서 payload를 받아
  `DetectionEvent`를 하나씩 `yield`하는 제너레이터. 한 패킷에 `detections` 리스트로 객체
  여러 개가 들어올 수 있습니다.
- 단순 블로킹 루프 구조입니다. 독립 프로세스로 실행하는 구성 자체는 정상이나, 현재는
  DB 재연결도 같은 스레드에서 수행하므로 DB 장애 중 UDP 수신까지 멈추는 제한이 있습니다.
  아래 "현재 남은 실제 문제"를 확인하세요.

```bash
# Jetson 쪽(Linux)에서 실행
export SFP_DB_PASSWORD='실제비밀번호'
python3 jetson_patrol_pipeline.py --zone A --udp-port 9998
```

## patrol_pipeline_rag.py

`jetson_patrol_pipeline.py`를 수정하지 않고, 그 안의 `PatrolLogWriter`와
`iter_udp_detections`를 그대로 import해서 재사용합니다 (적재/UDP 수신 로직이 두 벌로
갈라지면 한쪽만 고쳐지는 문제가 생기므로, `common/schema.py` 도입 배경과 같은 이유).

- **`RagPatrolLogWriter(PatrolLogWriter)`**: `write_response_plan(plan)` 메서드를 추가해
  생성된 `ResponsePlan`을 `response_plans` 테이블에 적재합니다. `reference_docs` 목록은
  줄바꿈으로 이어붙여 TEXT 컬럼에 저장합니다.
- `main()` 흐름: UDP 수신 → `patrol_logs` 적재 → `event.risk_level == "위험"`인 경우에만
  중복 억제를 거쳐 `integration.response_agent.RagResponseAgent.generate_response()` 호출
  → `response_plans` 적재. 주의/정상 등급까지 생성하면 같은 내용이 반복 저장되고 연산
  부담도 커지므로 위험 등급만 대응안을 생성합니다.
- `--use-embedding`: 벡터 유사도 검색 사용 (기본은 위험유형 키워드 조회).
- `--use-llm`: 검색한 근거로 경량 LLM이 대응 지침 문장을 생성(`rag/vllm_client.py`).
  기본은 꺼짐이며, 서버가 없거나 실패하면 규칙 기반 문장으로 조용히 떨어지므로 켜 두어도
  파이프라인이 멈추지 않습니다. 서버 주소는 `LLM_BASE_URL` / `LLM_MODEL`, 생성 제한
  시간은 `LLM_TIMEOUT`으로 지정합니다.
- `--async-llm`: 대응안 생성을 `integration/response_plan_worker.py`의 별도 스레드로
  분리합니다. LLM 생성 중에도 UDP 수신과 `patrol_logs` 적재가 계속됩니다.
- `--plan-queue-size`: 비동기 대응안 큐 크기(기본 32). 포화 시 오래된 대응안 요청을
  버리고 최신 이벤트를 남기며, 탐지 이벤트 자체는 이미 DB에 적재된 상태입니다.
- `--verbose-rag`: 대응안 생성 과정을 콘솔에 자세히 출력.
- `--suppress-seconds`: 대응안 중복 억제 간격 (기본 30초, `0`이면 억제 없음). 아래 참고.
- `--tts`: 생성된 대응안을 젯슨 스피커로 음성 방송. 아래 참고.

```bash
# CPU Ollama 사용 시 권장 실행
./run_pipeline_rag.sh --use-llm --async-llm

# 기존 TTS·중복 억제와 함께 사용 가능
./run_pipeline_rag.sh --use-llm --async-llm --tts --suppress-seconds 30
```

### 음성 경보 (`--tts`)

대응안을 `response_plans`에 적재한 직후, `구역 + 탐지 객체 + 대응안` 문장을 젯슨의
TTS 수신기로 TCP 전송합니다. 자동·수동 방송은 같은 문장 builder를 사용합니다.

```bash
# 젯슨에서 수신기를 먼저 띄운 뒤
./run_pipeline_rag.sh --tts
```

- 송신 모듈은 저장소 루트의 [`TTS Engine and pipeline/`](../../TTS%20Engine%20and%20pipeline/)에서
  자동으로 찾습니다. 폴더를 옮겼다면 `TTS_MODULE_DIR`로 지정하세요.
- **없어도 나머지는 그대로 동작합니다.** 모듈을 못 찾으면 경고만 남기고 음성 경보만
  비활성화됩니다 (`RagResponseAgent`의 지식베이스 폴백과 같은 방식).
- 전송은 항상 **비동기**이며 호출마다 스레드를 만들지 않고 크기 제한 송신 worker를
  사용합니다. 젯슨이 꺼져 있어도 UDP 수신 루프는 멈추지 않으며, 파이프라인 종료 시
  제출·`queued` ACK 성공·실패·드롭 통계를 남깁니다.
- 각 요청에는 `message_id`, 우선순위, 생성·만료시각이 포함됩니다. 젯슨은 같은 ID를
  다시 받으면 중복 재생하지 않고, 기본 20초가 지난 요청은 방송하지 않습니다.
- 재생 대기열은 화재·누출 → 작업자·안전모 → 차량·중장비 → 기타 순서입니다. 포화 시
  낮은 우선순위부터 버리고 같은 등급에서는 오래된 요청을 교체합니다.
- 기본값으로 켜두지 않은 이유: 젯슨 없이 개발용으로 돌릴 때 경보마다 연결 실패
  로그가 쌓이기 때문입니다.
- 방송 빈도는 위 중복 억제(기본 30초)가 그대로 결정합니다. 여기서 따로 억제하지
  않습니다.

### 대응안 중복 억제

젯슨이 30fps로 추론하므로, 위험 상황 하나가 10초만 지속돼도 같은 `(구역, 객체)` 조합의
"위험" 이벤트가 수백 건 들어옵니다. `patrol_logs`는 시계열 기록이라 전부 남겨야 하지만,
대응안은 같은 조합이면 내용이 동일해서 반복 생성하면 `response_plans`가 같은 문장으로
가득 차고(관제 화면에서 정작 다른 위험을 못 보게 됨) 검색·LLM 연산도 낭비됩니다.

`common.schema.ResponsePlanThrottle`이 이를 `(zone, detected_object)` 단위로 억제합니다.

| 동작 | 결과 |
|---|---|
| 같은 조합, 억제 간격 안, 위험도 동일/하향 | 대응안 생성 **건너뜀** (`patrol_logs` 적재는 그대로) |
| 같은 조합, 억제 간격 경과 | 생성 |
| 같은 조합, **위험도 상향** (예: 주의 → 위험) | 간격 무시하고 **즉시 생성** |
| 구역 또는 객체가 다름 | 별개로 관리 |

억제된 이벤트는 기준 시각을 갱신하지 않습니다. 갱신하면 "생성 후 30초"가 아니라
"감지가 끊긴 후 30초"가 되어, 계속 감지되는 물체는 영영 다음 대응안이 나오지 않습니다.
시각은 `time.monotonic()` 기준이라 시스템 시계가 뒤로 점프해도 영향을 받지 않습니다.
상태는 프로세스 메모리에만 있어 재시작하면 초기화됩니다(재시작 직후엔 현재 상태를 한 번
기록하는 편이 안전하므로 의도된 동작).

기본 간격을 바꾸려면 `common/schema.py`의 `RESPONSE_PLAN_SUPPRESS_SECONDS`를,
실행할 때만 바꾸려면 `--suppress-seconds`를 쓰세요. 검증은
[`../self_test/test_response_throttle.py`](../self_test/test_response_throttle.py).

```bash
source ../set_env.sh
python3 patrol_pipeline_rag.py --zone A

# 억제 간격을 60초로 늘리거나, 아예 끄고 싶을 때
python3 patrol_pipeline_rag.py --zone A --suppress-seconds 60
python3 patrol_pipeline_rag.py --zone A --suppress-seconds 0

# 권장: 프로젝트 루트의 실행 스크립트 사용
../run_pipeline_rag.sh
```

억제된 건수는 대응안 생성 로그와 종료 시 요약(`처리 결과: 이벤트 N건 / 대응안 N건 /
억제 N건`)에 함께 표시됩니다. 개별 억제 내역까지 보려면 로그 레벨을 `DEBUG`로 올리세요.

대응안 생성이 필요 없으면 `jetson_patrol_pipeline.py`를 그대로 쓰면 됩니다.

## 환경변수

`common.schema.get_db_config()`가 읽는 `SFP_DB_NAME` / `SFP_DB_USER` / `SFP_DB_PASSWORD` /
`SFP_DB_HOST` / `SFP_DB_PORT`. Jetson(Linux)에서는 `../set_env.example.sh`를 복사해서
`source`로 로드합니다.

## 다른 폴더와의 연동 지점

- `common/schema.py`: `DetectionEvent`, `build_detection_event`, `get_db_config`,
  `init_all_tables` 전부 여기서 가져옵니다.
- `integration/response_agent.py`: `patrol_pipeline_rag.py`가 `RagResponseAgent`를 사용.
- `dashboard/smart_factory_dashboard_v3.py`: 이 파이프라인이 적재한 `patrol_logs`를 조회해서
  화면에 표시합니다.
- `self_test/fake_jetson_sender.py`: `jetson_patrol_pipeline.py`를 Jetson 없이 테스트할 때
  사용하는 가짜 UDP 송신기.

## 이슈 상태

### 해결 완료

- **위험도 오분류 `[해결 완료]`**: `common/schema.py`의 `classify_risk()`가 실제 라벨인
  `person with no helmet`, `fire`, `vehicle`을 인식합니다. 안전모 미착용·화재는 거리와
  무관하게 `위험`, 차량은 거리에 따라 분류되며 `test_schema_logic.py`로 검증합니다.
  상세는 [`../common/README.md`](../common/README.md)와
  [`../docs/판정정책_갱신제안.md`](../docs/판정정책_갱신제안.md)를 참고하세요.
- **RagResponseAgent 영구 폴백 `[해결 완료]`**: 지식베이스 연결 실패 시 builtin 대응안을
  사용하는 것은 유지하되, `KB_RETRY_INTERVAL_SEC` 간격으로 다시 연결합니다. DB가 복구되면
  프로세스 재시작 없이 지식베이스 검색으로 돌아갑니다. 상세는
  [`../integration/README.md`](../integration/README.md)를 참고하세요.

### 현재 남은 실제 문제

- **DB 장애 시 UDP 수신 정지**: `PatrolLogWriter._ensure_connected()`의 무한 재시도와
  `iter_udp_detections()` 소비가 같은 메인 스레드에서 실행됩니다. DB 장애 동안 UDP 소켓을
  읽지 못해 커널 버퍼를 넘긴 패킷이 유실될 수 있습니다. 수신 큐와 DB writer를 별도
  스레드/프로세스로 분리하기 전까지는 DB 가용성을 함께 감시해야 합니다.
- **대응안 실패 후에도 억제 시간 선반영**: `ResponsePlanThrottle.should_generate()`가
  생성 허용과 동시에 내부 시각을 기록합니다. 이후 응답 생성 실패, 비동기 큐 포화 또는
  `response_plans` 저장 실패가 발생해도 같은 경보가 기본 30초 억제될 수 있습니다.
  `--suppress-seconds 0`으로 우회할 수 있으나, 근본적으로는 처리 성공 후 commit하거나
  실패 시 상태를 되돌려야 합니다.
- **UDP JSON 타입 검증 부족**: JSON 문법 오류는 무시하지만, 최상위 값이 객체가 아닌
  `[]`이거나 탐지 항목이 객체가 아닌 `{"detections":[1]}` 같은 패킷은
  `AttributeError`/`TypeError`로 리스너를 종료시킬 수 있습니다. 송신 입력을 신뢰할 수 없는
  환경에서는 상위 방화벽/프록시보다 애플리케이션 스키마 검증 보강이 필요합니다.
- **실행 래퍼의 종료 코드**: `../run_pipeline_rag.sh`는 Python 종료 후 대기용 `read`를
  실행하므로 최종 종료 코드가 Python 결과와 다를 수 있습니다. 서비스·CI에서는
  `python3 pipeline/patrol_pipeline_rag.py ...`를 직접 실행해야 하며, 래퍼는 추후 Python
  종료 코드를 저장해 그대로 반환하도록 수정해야 합니다.
- **실행 권한**: 현재 `../run_pipeline_rag.sh`의 Git 실행 비트가 없어 새 Linux clone에서
  `./run_pipeline_rag.sh`가 거부될 수 있습니다. 반영 전에는
  `bash ./run_pipeline_rag.sh ...` 또는 최초 1회 `chmod +x run_pipeline_rag.sh`를 사용하세요.
