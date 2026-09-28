# integration — 팀 통합 어댑터 & 대응안 생성 에이전트

프로젝트 전체 배경과 실행 순서는 최상위 [`../README.md`](../README.md)를 참고하세요.
이 폴더는 팀원별 파트(비전/디지털트윈/LLM/데이터플랫폼)가 서로 어떤 데이터를 주고받는지에
대한 **인터페이스(Protocol) 정의**와, 그중 실제로 코드가 도착한 파트의 **구현체**를 담고
있습니다.

## 파일 구성

| 파일 | 설명 |
|---|---|
| `team_integration_adapter.py` | 팀원 파트 간 인터페이스(Protocol) 정의 + 비전/데이터플랫폼 실제 구현체 + 오케스트레이터 |
| `response_agent.py` | LLM 에이전트(정진철 파트) 실제 구현체 — RAG 기반 대응안 생성 |
| `response_plan_worker.py` | LLM 대응안 생성을 UDP 수신 루프와 분리하는 비동기 워커 |

## team_integration_adapter.py

### 데이터 계약

- **`ResponsePlan`** (dataclass): `zone`, `source_event`(`DetectionEvent`),
  `recommended_action`, `reference_docs`(리스트), `confidence`. LLM 에이전트가 생성하는
  대응안의 표준 형태.
- `DetectionEvent`는 별도로 정의하지 않고 `common.schema`의 것을 그대로 사용합니다.

### 1. 비전 모듈 어댑터 — 코드 도착, 실제 구현체 있음

- **`VisionModuleAdapter`** (Protocol): `get_next_event() -> Optional[DetectionEvent]`.
- **`NanoOwlUdpVisionAdapter`**: `ai_inference_sender.py`(손준영 파트)가 UDP
  `:9998`(대시보드/DB용 포트)로 보내는 JSON을 수신해 큐에 쌓는 어댑터.
  `start()`로 백그라운드 스레드에서 UDP 수신을 시작하고, `get_next_event()`로 논블로킹
  방식으로 하나씩 꺼냅니다. 큐가 가득 차면(`max_queue_size` 기본 1000) 가장 오래된 항목을
  버리고 최신 항목을 넣습니다(실시간 알림은 backlog보다 fresh가 중요하다는 판단).
  `pipeline/jetson_patrol_pipeline.py`의 단순 블로킹 루프와 달리, 이 어댑터는 여러 컴포넌트
  안에 리스너를 "끼워 넣어야" 하는 경우(예: 오케스트레이터)를 위한 큐+스레드 구조입니다.

### 2. 디지털 트윈 — 이 파일을 거치지 않습니다 [정리 완료]

여기에는 `DigitalTwinAdapter`(Protocol)와 `ZoneLayout`(dataclass)이 있었습니다.
ROS2 토픽에서 구역 좌표/커버리지를 읽어오는 인터페이스로 설계했지만 **구현체도
호출부도 끝내 생기지 않았고**, 그 사이 이다은 파트는 전혀 다른 경로로 연동을
마쳤습니다.

```
젯슨 각도 UDP(:9091)
  → dashboard_link/minimap_renderer.py   (SLAM 지도에 레이캐스팅)
  → HTTP /detections
  → edge_video/event_logger.py
  → DB(detection_events)
```

지도와 로봇 위치는 `minimap_renderer.py`의 `/map_data`·`/state`가 이미 제공하고,
대시보드는 그 페이지를 iframe으로 그대로 띄웁니다. 즉 `ZoneLayout`이 채우려던
자리는 이미 채워져 있고, 어댑터를 구현하면 `/state`가 하는 일을 ROS2로 한 번 더
하는 셈이 됩니다. 그래서 "언젠가 할 일"로 남겨두는 대신 삭제했습니다 — 없는 계획을
있는 것처럼 보이게 하는 쪽이 더 비쌉니다.

> 나중에 "A 구역이 지도 어디인가"가 필요해지면, 이 어댑터가 아니라 `patrol_zones`에
> 구역 경계(폴리곤 또는 사각형)를 넣는 편이 맞습니다. 그건 팀이 구역을 어떻게 나눌지
> 정한 뒤에 할 일입니다.

### 3. LLM 에이전트 어댑터 — 코드 도착, 실제 구현체 있음

- **`ResponseAgentAdapter`** (Protocol): `generate_response(event) -> ResponsePlan`.
  실제 구현체는 이 폴더의 `response_agent.py`의 `RagResponseAgent`(정진철 작성).

### 4. 데이터 플랫폼 어댑터 — 실제 구현체 있음 (이상민 본인 파트)

- **`DataPlatformPort`** (Protocol): `save_event(event)`, `save_response_plan(plan)`.
- **`PostgresDataPlatform`**: `common.schema`의 DDL을 그대로 사용하는 실제 DB 적재
  구현체. `dashboard/smart_factory_dashboard_v3.py`가 조회하는 것과 완전히 같은 테이블에
  씁니다.

### 5. 오케스트레이터

- **`PatrolIntegrationOrchestrator`**: 비전 → DB 적재 → (위험 등급일 때만) LLM 대응안 생성
  → DB 저장까지 조율. `process_once()`가 이벤트 하나를 처리합니다.
  대응안 생성 앞에는 `common.schema.ResponsePlanThrottle`(기본 30초)이 걸려 있어,
  같은 `(구역, 객체)` 조합이 반복 감지돼도 대응안은 간격당 한 번만 만듭니다
  (위험도가 올라가면 간격과 무관하게 통과). `patrol_logs` 적재는 억제하지 않으므로
  시계열 기록은 그대로 남습니다. 억제를 끄려면 `suppress_seconds=0`,
  간격만 바꾸려면 `common/schema.py`의 `RESPONSE_PLAN_SUPPRESS_SECONDS`를 고치세요.
  억제된 건수는 `orchestrator.suppressed_count`로 확인할 수 있습니다.
  (`get_zone_context()`는 위 2번과 함께 삭제했습니다 — 항상 빈 dict를 돌려주던
  메서드였습니다.)

### 6. Mock 구현체

`_MockVisionAdapter`, `_MockDataPlatform`, `_MockResponseAgent` — 실제 어댑터 없이 통합
로직만 테스트할 때 사용. `_build_data_platform()`은 DB 연결을 미리 확인해서, 성공하면
`PostgresDataPlatform`, 실패하면(현장에 DB가 아직 없는 경우 등) 자동으로
`_MockDataPlatform`(콘솔 출력)으로 대체합니다.

### 단독 실행

```powershell
python integration\team_integration_adapter.py
```

`NanoOwlUdpVisionAdapter`(실제)를 `_MockResponseAgent`(mock)와 조합해서, DB가 없어도
콘솔 출력으로 전체 배선이 동작하는지 확인할 수 있습니다.

## response_agent.py

`RagResponseAgent`: `risk_level == "위험"`인 `DetectionEvent`를 받아 산업안전 지식베이스에서
근거를 검색하고 `ResponsePlan`을 생성합니다. `ResponseAgentAdapter` Protocol을 만족하므로
`team_integration_adapter.py`의 다른 코드 수정 없이 `_MockResponseAgent` 자리에 그대로
끼워 넣을 수 있습니다.

### 동작 경로 3가지 (어떤 상황에서도 예외 없이 ResponsePlan을 반환)

| 경로 | 조건 | 확신도 |
|---|---|---|
| `vector` | 지식베이스 + 임베딩 모델(`use_embedding=True`)로 유사도 검색 성공 | 0.50~0.95 |
| `keyword` | 지식베이스는 있지만 벡터 검색 미사용/실패 — 위험유형 기준 조회 | 0.60 |
| `builtin` | 지식베이스 자체에 연결 불가 — 내장 표준 지침(`BUILTIN_GUIDELINES`) 사용 | 0.40 |

기본값은 `keyword` 경로(`use_embedding=False`)입니다. 엣지 보드에서 임베딩 모델을 상주시키면
비전 추론과 메모리를 다투므로 기본 구성에서는 사용하지 않습니다.

### 핵심 구성

- **`RISK_TYPE_MAP`**: 감지 객체명(영문 원문 + 한글 변환명 모두 키로 등록) → 위험유형
  (`PPE`, `fire`, `machinery`, `chemical`, `access`, `loto`, `fall`, `general`) 매핑.
  `person with no helmet` → `PPE`, `fire` → `fire`, `vehicle` → `machinery`가 현재 탐지
  클래스 기준으로 이미 등록되어 있습니다(구버전 라벨 `hazardous leak`/`obstacle`/`human`도
  과거 로그 조회 대응용으로 남아 있음).
- **`resolve_risk_type(detected_object)`**: 매핑에 없으면 `general` 반환 (대소문자/공백
  차이도 보정).
- **`BUILTIN_GUIDELINES`**: 위험유형별 (대응 절차, 근거 조항 목록) 내장 지침. 지식베이스가
  없어도 통합 테스트가 환경 문제로 중단되지 않게 하는 폴백입니다.
- **`_kb_available()`**: `safety_chunks` 테이블 존재 여부를 확인합니다. **성공은 영구
  캐시하지만 실패는 `KB_RETRY_INTERVAL_SEC`(기본 60초) 뒤에 다시 확인**합니다 — 아래
  "해결된 이슈" 참고.
- **`generate_response(event)`**: 위험유형 판정 → 지식베이스 가용 시 벡터/키워드 검색 →
  근거 결합 → 확신도 산정 → `MAX_ACTION_LEN=500`(`response_plans.recommended_action`
  컬럼 제약)에 맞춰 텍스트를 자르고 `ResponsePlan` 반환. 거리 측정 실패
  (`distance < 0` 또는 `> 15.0`)인 이벤트는 확신도를 0.15 낮추고 근거에 "거리 측정 실패 —
  현장 확인 필요"를 추가합니다.

### 단독 실행

```bash
python3 response_agent.py
```

## 환경변수

`common.schema.get_db_config()`를 통해 `SFP_DB_*`(지식베이스 접속용, `RagResponseAgent`의
`db_config` 인자를 생략하면 이걸 사용). `NanoOwlUdpVisionAdapter`는 코드 내 기본값
(`udp_port=9998`)을 그대로 씁니다. 바인딩 주소는 `PIPELINE_UDP_BIND`(기본 `0.0.0.0`)
입니다 — 예전 기본값 `127.0.0.1`은 같은 PC에서 보낸 패킷만 받아서, 다른 기기인
젯슨의 패킷이 하나도 도착하지 않았습니다.

## 해결된 이슈

### `_kb_available()`의 영구 캐시 버그 [적용 완료]

지식베이스 확인에 한 번 실패하면 `self._kb_ready`가 `False`로 **영구** 캐시되어,
그 프로세스가 살아있는 동안 다시는 지식베이스를 보지 않았습니다.

문제는 이게 예외적인 상황이 아니라는 점입니다. **파이프라인은 보통 PostgreSQL보다
먼저 뜨므로 첫 확인은 거의 항상 실패합니다.** 그러면 DB가 정상화된 뒤에도 모든
대응안이 `builtin`(확신도 0.4) 폴백으로만 나옵니다. 대시보드가 확신도를 화면에
표시하게 되면서(v7), **모든 대응안이 40%로 찍히는** 형태로 눈에 띕니다.

대시보드 v2가 `init_db()`에서 겪었던 "실패 결과를 캐싱해서 영구히 실패 상태로
남는" 버그와 정확히 같은 것입니다 — 같은 폴더 다른 파일에서 재발했습니다.

성공은 그대로 영구 캐시하되, **실패는 `KB_RETRY_INTERVAL_SEC`(기본 60초) 뒤에 다시
확인**하도록 고쳤습니다. `_get_conn()`은 원래부터 실패를 캐시하지 않고 매번
재시도하므로, 이 한 곳만 고치면 됩니다.

### 위험 등급 오분류로 인한 대응안 미생성 [적용 완료]

`classify_risk()`가 실제 탐지 라벨(`person with no helmet`, `vehicle`)을 "위험"으로
판정하지 못해 이 에이전트가 호출될 기회 자체가 없었습니다. `DISTANCE_BASED_OBJECTS`/
`ALWAYS_DANGER_OBJECTS` 집합 도입으로 해결했습니다. 상세는
[`../common/README.md`](../common/README.md) 참고.

### 죽은 코드였던 LLM 클라이언트 연결 [적용 완료]

`rag/vllm_client.py`는 프롬프트 설계까지 끝난 완성된 클라이언트인데 **파이프라인
어디서도 호출되지 않았습니다.** 그 파일 상단 주석이 이미 "호출하는 쪽
(`integration/response_agent.py`)"이라고 적어 두었는데도 그 배선만 빠져 있었습니다.
그동안 "RAG 대응안"은 사실 **검색 + 문장 템플릿**이었고 생성 단계가 없었습니다.

이제 `RagResponseAgent(use_llm=True)` 또는 파이프라인의 `--use-llm`으로 켭니다.

```bash
./run_pipeline_rag.sh --use-llm --async-llm           # CPU 환경 권장
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=gemma2:2b \
  LLM_TIMEOUT=180 ./run_pipeline_rag.sh --use-llm --async-llm  # Ollama
```

- **기본값은 꺼짐입니다.** 켜지 않으면 동작이 예전과 완전히 같습니다.
- 검색 근거가 없을 때도 이벤트와 내장 지침을 바탕으로 호출할 수 있습니다.
- 서버가 없거나, 응답이 비었거나, 예외가 나면 **조용히 규칙 기반 문장으로 떨어집니다.**
  안전 파이프라인이 LLM 서버 때문에 멈추면 안 되기 때문입니다.
- LLM 생성에 성공하면 해당 검색 경로의 확신도에 0.05를 더하되 최대 0.95로 제한합니다.
- 응답 키 누락·비정상 형식·500자 초과·여러 줄 출력은 에이전트에서 방어적으로 처리합니다.
- 서버 주소는 `LLM_BASE_URL` / `LLM_MODEL`. `vllm_client.py`가 단독 실행될 때도 **같은
  환경변수**를 읽습니다 — 한쪽만 바꿔서 "단독 실행은 되는데 파이프라인은 다른 서버를
  본다"가 되지 않도록 했습니다.
- 생성 제한 시간은 `LLM_TIMEOUT`으로 전달합니다. CPU Ollama는 180초를 권장합니다.

### 비동기 대응안 워커 [적용 완료]

`--async-llm`을 켜면 `ResponsePlanWorker`가 대응안 생성을 전용 스레드에서
처리합니다. 메인 수신 루프와 워커는 `RagResponseAgent` 및 DB 커넥션을 공유하지
않습니다. 중복 억제는 큐 투입 전에 적용되며, 종료 시 남은 작업을 제한 시간 동안
드레인한 뒤 생성·적재·드롭·실패 통계를 남깁니다.

`self_test/test_response_agent.py`의 6번 블록이 서버 없는 환경에서 `use_llm=True`로
돌려, 폴백이 실제로 도는지(규칙 기반과 같은 결과가 나오는지) 확인합니다.

## 알려진 이슈

특별히 알려진 이슈는 없습니다.

## 다른 폴더와의 연동 지점

- `common/schema.py`: `DetectionEvent`, `build_detection_event`, `get_db_config`,
  `init_all_tables` 전부 여기서 가져옵니다.
- `pipeline/patrol_pipeline_rag.py`: `response_agent.RagResponseAgent`를 import해서 사용.
- `rag/setup_knowledge_base.py`: `RagResponseAgent`가 조회하는 `safety_chunks`/
  `safety_documents` 테이블을 최초 1회 구성.
- `self_test/test_response_agent.py`: `RagResponseAgent`, `resolve_risk_type`,
  `MAX_ACTION_LEN`을 DB 없이 검증.
