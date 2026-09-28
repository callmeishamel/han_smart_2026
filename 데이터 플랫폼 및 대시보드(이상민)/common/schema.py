"""
smart_factory_project 공통 스키마 모듈 (common/schema.py)

왜 이 파일이 필요한가
----------------------
지금까지 작업하면서 아래와 같은 "같은 것이 여러 곳에서 서로 다르게
정의되는" 문제가 반복적으로 발견됐습니다.

1. `manage_smart_factory_db.py`(팀원이 준 CLI 관리 스크립트)는
   `patrol_zones` 테이블을 zone_name/robot_id/status/risk_level 컬럼으로
   가정했지만, 대시보드는 `patrol_logs`라는 다른 테이블/스키마를 봄
   -> 두 스크립트가 서로 다른 세상을 보고 있었음.
2. `jetson_patrol_pipeline.py`와 `team_integration_adapter.py`가
   `DetectionEvent`와 위험도 판정 로직(`_classify_risk`)을 각자 파일에
   따로 정의 -> 나중에 임계값 하나를 고치면 한쪽만 반영되고 다른 쪽은
   구버전 로직으로 남는 "논리 drift" 위험이 있었음.
3. DB 비밀번호가 `manage_smart_factory_db.py`에 평문으로 하드코딩되어
   있었음.

이 파일이 그 세 가지 문제의 "단일 진실 공급원(single source of truth)"
역할을 합니다. dashboard/pipeline/integration/admin 네 컴포넌트 모두
이 파일의 DDL, DetectionEvent, classify_risk를 가져다 씁니다.
"""

import os
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


# ==========================================
# 1. DB 접속 설정
# ==========================================
def get_db_config() -> dict:
    """환경변수 우선, 없으면 로컬 개발 기본값.

    비밀번호를 코드에 하드코딩하지 않기 위해 환경변수(SFP_DB_PASSWORD)로
    분리했습니다. 실행 전에 다음과 같이 설정하세요.

        export SFP_DB_PASSWORD='실제비밀번호'

    Streamlit 대시보드는 st.secrets를 쓰므로 이 함수를 사용하지 않고,
    dashboard/smart_factory_dashboard_v3.py 에서 st.secrets로 별도 처리합니다.
    (pipeline, integration, admin 스크립트는 Streamlit 밖에서 돌아가므로
    이 함수를 사용합니다.)
    """
    return {
        "dbname": os.environ.get("SFP_DB_NAME", "smart_factory_db"),
        "user": os.environ.get("SFP_DB_USER", "postgres"),
        "password": os.environ.get("SFP_DB_PASSWORD", ""),
        "host": os.environ.get("SFP_DB_HOST", "localhost"),
        "port": os.environ.get("SFP_DB_PORT", "5432"),
    }


# ==========================================
# 2. 테이블 정의 (DDL) — 모든 컴포넌트가 이걸 그대로 실행
# ==========================================
DEFAULT_ZONES = ["A", "B", "C"]

ZONE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS patrol_zones (
    zone VARCHAR(10) PRIMARY KEY,
    robot_id VARCHAR(50),
    description VARCHAR(255)
);
"""

LOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS patrol_logs (
    id SERIAL PRIMARY KEY,
    zone VARCHAR(10) REFERENCES patrol_zones(zone),
    detected_object VARCHAR(50),
    distance FLOAT,
    box_position VARCHAR(50),
    risk_level VARCHAR(20),
    issue VARCHAR(255),
    log_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    map_x DOUBLE PRECISION,
    map_y DOUBLE PRECISION
);
"""

# 이미 patrol_logs가 만들어진(map_x/map_y 없이) 기존 DB를 위한 마이그레이션.
# 새로 DB를 만드는 경우엔 LOG_TABLE_DDL이 이미 map_x/map_y를 포함하므로
# 이 DDL은 아무 일도 하지 않습니다(IF NOT EXISTS라 안전하게 여러 번 실행 가능).
MIGRATE_LOG_TABLE_MAP_COLUMNS_DDL = """
ALTER TABLE patrol_logs ADD COLUMN IF NOT EXISTS map_x DOUBLE PRECISION;
ALTER TABLE patrol_logs ADD COLUMN IF NOT EXISTS map_y DOUBLE PRECISION;
"""

# detection_events 에 session_id 를 추가하는 마이그레이션.
# 기존 행에는 값이 없으므로 빈 문자열로 채웁니다(아래 유니크 인덱스가 걸리려면
# NULL 이면 안 됩니다 — Postgres 에서 NULL 끼리는 서로 다른 값 취급이라
# 중복 방지가 동작하지 않습니다).
MIGRATE_DETECTION_EVENTS_SESSION_DDL = """
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT '';
"""

# response_plans가 이미 만들어진 운영 DB에도 생성 경로를 추가합니다.
# 예전 행은 실제 경로를 복원할 수 없으므로 unknown으로 남겨 오표시를 피합니다.
MIGRATE_RESPONSE_PLAN_GENERATION_MODE_DDL = """
ALTER TABLE response_plans
    ADD COLUMN IF NOT EXISTS generation_mode VARCHAR(20) NOT NULL DEFAULT 'unknown';
"""

# 이다은님 노트북(SLAM/미니맵)에서 각도 기반 레이캐스팅으로 확정한 탐지를
# 기록하는 테이블. patrol_logs(손준영 팀장 쪽 UDP 탐지)와는 별도 흐름이라
# 테이블을 분리했습니다. event_logger.py가 이 DDL을 그대로 사용합니다.
DETECTION_EVENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS detection_events (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    tracker_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    map_x DOUBLE PRECISION NOT NULL,
    map_y DOUBLE PRECISION NOT NULL,
    hit_count INTEGER NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 정진철 팀원의 LLM 에이전트가 생성할 대응안을 저장할 테이블.
# 아직 그 팀원 코드가 없으므로 team_integration_adapter.py 에서는
# Mock으로만 채워지지만, 스키마는 미리 확정해둡니다.
RESPONSE_PLAN_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS response_plans (
    id SERIAL PRIMARY KEY,
    zone VARCHAR(10) REFERENCES patrol_zones(zone),
    detected_object VARCHAR(50),
    risk_level VARCHAR(20),
    recommended_action VARCHAR(500),
    reference_docs TEXT,
    confidence FLOAT,
    generation_mode VARCHAR(20) NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

SEED_ZONES_SQL = """
INSERT INTO patrol_zones (zone, robot_id, description)
VALUES ('A', 'ROBO-A01', 'A 구역'),
       ('B', 'ROBO-B01', 'B 구역'),
       ('C', 'ROBO-C01', 'C 구역')
ON CONFLICT (zone) DO NOTHING;
"""

ALL_TABLE_DDL = [ZONE_TABLE_DDL, LOG_TABLE_DDL, RESPONSE_PLAN_TABLE_DDL, DETECTION_EVENTS_TABLE_DDL]

# ==========================================
# 2-1. 인덱스 — 대시보드 조회 성능에 직결됩니다
# ==========================================
# 젯슨이 30fps로 추론하므로 patrol_logs 는 매우 빠르게 커집니다. 그런데 대시보드는
# 0.5초마다 "구역별 최신 로그"와 "최근 5분 위험 건수"를 조회합니다. 인덱스가 없으면
# 두 쿼리 모두 테이블 전체를 훑기 때문에, 로그가 쌓일수록 대시보드가 점점 느려지다가
# 결국 갱신 주기를 못 따라갑니다. 시연 중에 느려지는 전형적인 원인입니다.
#
#   idx_patrol_logs_zone_time  구역별 최신 1건 (DISTINCT ON) + 구역 상세 로그
#   idx_patrol_logs_risk_time  최근 N분 위험 건수 COUNT
#   idx_patrol_logs_time       전 구역 통합 조회 (대시보드 상세 조회의 '전체')
#   idx_detection_events_*     event_logger 중복 확인 / 시간순 조회
#   idx_response_plans_created 대응안 최신순 조회 (대시보드 상단 카드)
#   idx_response_plans_zone_created 구역별 대응안 이력 (대시보드 상세 조회)
#
# idx_patrol_logs_time 이 따로 필요한 이유: (zone, log_time DESC) 복합 인덱스는
# 구역을 지정했을 때만 정렬에 쓸 수 있습니다. 대시보드 상세 조회의 기본값인
# '전체 구역'은 WHERE 없이 ORDER BY log_time DESC LIMIT N 을 돌리는데, 이때는
# 저 복합 인덱스를 못 타서 patrol_logs 전체를 훑고 정렬합니다. 30fps로 쌓이는
# 테이블에서 2초마다 그러면 시연 도중 눈에 띄게 느려집니다.
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_patrol_logs_zone_time
    ON patrol_logs (zone, log_time DESC);
CREATE INDEX IF NOT EXISTS idx_patrol_logs_risk_time
    ON patrol_logs (risk_level, log_time DESC);
CREATE INDEX IF NOT EXISTS idx_patrol_logs_time
    ON patrol_logs (log_time DESC);
CREATE INDEX IF NOT EXISTS idx_detection_events_detected_at
    ON detection_events (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_response_plans_created
    ON response_plans (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_response_plans_zone_created
    ON response_plans (zone, created_at DESC);
"""

# 중복 적재 방지의 실제 근거. event_logger.py 의 메모리 캐시는 이제 왕복을
# 아끼는 최적화일 뿐이고, "이미 기록했는가"의 판단은 이 인덱스가 합니다.
#
# session_id 가 빈 문자열인 행은 제외합니다(부분 인덱스). 두 가지 이유입니다.
#   1. 이 컬럼이 생기기 전에 쌓인 기존 행은 전부 '' 이고 중복이 있을 수 있는데,
#      그걸 대상으로 유니크 인덱스를 만들면 생성 자체가 실패합니다.
#   2. 구버전 미니맵(session 을 안 보내는)과 붙었을 때도 적재가 거부되지 않고
#      예전 동작 그대로 굴러가야 합니다.
UNIQUE_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_detection_events_session_tracker
    ON detection_events (session_id, tracker_id)
    WHERE session_id <> '';
"""


def _try_create_unique_index(cursor):
    """유니크 인덱스를 만들되, 실패해도 나머지 초기화를 막지 않는다.

    init_all_tables() 는 진입점 5곳이 시작할 때마다 부르는 함수라 여기서
    예외가 나면 대시보드도 파이프라인도 뜨지 않습니다. 인덱스 하나 때문에
    전체가 멈추는 것보다는, 중복 방지가 한 단계 약해지는 편이 낫습니다.
    """
    conn = getattr(cursor, "connection", None)
    autocommit = getattr(conn, "autocommit", True)
    try:
        # autocommit 이 꺼져 있으면 실패한 문장이 트랜잭션 전체를 오염시켜서
        # 뒤따르는 SEED_ZONES_SQL 까지 거부됩니다. 세이브포인트로 격리합니다.
        if not autocommit:
            cursor.execute("SAVEPOINT sfp_uq_detection_events")
        cursor.execute(UNIQUE_INDEX_DDL)
        if not autocommit:
            cursor.execute("RELEASE SAVEPOINT sfp_uq_detection_events")
    except Exception as exc:
        if not autocommit:
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT sfp_uq_detection_events")
            except Exception:
                pass
        print(
            "[schema] detection_events 중복 방지 인덱스를 만들지 못했습니다: "
            f"{exc}\n"
            "         (session_id 가 같은 중복 행이 이미 있다는 뜻입니다. "
            "admin CLI 의 '오래된 기록만 정리' 로 줄인 뒤 다시 시작하세요.)"
        )


def init_all_tables(cursor):
    """테이블 4개 + 인덱스 + 기본 구역 시드를 생성하고, 기존 patrol_logs에는
    map_x/map_y 컬럼을 추가(없으면)합니다. psycopg2 cursor를 받아 실행만 함
    (commit은 호출자 책임).

    전부 IF NOT EXISTS 라 여러 번 실행해도 안전하고, 이미 운영 중인 DB에
    적용하면 인덱스만 새로 생깁니다.

    **데이터는 지우지 않습니다.** 이 함수는 대시보드/파이프라인/어댑터/관리
    CLI 등 5개 진입점이 각자 시작할 때 호출하고, 그중 PatrolLogWriter._connect()
    는 DB가 끊길 때마다 다시 호출됩니다. 여기서 데이터를 비우면 나중에 뜬
    프로세스가 먼저 뜬 프로세스의 기록을 지우고, 시연 도중 네트워크가 한 번만
    끊겨도 그때까지의 로그가 통째로 사라집니다.
    데이터를 비우는 건 reset_operational_data() 로 명시적으로만 하세요.
    """
    for ddl in ALL_TABLE_DDL:
        cursor.execute(ddl)
    cursor.execute(MIGRATE_LOG_TABLE_MAP_COLUMNS_DDL)
    cursor.execute(MIGRATE_DETECTION_EVENTS_SESSION_DDL)
    cursor.execute(MIGRATE_RESPONSE_PLAN_GENERATION_MODE_DDL)
    cursor.execute(INDEX_DDL)
    _try_create_unique_index(cursor)
    cursor.execute(SEED_ZONES_SQL)


# ==========================================
# 2-2. 적재 SQL — DDL 바로 옆에 둡니다
# ==========================================
# 예전에는 INSERT 문이 적재하는 쪽마다 따로 있었습니다.
#
#   patrol_logs     pipeline/jetson_patrol_pipeline.py  +  integration/team_integration_adapter.py
#   response_plans  pipeline/patrol_pipeline_rag.py     +  integration/team_integration_adapter.py
#
# 이게 실제로 데이터를 잃었습니다. patrol_logs 에 map_x/map_y 컬럼을 추가하고
# (MIGRATE_LOG_TABLE_MAP_COLUMNS_DDL), DetectionEvent 에도 필드를 넣고,
# build_map_detection_event() 가 값을 채우고, 테스트까지 그 값을 검증하는데 —
# **두 INSERT 문 어느 쪽도 그 컬럼을 쓰지 않아서 계속 NULL 이었습니다.**
# 컬럼을 늘릴 때 INSERT 두 곳을 같이 고쳐야 한다는 걸 아무도 기억하지 못한 겁니다.
#
# response_plans 쪽도 조용히 갈라져 있었습니다. 한쪽은 `plan.reference_docs or []`
# 인데 다른 쪽은 `plan.reference_docs` 라, 근거가 없는 대응안이 오면 한쪽만
# TypeError 로 죽었습니다.
#
# 그래서 SQL 과 파라미터 조립을 DDL 바로 옆으로 옮겼습니다. 컬럼을 바꾸면
# 이 블록만 고치면 되고, 적재하는 쪽은 전부 자동으로 따라옵니다.

INSERT_PATROL_LOG_SQL = """
INSERT INTO patrol_logs
    (zone, detected_object, distance, box_position, risk_level, issue, map_x, map_y)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

INSERT_RESPONSE_PLAN_SQL = """
INSERT INTO response_plans
    (zone, detected_object, risk_level, recommended_action, reference_docs,
     confidence, generation_mode)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

# ON CONFLICT DO NOTHING 이 중복 방지의 실제 근거입니다 (UNIQUE_INDEX_DDL 참고).
# event_logger.py 의 메모리 캐시는 왕복을 아끼는 최적화일 뿐이라, 캐시가
# 비어 있어도(재시작) 넘쳐서 버려져도(MAX_SEEN_IDS) 중복 행이 생기지 않습니다.
INSERT_DETECTION_EVENT_SQL = """
INSERT INTO detection_events
    (session_id, tracker_id, label, map_x, map_y, hit_count, detected_at)
VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s))
ON CONFLICT DO NOTHING
"""


def detection_event_params(session_id: str, obj: dict) -> tuple:
    """미니맵 /detections 응답의 항목 하나를 INSERT_DETECTION_EVENT_SQL 파라미터로.

    session_id 는 미니맵 프로세스가 뜰 때마다 새로 만드는 값입니다. tracker id 는
    미니맵이 재시작하면 1부터 다시 시작하므로 id 하나만으로는 예전 물체와 새
    물체를 구분할 수 없습니다. 빈 문자열이면 구버전 미니맵(session 미전송)이라는
    뜻이고, 그 경우 부분 유니크 인덱스가 적용되지 않아 예전 동작 그대로입니다.
    """
    return (
        session_id or "",
        obj["id"], obj["label"], obj["x"], obj["y"],
        obj["hit_count"], obj["last_seen"],
    )


def patrol_log_params(event) -> tuple:
    """DetectionEvent 하나를 INSERT_PATROL_LOG_SQL 의 파라미터로 바꾼다.

    map_x/map_y 는 SLAM 경로(build_map_detection_event)에서만 채워지고 NanoOWL
    UDP 경로에서는 None 입니다. 컬럼이 NULL 을 허용하므로 그대로 넘깁니다.
    """
    return (
        event.zone,
        event.detected_object,
        event.distance,
        event.box_position,
        event.risk_level,
        event.issue,
        getattr(event, "map_x", None),
        getattr(event, "map_y", None),
    )


def response_plan_params(plan) -> tuple:
    """ResponsePlan 하나를 INSERT_RESPONSE_PLAN_SQL 의 파라미터로 바꾼다.

    ResponsePlan 은 integration 패키지에 있고 그쪽이 이 모듈을 import 하므로,
    타입을 명시하면 순환 import 가 됩니다. 필요한 속성만 읽습니다.

    reference_docs 가 None 일 수 있어 `or []` 로 받습니다 — 이 가드가 한쪽에만
    있어서 근거 없는 대응안에서 TypeError 로 죽던 곳이 있었습니다.
    """
    return (
        plan.zone,
        plan.source_event.detected_object,
        plan.source_event.risk_level,
        plan.recommended_action,
        "\n".join(plan.reference_docs or []),
        plan.confidence,
        getattr(plan, "generation_mode", "unknown") or "unknown",
    )


# ==========================================
# 2-3. 운영 데이터 초기화 / 보존 정책
# ==========================================
# "프로그램 켤 때마다 DB를 비우면 되지 않나?"에 대한 답이 이 블록입니다.
# 자동으로 비우는 건 위험하지만(위 init_all_tables 주석 참고), 비울 방법 자체는
# 필요합니다. 그래서 사람이 명시적으로 부를 때만 도는 함수로 분리했습니다.
#
# 지우는 것   patrol_logs / response_plans / detection_events  (운영 중 쌓이는 기록)
# 남기는 것   patrol_zones (구역 정의)
#             safety_chunks 등 RAG 지식베이스 (setup_knowledge_base.py 산출물,
#                                              다시 만들려면 오래 걸림)
RESET_TABLES = ("patrol_logs", "response_plans", "detection_events")


def reset_operational_data(cursor) -> None:
    """순찰 기록을 전부 비웁니다. 구역 정의와 RAG 지식베이스는 남습니다.

    시연 직전에 화면을 깨끗하게 만들거나, 판정 정책을 바꾼 뒤 옛 기준으로
    쌓인 로그를 털어낼 때 씁니다. commit은 호출자 책임입니다.

    RESTART IDENTITY 로 id 시퀀스도 1부터 되돌립니다.
    """
    cursor.execute(
        "TRUNCATE {} RESTART IDENTITY;".format(", ".join(RESET_TABLES))
    )


def purge_old_logs(cursor, days: int = 7) -> dict:
    """지정한 일수보다 오래된 기록을 삭제하고 테이블별 삭제 건수를 돌려줍니다.

    젯슨이 30fps로 추론하므로 patrol_logs 는 하루에도 수백만 건까지 늘어날 수
    있습니다. 지금까지 이 테이블을 정리하는 코드가 어디에도 없었습니다.
    주기적으로 이 함수를 돌리면 초기화 없이도 크기를 일정하게 유지할 수 있습니다.
    """
    if days < 0:
        raise ValueError("days 는 0 이상이어야 합니다.")

    # 기준 시각 함수는 컬럼 타입에 맞춰야 합니다.
    # patrol_logs.log_time / response_plans.created_at 은 TIMESTAMP(시간대 없음),
    # detection_events.detected_at 은 TIMESTAMPTZ 입니다. 섞어 쓰면 세션 시간대에
    # 따라 경계가 몇 시간씩 밀립니다.
    targets = (
        ("patrol_logs", "log_time", "LOCALTIMESTAMP"),
        ("response_plans", "created_at", "LOCALTIMESTAMP"),
        ("detection_events", "detected_at", "now()"),
    )

    deleted = {}
    for table, time_column, now_expr in targets:
        cursor.execute(
            "DELETE FROM {} WHERE {} < {} - (%s * INTERVAL '1 day');".format(
                table, time_column, now_expr),
            (days,),
        )
        deleted[table] = cursor.rowcount
    return deleted


# ==========================================
# 3. 감지 이벤트 데이터 계약
# ==========================================
@dataclass
class DetectionEvent:
    """비전 모듈(NanoOWL) 탐지 결과 1건의 표준 형태.

    jetson_patrol_pipeline.py, team_integration_adapter.py 모두
    이 클래스 하나만 사용합니다 (더 이상 각자 파일에 따로 정의하지 않음).

    map_x/map_y는 선택 필드입니다. 손준영 팀장 쪽 UDP 탐지(bbox 기반)는
    이 값이 없고(None), 이다은님 쪽 SLAM 레이캐스팅 결과를 patrol_logs에
    같이 기록하고 싶을 때만 채워서 씁니다 (build_map_detection_event 참고).
    """

    zone: str
    detected_object: str
    distance: float
    box_position: str
    risk_level: str  # '정상' | '주의' | '위험'
    issue: str
    map_x: Optional[float] = None
    map_y: Optional[float] = None


# ==========================================
# 4. 위험도 판정 정책
# ==========================================
# 손준영 팀장의 ai_inference_sender.py 가 보내는 payload에는 위험도가
# 없으므로 (object, distance)만 보고 여기서 위험도를 매깁니다.
# 팀/멘토와 협의해 조정할 값은 이 블록의 상수만 바꾸면 됩니다.
# 안전모 미착용은 거리와 무관하게 규정 위반 상태이므로 항상 '위험'.
ALWAYS_DANGER_OBJECTS = {"fire", "hazardous leak", "person with no helmet"}
ALWAYS_CAUTION_OBJECTS = {"obstacle"}
# 거리에 따라 등급이 달라지는 객체. 차량은 작업자와 같은 기준을 씁니다.
#
# "person" 은 젯슨 text_prompt 의 현재 클래스이고, "human" 은 옛 이름입니다.
# 예전에는 여기에 "human" 만 있었는데 그 라벨을 만드는 코드가 어디에도 없어서,
# 아래 거리 기준이 차량에만 적용되고 "작업자 근접" 판정은 한 번도 돌지
# 않았습니다. 안전모를 쓴 작업자는 애초에 탐지 대상도 아니었습니다.
DISTANCE_BASED_OBJECTS = {"person", "human", "vehicle"}

HUMAN_DANGER_DISTANCE_M = 1.5
HUMAN_CAUTION_DISTANCE_M = 3.0

# 실내 공장 기준 거리 상한. 이 값을 넘으면 시차 계산 실패로 보고 '거리 미상'
# 처리합니다. ai_inference_sender.py 가 유효하지 않은 시차를 -1.0 으로
# 마스킹하도록 고쳐졌지만(docs/판정정책_갱신제안.md 5.4), 실측이라도 실내
# 기준을 벗어나는 이상치가 들어올 수 있어 이쪽에서도 한 번 걸러냅니다.
MAX_VALID_DISTANCE_M = 15.0

OBJECT_KO_NAME = {
    # 현재 비전 모듈(ai_inference_sender.py)의 text_prompt 클래스
    "person with no helmet": "안전모 미착용",
    "person": "작업자",
    "fire": "화재",
    "vehicle": "차량·중장비",
    # 과거 로그 조회를 위해 유지
    "hazardous leak": "유해물질 누출",
    "obstacle": "장애물",
    "human": "작업자",
}


def _coerce_distance(distance_m):
    """외부 payload의 거리값을 비교 가능한 float로 정규화합니다."""
    if distance_m is None:
        return None
    try:
        return float(distance_m)
    except (TypeError, ValueError):
        return None


def is_distance_valid(distance_m) -> bool:
    """StereoSGBM 이 유효 뎁스를 못 구한 값(-1.0)과 실내에서 성립하지 않는
    이상치를 함께 배제합니다."""
    d = _coerce_distance(distance_m)
    if d is None:
        return False
    return 0 <= d <= MAX_VALID_DISTANCE_M


def classify_risk(object_name: str, distance_m: float) -> tuple:
    """(object, distance) -> (risk_level, issue).

    비전 모듈의 탐지 클래스가 바뀌면 이 함수가 아니라 위쪽 집합 상수만
    고치면 됩니다. 예전에는 거리 기반 판정 대상이 "human" 문자열 하나로
    하드코딩돼 있어서, 클래스가 person with no helmet / vehicle 로 바뀐 뒤
    두 클래스가 통째로 '미분류 → 주의'로 빠졌습니다. 그 결과 안전모 미착용
    상황에서 대응안이 아예 생성되지 않았습니다
    (파이프라인은 '위험' 등급에서만 대응안을 만들기 때문).
    """
    ko_name = OBJECT_KO_NAME.get(object_name, object_name)

    if object_name in ALWAYS_DANGER_OBJECTS:
        return "위험", f"{ko_name} 감지"

    if object_name in ALWAYS_CAUTION_OBJECTS:
        return "주의", f"{ko_name} 감지"

    if object_name in DISTANCE_BASED_OBJECTS:
        distance = _coerce_distance(distance_m)
        if not is_distance_valid(distance):
            return "주의", f"{ko_name} 감지 (거리 미상)"
        if distance < HUMAN_DANGER_DISTANCE_M:
            return "위험", f"{ko_name} 근접 위험 ({distance}m)"
        if distance < HUMAN_CAUTION_DISTANCE_M:
            return "주의", f"{ko_name} 접근 관찰 필요 ({distance}m)"
        return "정상", ""

    # text_prompt에 새 클래스가 추가되는 등, 아직 정의되지 않은 객체
    return "주의", f"미분류 객체 감지: {object_name}"


def build_detection_event(zone: str, det: dict) -> DetectionEvent:
    """NanoOWL UDP payload의 detection 딕셔너리 1개 -> DetectionEvent 변환.

    입력 예시 (ai_inference_sender.py가 실제로 보내는 형태):
        {"object": "human", "bbox": [x1, y1, x2, y2], "distance_meter": 1.2}
    """
    object_name = det.get("object", "unknown")
    distance_m = _coerce_distance(det.get("distance_meter", -1.0))
    if distance_m is None:
        distance_m = -1.0
    risk_level, issue = classify_risk(object_name, distance_m)

    # bbox 키가 있는데 값이 None 이면 det.get(..., 기본값) 은 None 을 그대로
    # 돌려주므로, 아래 인덱싱에서 TypeError 가 납니다. 길이도 함께 확인합니다.
    bbox = det.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
        box_position = f"x:{bbox[0]},y:{bbox[1]}"
    else:
        box_position = "unknown"

    return DetectionEvent(
        zone=zone,
        detected_object=OBJECT_KO_NAME.get(object_name, object_name),
        distance=distance_m,
        box_position=box_position,
        risk_level=risk_level,
        issue=issue,
    )


def build_map_detection_event(zone: str, tracked_obj: dict) -> DetectionEvent:
    """이다은님 노트북(minimap_renderer.py)의 SLAM 기반 탐지 1건 ->
    DetectionEvent 변환 (map_x/map_y 채움).

    입력 예시 (minimap_renderer.py의 /detections 엔드포인트가 반환하는 형태):
        {"id": 1, "label": "person", "x": 1.2, "y": 0.5, "hit_count": 3, "last_seen": ...}

    NanoOWL 쪽 UDP 탐지(build_detection_event)와 달리 실제 거리(distance_meter)가
    아니라 지도 좌표(x, y)라서, classify_risk()는 "거리 미상(-1)"으로 호출합니다.
    즉 이 경로로 들어오는 이벤트는 위치 기록이 주 목적이고, 위험도 판정의
    1차 기준은 여전히 NanoOWL 쪽(build_detection_event) 흐름입니다.
    """
    label = tracked_obj.get("label", "unknown")
    risk_level, issue = classify_risk(label, -1.0)

    return DetectionEvent(
        zone=zone,
        detected_object=OBJECT_KO_NAME.get(label, label),
        distance=-1.0,
        box_position="map",
        risk_level=risk_level,
        issue=issue,
        map_x=tracked_obj.get("x"),
        map_y=tracked_obj.get("y"),
    )


# ==========================================
# 5. 대응안 중복 생성 억제 정책
# ==========================================
# 젯슨은 30fps로 추론하므로, 위험 상황 하나가 10초만 지속돼도 같은
# (구역, 객체) 조합의 '위험' 이벤트가 수백 건 발생합니다.
#
# patrol_logs는 시계열 기록이라 전부 남겨야 하지만, 대응안은 같은 조합이면
# 내용이 동일합니다. 반복 생성하면 response_plans가 같은 문장으로 가득 차서
# 관제 화면에서 정작 다른 위험을 못 보게 되고(알림 피로), 지식베이스 검색과
# LLM 호출 비용도 그만큼 낭비됩니다.
#
# docs/판정정책_갱신제안.md 6절 제안에 따라 (구역, 객체) 단위로 억제합니다.
# 억제 간격을 바꾸려면 이 상수만 고치면 됩니다.
RESPONSE_PLAN_SUPPRESS_SECONDS = 30.0

# 위험도 비교용 순서. 값이 클수록 심각합니다.
RISK_ORDER = {"정상": 0, "주의": 1, "위험": 2}


def risk_rank(risk_level: str) -> int:
    """위험도 문자열 -> 심각도 순위. 알 수 없는 등급은 가장 낮게 봅니다.

    억제 여부를 판단할 때만 쓰는 보조 함수입니다. 모르는 등급을 낮게 잡아야
    "상향 시 예외" 조건이 과도하게 열리지 않습니다.
    """
    return RISK_ORDER.get(risk_level, 0)


class ResponsePlanThrottle:
    """(구역, 객체) 조합별 대응안 생성 빈도 제한기.

    억제 간격 안에 들어온 같은 조합은 대응안을 새로 만들지 않습니다.
    단 **위험도가 직전보다 올라간 경우는 간격과 무관하게 통과**시킵니다.
    상황이 악화되는 순간을 놓치면 억제 자체가 사고 원인이 되기 때문입니다.
    (예: 차량이 주의 -> 위험으로 근접한 순간)

    시각은 time.monotonic()을 씁니다. 시스템 시계가 NTP 등으로 뒤로 점프해도
    억제가 30초보다 길게 걸리는 일이 없도록 하기 위함입니다.

    상태는 프로세스 메모리에만 있습니다. 파이프라인을 재시작하면 초기화되어
    첫 이벤트가 곧바로 통과하는데, 재시작 직후엔 오히려 현재 상태를 한 번
    기록하는 편이 안전하므로 의도된 동작입니다.

    사용 예:
        throttle = ResponsePlanThrottle()
        if throttle.should_generate(event):
            plan = agent.generate_response(event)
    """

    def __init__(self,
                 suppress_seconds: float = RESPONSE_PLAN_SUPPRESS_SECONDS,
                 time_func=time.monotonic):
        # 0 이하를 주면 억제가 완전히 꺼집니다 (모든 이벤트가 통과).
        self.suppress_seconds = suppress_seconds
        self._time = time_func
        # key: (zone, detected_object) -> (마지막 생성 시각, 그때의 위험도 순위)
        self._last: Dict[Tuple[str, str], Tuple[float, int]] = {}

    def should_generate(self, event: DetectionEvent) -> bool:
        """이 이벤트로 대응안을 생성해야 하면 True.

        True를 반환할 때만 내부 기준 시각을 갱신합니다. 억제된 이벤트마다
        시각을 밀어버리면, 계속 감지되는 물체는 영영 다음 대응안이 나오지
        않게 됩니다("생성 후 30초"가 아니라 "감지가 끊긴 후 30초"가 되어버림).
        """
        key = (event.zone, event.detected_object)
        now = self._time()
        rank = risk_rank(event.risk_level)

        previous = self._last.get(key)
        if previous is not None:
            last_time, last_rank = previous
            within_window = (now - last_time) < self.suppress_seconds
            if within_window and rank <= last_rank:
                return False

        self._last[key] = (now, rank)
        return True

    def reset(self) -> None:
        """추적 상태를 모두 비웁니다 (테스트용)."""
        self._last.clear()
