# common — 공통 스키마 모듈

프로젝트 전체 실행 순서와 배경은 최상위 [`../README.md`](../README.md)를 참고하세요.
이 폴더는 `dashboard`, `pipeline`, `integration`, `admin`, `edge_video`가 전부 import해서
쓰는 **단일 진실 공급원(single source of truth)**입니다.

## 파일 구성

| 파일 | 설명 |
|---|---|
| `schema.py` | DB 접속 설정, 테이블 DDL, `DetectionEvent` 데이터 계약, 위험도 판정 로직, 대응안 중복 억제 정책 |
| `console.py` | Windows 콘솔 출력 인코딩 보정 (`enable_utf8_console`) |
| `__init__.py` | 빈 파일 (패키지 마커) |

### `console.py` — 왜 필요한가

Windows 기본 콘솔은 cp949 라서 이 저장소가 로그에 쓰는 이모지(📤 ⚠ 🤖)와
em 대시(—)를 인코딩하지 못합니다. `print()` 한 줄에서 `UnicodeEncodeError`가
나고 **프로세스가 그대로 죽습니다.** 실제로 DB 관리 CLI가 메뉴를 띄우기도 전에
죽었고, 자체 테스트 5종도 로직은 전부 통과하면서 출력 단계에서 크래시했습니다.

그래서 직접 실행되는 스크립트는 맨 위에서 한 번 불러줍니다.

```python
from common.console import enable_utf8_console
enable_utf8_console()
```

라이브러리 모듈에서는 부르지 마세요 — import 만으로 전역 상태가 바뀌면 안 됩니다.
`TTS Engine and pipeline/`은 이 트리에 의존하지 않으므로 같은 처리를 직접 합니다.

## 왜 이 파일 하나로 통일했는가

`schema.py` 상단 주석에 남아 있듯, 원래는 아래 세 가지 문제가 있었습니다.

1. `manage_smart_factory_db.py`(관리 CLI)와 대시보드가 `patrol_zones`를 서로 다른 컬럼
   구성으로 가정 — CLI로 넣은 데이터가 대시보드에 반영되지 않는 버그.
2. `DetectionEvent`와 위험도 판정 로직(`classify_risk`)이 파이프라인/통합 어댑터에 각자
   따로 정의되어 있어, 임계값을 바꾸면 한쪽만 반영되는 "논리 drift" 위험.
3. DB 비밀번호가 코드에 평문 하드코딩.

이 세 문제를 해결하기 위해 DDL·데이터 계약·판정 로직·DB 접속 설정을 이 파일 하나로
모았습니다. **위험도 판정 기준을 바꾸고 싶으면 이 파일 하나만 고치면 됩니다.**

## 핵심 함수/클래스

- **`get_db_config() -> dict`**: `SFP_DB_NAME` / `SFP_DB_USER` / `SFP_DB_PASSWORD` /
  `SFP_DB_HOST` / `SFP_DB_PORT` 환경변수를 읽어 접속 정보를 반환합니다 (기본값:
  `smart_factory_db` / `postgres` / 빈 문자열 / `localhost` / `5432`). Streamlit 대시보드는
  `st.secrets`를 따로 쓰므로 이 함수를 호출하지 않습니다 — pipeline / integration / admin /
  edge_video처럼 Streamlit 밖에서 도는 스크립트 전용입니다.
- **테이블 DDL**: `ZONE_TABLE_DDL`(patrol_zones), `LOG_TABLE_DDL`(patrol_logs),
  `RESPONSE_PLAN_TABLE_DDL`(response_plans), `DETECTION_EVENTS_TABLE_DDL`(detection_events),
  `MIGRATE_LOG_TABLE_MAP_COLUMNS_DDL`(기존 patrol_logs에 map_x/map_y 컬럼 추가, 멱등).
  네 개를 묶은 목록이 `ALL_TABLE_DDL`.
- **`init_all_tables(cursor)`**: 위 4개 테이블 생성 + 인덱스 + 마이그레이션 +
  `SEED_ZONES_SQL`(A/B/C 구역 기본 시드)까지 한 번에 실행합니다. `commit()`은 호출자
  책임입니다. **데이터는 지우지 않습니다** — 진입점 5개가 각자 호출하고 그중
  `PatrolLogWriter._connect()`는 재연결마다 호출되므로, 여기서 비우면 서로의 기록을
  지우게 됩니다. 자세한 이유는 [`../admin/README.md`](../admin/README.md) 참고.
- **적재 SQL**: `INSERT_PATROL_LOG_SQL` + `patrol_log_params(event)`,
  `INSERT_RESPONSE_PLAN_SQL` + `response_plan_params(plan)`,
  `INSERT_DETECTION_EVENT_SQL` + `detection_event_params(session_id, obj)`.
  아래 "INSERT 문을 여기로 모은 이유" 참고.
- **`reset_operational_data(cursor)`**: `RESET_TABLES`(`patrol_logs`, `response_plans`,
  `detection_events`)를 `TRUNCATE ... RESTART IDENTITY`로 비웁니다. 구역 정의와 RAG
  지식베이스는 남습니다. 시연 직전 정리용이며, 관리 CLI 메뉴 5번이 이걸 호출합니다.
- **`purge_old_logs(cursor, days=7)`**: 지정 일수보다 오래된 기록만 삭제하고 테이블별
  삭제 건수를 돌려줍니다. 초기화 없이 테이블 크기를 일정하게 유지하는 용도입니다.
  `patrol_logs`/`response_plans`는 `LOCALTIMESTAMP`, `detection_events`는 컬럼이
  `TIMESTAMPTZ`라 `now()` 기준으로 비교합니다(섞으면 세션 시간대만큼 경계가 밀립니다).
- **`DetectionEvent`** (dataclass): `zone`, `detected_object`, `distance`, `box_position`,
  `risk_level`('정상'|'주의'|'위험'), `issue`, 그리고 선택 필드 `map_x`/`map_y`. NanoOWL UDP
  경로(손준영 파트)는 `map_x`/`map_y`가 `None`이고, SLAM 경로(이다은 파트)는 이 값이 채워집니다.
- **`classify_risk(object_name, distance_m) -> (risk_level, issue)`**: 세 집합으로 판정합니다.

  | 집합 | 대상 | 판정 |
  |---|---|---|
  | `ALWAYS_DANGER_OBJECTS` | `fire`, `hazardous leak`, `person with no helmet` | 거리 무관 **위험** |
  | `ALWAYS_CAUTION_OBJECTS` | `obstacle` | 거리 무관 **주의** |
  | `DISTANCE_BASED_OBJECTS` | `human`, `vehicle` | 1.5m 미만 위험 / 3.0m 미만 주의 / 그 이상 정상 |

  그 외 객체는 `"주의", "미분류 객체 감지: {object_name}"`로 떨어집니다. 비전 모듈의
  탐지 클래스가 바뀌면 함수가 아니라 **이 집합 상수만** 고치면 됩니다.
- **`is_distance_valid(distance_m) -> bool`**: 시차 계산 실패값(`-1.0`)과 실내 공장에서
  성립하지 않는 이상치(`MAX_VALID_DISTANCE_M=15.0` 초과)를 함께 걸러냅니다. 거리 기반
  판정은 이 검사를 통과한 값에만 적용되고, 실패하면 보수적으로 "주의 (거리 미상)"입니다.
- **`build_detection_event(zone, det)`**: NanoOWL UDP payload
  (`{"object": ..., "bbox": [...], "distance_meter": ...}`) 1건을 `DetectionEvent`로 변환.
  `classify_risk()`를 내부에서 호출합니다.
- **`build_map_detection_event(zone, tracked_obj)`**: `minimap_renderer.py`의 `/detections`
  응답(`{"id", "label", "x", "y", "hit_count", "last_seen"}`) 1건을 `DetectionEvent`로 변환.
  실제 거리 대신 지도 좌표를 다루므로 `classify_risk()`를 항상 "거리 미상(-1)"으로 호출합니다
  — 위험도 판정의 1차 기준은 여전히 NanoOWL 쪽(`build_detection_event`) 흐름입니다.
- **`ResponsePlanThrottle(suppress_seconds, time_func)`**: 대응안 중복 생성 억제기.
  `should_generate(event) -> bool`로 판단하며, `(zone, detected_object)` 조합이 같고
  `RESPONSE_PLAN_SUPPRESS_SECONDS`(기본 30초) 안이면 `False`를 돌려줍니다. 단 **위험도가
  직전보다 올라가면 간격을 무시하고 통과**시킵니다(상황 악화 순간을 놓치면 억제가 사고
  원인이 되므로). `True`를 반환할 때만 기준 시각을 갱신해서, "생성 후 30초"가 "감지가
  끊긴 후 30초"로 변질되지 않게 합니다. 시각은 `time.monotonic()` 기준이고 상태는 프로세스
  메모리에만 있습니다. **대응안을 만드는 두 경로가 모두 이걸 씁니다** —
  `pipeline/patrol_pipeline_rag.py`와 `integration/team_integration_adapter.py`의
  `PatrolIntegrationOrchestrator`. 예전에는 파이프라인에만 있어서, 어댑터에 실제
  에이전트를 꽂으면 억제가 통째로 빠지는 구조였습니다(이 모듈을 만들어 없애려던
  바로 그 "정책이 한쪽에만 반영되는" 패턴이라 양쪽을 맞췄습니다).
- **`risk_rank(risk_level) -> int`**: `RISK_ORDER`(정상 0 / 주의 1 / 위험 2) 기준 심각도
  순위. 모르는 등급은 0으로 취급합니다 — 억제 판단의 "상향 시 예외" 조건이 과도하게
  열리지 않도록 하기 위함입니다.

## INSERT 문을 여기로 모은 이유 — 실제로 데이터를 잃었습니다

예전에는 INSERT 문이 적재하는 쪽마다 따로 있었습니다.

| 테이블 | 어디에 있었나 |
|---|---|
| `patrol_logs` | `pipeline/jetson_patrol_pipeline.py` + `integration/team_integration_adapter.py` |
| `response_plans` | `pipeline/patrol_pipeline_rag.py` + `integration/team_integration_adapter.py` |
| `detection_events` | `edge_video/event_logger.py` |

**`patrol_logs`에 `map_x`/`map_y` 컬럼을 추가했는데, 두 INSERT 문 어느 쪽도 그 컬럼을
쓰지 않아서 계속 NULL이었습니다.** 컬럼도 있고(`MIGRATE_LOG_TABLE_MAP_COLUMNS_DDL`),
`DetectionEvent`에 필드도 있고, `build_map_detection_event()`가 값을 채우고,
`test_schema_logic.py`가 그 값을 검증까지 하는데 — 저장만 안 됐습니다. 컬럼을 늘릴 때
INSERT 두 곳을 같이 고쳐야 한다는 걸 아무도 기억하지 못한 겁니다.

`response_plans` 쪽도 조용히 갈라져 있었습니다. 한쪽은 `plan.reference_docs or []`인데
다른 쪽은 `plan.reference_docs`라, 근거를 못 찾은 대응안이 오면 **한쪽만 `TypeError`로
죽었습니다.**

그래서 SQL과 파라미터 조립을 DDL 바로 옆으로 옮겼습니다. 컬럼을 바꾸면 이 블록만
고치면 되고, 적재하는 쪽은 전부 자동으로 따라옵니다.
`self_test/test_integration_wiring.py`가 **`schema.py` 밖에 `INSERT INTO` 문이 다시
생기면 실패**하도록 검사합니다.

## detection_events 중복 방지 — `session_id`

`event_logger.py`의 메모리 캐시(`seen`)가 유일한 방어선이던 시절, 세 가지 상황에서
조용히 틀렸습니다.

| 상황 | 예전 결과 |
|---|---|
| 이벤트 로거 재시작 | 캐시가 비어서, 미니맵이 아직 들고 있는 물체를 전부 다시 적재 (중복 행) |
| **미니맵 재시작** | tracker id가 1부터 다시 시작하는데 캐시에 옛 1번이 남아 있어 **새로 잡힌 물체를 통째로 버림 (기록 누락)** |
| `MAX_SEEN_IDS` 초과 | 오래된 절반을 버려서, 아직 살아 있는 물체를 다시 적재 (중복 행) |

가운데 항목이 가장 위험합니다 — 중복은 보기 싫을 뿐이지만, 저건 안전 기록이 사라지는
것입니다.

`minimap_renderer.py`가 프로세스마다 새로 만드는 `SESSION_ID`를 `/detections` 응답에
함께 실어 보내고, `event_logger.py`는 `(session_id, tracker_id)`로 판단합니다.

- `UNIQUE_INDEX_DDL`: `uq_detection_events_session_tracker` — `(session_id, tracker_id)`
  유니크. **`WHERE session_id <> ''` 부분 인덱스**입니다. 이 컬럼이 생기기 전에 쌓인 행은
  전부 `''`이고 중복이 있을 수 있는데, 그걸 대상으로 유니크 인덱스를 만들면 생성 자체가
  실패하기 때문입니다. 구버전 미니맵(session 미전송)과 붙었을 때도 적재가 거부되지 않고
  예전 동작 그대로 굴러갑니다.
- `INSERT_DETECTION_EVENT_SQL`의 `ON CONFLICT DO NOTHING`이 실제 방어선입니다. 이제
  메모리 캐시는 **DB 왕복을 아끼는 최적화일 뿐**이라, 캐시가 비어 있어도 넘쳐서
  버려져도 중복 행이 생기지 않습니다.
- **`_try_create_unique_index(cursor)`**: 인덱스 생성이 실패해도 나머지 초기화를 막지
  않습니다. `init_all_tables()`는 진입점 5곳이 시작할 때마다 부르는 함수라, 여기서
  예외가 나면 대시보드도 파이프라인도 뜨지 않습니다. `autocommit`이 꺼진 커넥션에서는
  실패한 문장이 트랜잭션 전체를 오염시키므로 세이브포인트로 격리합니다.

## 인덱스

`INDEX_DDL`이 `init_all_tables()`에서 함께 실행됩니다. 전부 `IF NOT EXISTS`라 이미
운영 중인 DB에 적용해도 인덱스만 새로 생깁니다.

| 인덱스 | 대상 | 쓰는 곳 |
|---|---|---|
| `idx_patrol_logs_zone_time` | `patrol_logs (zone, log_time DESC)` | 구역별 최신 1건, 구역 상세 로그 |
| `idx_patrol_logs_risk_time` | `patrol_logs (risk_level, log_time DESC)` | 최근 N분 위험 건수 |
| `idx_patrol_logs_time` | `patrol_logs (log_time DESC)` | 전 구역 통합 조회 (대시보드 상세 조회의 '전체') |
| `uq_detection_events_session_tracker` | `detection_events (session_id, tracker_id)` **UNIQUE**, `session_id <> ''` | 중복 적재 차단 (위 항목 참고) |
| `idx_detection_events_detected_at` | `detection_events (detected_at DESC)` | 시간순 조회 |
| `idx_response_plans_created` | `response_plans (created_at DESC)` | 대응안 최신순 조회 |
| `idx_response_plans_zone_created` | `response_plans (zone, created_at DESC)` | 구역별 대응안 이력 |

젯슨이 30fps로 추론하므로 `patrol_logs`는 매우 빠르게 커지는데, 대시보드는 1초마다
이 테이블을 조회합니다(브라우저 탭 수만큼 곱해집니다). 인덱스가 없으면 조회할 때마다
테이블 전체를 훑어서, 로그가 쌓일수록 대시보드가 점점 느려지다가 갱신 주기를 못
따라갑니다.

`idx_patrol_logs_time`이 `idx_patrol_logs_zone_time`과 별도로 필요한 이유: 복합
인덱스 `(zone, log_time DESC)`는 **선두 컬럼인 `zone`을 지정했을 때만** 정렬에 쓸 수
있습니다. 대시보드 상세 조회의 기본값인 '전체 구역'은 `WHERE` 없이
`ORDER BY log_time DESC LIMIT N`을 돌리므로 저 인덱스를 못 타고 테이블 전체를 훑습니다.

## 해결된 이슈 — 탐지 라벨 불일치 [적용 완료]

`classify_risk()`가 `human` 문자열 하나만 거리 기반으로 처리하던 시절, 비전 모듈의
탐지 클래스가 `["person with no helmet", "fire", "vehicle"]`로 바뀌면서 세 클래스 중
둘이 `"주의", "미분류 객체 감지: ..."`로 빠졌습니다. 파이프라인은 `risk_level == "위험"`
일 때만 대응안을 만들기 때문에, **안전모 미착용 상황에서 대응안이 생성되지 않았습니다.**

`DISTANCE_BASED_OBJECTS` 집합 도입과 `OBJECT_KO_NAME` 갱신으로 해결했습니다.
경위와 검증 방법은 [`../docs/판정정책_갱신제안.md`](../docs/판정정책_갱신제안.md) 참고.

```bash
python self_test/test_schema_logic.py     # 판정 정책 검증
python self_test/test_response_agent.py   # 5번 항목이 '안전모 미착용' / '위험' 이면 정상
```

## 다른 폴더와의 연동 지점

- `pipeline/jetson_patrol_pipeline.py`, `pipeline/patrol_pipeline_rag.py`:
  `get_db_config`, `init_all_tables`, `DetectionEvent`, `build_detection_event` import.
- `integration/team_integration_adapter.py`, `integration/response_agent.py`:
  `get_db_config`, `init_all_tables`, `DetectionEvent`, `build_detection_event` import.
- `dashboard/smart_factory_dashboard_v3.py`: `init_all_tables`만 import (DB 접속 정보는
  `st.secrets`로 별도 처리).
- `admin/manage_smart_factory_db.py`: `get_db_config`, `init_all_tables` import.
- `edge_video/event_logger.py`: `get_db_config`, `DETECTION_EVENTS_TABLE_DDL` import.
- `self_test/test_schema_logic.py`: `classify_risk`, `build_detection_event`,
  `build_map_detection_event`의 로직만 DB/네트워크 없이 검증.
- `self_test/test_response_throttle.py`: `ResponsePlanThrottle`, `risk_rank`를 가짜 시계로
  검증 (실제로 30초를 기다리지 않음).

## 실행 방법

이 폴더 자체에는 실행 진입점이 없습니다(라이브러리 모듈). 로직만 확인하려면:

```powershell
python self_test\test_schema_logic.py
python self_test\test_response_throttle.py
```
