# admin — DB 관리 CLI

프로젝트 전체 배경과 실행 순서는 최상위 [`../README.md`](../README.md)를 참고하세요.

## 파일 구성

| 파일 | 설명 |
|---|---|
| `manage_smart_factory_db.py` | 구역(zone) CRUD + 최근 감지 로그 조회용 터미널 CLI |

## manage_smart_factory_db.py

`patrol_zones`/`patrol_logs` 테이블을 다루는 메뉴 기반 터미널 프로그램(v2)입니다.
파일 상단 주석에 v1 대비 수정 내역이 정리되어 있습니다.

### v1 대비 변경 사항

1. **스키마 불일치 수정**: 원본 v1은 `patrol_zones`를 `zone_name`/`risk_level`/
   `last_patrol_time`/`status`/`robot_id` 컬럼으로 가정했지만, 실제 대시보드/파이프라인이
   쓰는 `patrol_zones`는 `zone`(PK)/`robot_id`/`description`입니다. v1 그대로 두면 이
   CLI로 넣은 데이터가 대시보드에 전혀 반영되지 않는 치명적 버그가 있었습니다. v2는
   `common/schema.py`의 실제 스키마를 그대로 사용합니다.
2. **비밀번호 하드코딩 제거**: DB 접속 정보를 `common.schema.get_db_config()`(환경변수
   기반)로 통일.
3. **기능 확장**: 구역 CRUD뿐 아니라 "최근 로그 조회" 메뉴를 추가.

### 핵심 함수

- **`ensure_schema()`**: 시작 시 `common.schema.init_all_tables()`를 호출해 테이블이 없으면
  생성. 실패해도 경고만 출력하고 메뉴는 계속 진행됩니다.
- **`read_zones()`**: `patrol_zones` 전체를 `zone` 기준 정렬해서 출력.
- **`insert_zone()`**: 구역 코드/로봇 ID/설명을 입력받아 INSERT. 중복 코드는 UNIQUE 제약
  위반 메시지로 안내(psycopg2 예외를 이름으로 잡지 않고 메시지로 안내하는 이유는
  psycopg2 미설치 환경에서도 이 파일이 import 시점에 깨지지 않도록 하기 위함).
- **`delete_zone()`**: 구역 삭제. 해당 구역을 참조하는 `patrol_logs` 기록이 있으면 FK
  제약으로 실패할 수 있다는 안내 메시지를 함께 출력.
- **`read_recent_logs(limit=20)`**: `patrol_logs`에서 최근 로그를 `log_time DESC`로 조회,
  거리값이 음수면 "측정불가"로 표시.
- **`reset_demo_data()`**: 순찰 기록 3개 테이블을 통째로 비웁니다. **`초기화`를 그대로
  타이핑해야** 진행되고, 구역 정의와 RAG 지식베이스는 남습니다.
- **`purge_logs()`**: 지정한 일수보다 오래된 기록만 삭제합니다(기본 7일).
- **`main_menu()`**: 1~7번 메뉴를 반복 표시하는 루프.

### 데이터 초기화 — 왜 자동이 아니라 메뉴인가

"프로그램 켤 때마다 DB를 초기화하면 되지 않나"는 자연스러운 발상이지만, 이 구조에서는
위험합니다. `init_all_tables()`를 호출하는 진입점이 **5개**입니다.

```
admin/manage_smart_factory_db.py      ensure_schema()
dashboard/smart_factory_dashboard_v3.py   init_db()
pipeline/jetson_patrol_pipeline.py    PatrolLogWriter._connect()
integration/team_integration_adapter.py
edge_video/event_logger.py            ensure_table()
```

여기서 데이터를 비우면 두 가지가 깨집니다.

1. **나중에 뜬 프로세스가 먼저 뜬 프로세스의 기록을 지웁니다.** 파이프라인이 10분간
   모은 로그를, 대시보드를 여는 순간 날려버립니다.
2. **`PatrolLogWriter._connect()`는 시작할 때만이 아니라 DB에 재연결할 때마다
   호출됩니다.** 시연 도중 네트워크가 한 번만 끊겨도 그때까지의 로그가 사라집니다.

게다가 `patrol_logs`는 안전 사고 기록입니다. 매 실행마다 지우면 데이터 플랫폼이 아니라
임시 버퍼가 됩니다.

그래서 **스키마 초기화(`CREATE TABLE IF NOT EXISTS`)는 지금처럼 매번 자동으로** 하되,
**데이터 삭제는 사람이 명시적으로 부를 때만** 하도록 나눴습니다.

| 상황 | 방법 |
|---|---|
| 시연 직전에 화면을 깨끗하게 | 메뉴 **5. 순찰 기록 전체 초기화** |
| 판정 정책을 바꾼 뒤 옛 기준 로그 정리 | 메뉴 **5**, 또는 `docs/판정정책_갱신제안.md` §7.1의 재분류 SQL |
| 테이블이 계속 커지는 것만 막고 싶음 | 메뉴 **6. 오래된 기록만 정리** (기본 7일) |
| 스키마만 맞추고 데이터는 유지 | 아무것도 안 해도 됨 (모든 진입점이 자동 처리) |

실제 삭제 로직은 `common/schema.py`의 `reset_operational_data()` /
`purge_old_logs()`에 있어, CLI 없이 스크립트에서도 호출할 수 있습니다.

### 실행 방법

```powershell
copy ..\set_env.example.ps1 ..\set_env.ps1   # 최초 1회, 이미 했다면 생략
. ..\set_env.ps1
python admin\manage_smart_factory_db.py
```

## 환경변수

`common.schema.get_db_config()`를 통해 `SFP_DB_NAME` / `SFP_DB_USER` / `SFP_DB_PASSWORD` /
`SFP_DB_HOST` / `SFP_DB_PORT`.

## 다른 폴더와의 연동 지점

- `common/schema.py`: `get_db_config`, `init_all_tables`, `reset_operational_data`,
  `purge_old_logs`, `RESET_TABLES` import. 이 CLI가 보는 테이블은
  `pipeline`/`integration`/`dashboard`가 쓰는 것과 완전히 같은 스키마입니다(과거 v1의
  스키마 불일치 버그가 이걸로 해결됨).
- `pipeline/jetson_patrol_pipeline.py`(또는 `patrol_pipeline_rag.py`)가 적재한
  `patrol_logs`를 이 CLI의 "최근 로그 조회" 메뉴로 확인할 수 있습니다.

## 알려진 이슈

특별히 알려진 이슈는 없습니다. 다만 `response_plans`/`detection_events` 테이블 조회 메뉴는
아직 없습니다(구역 CRUD와 `patrol_logs` 조회만 지원).
