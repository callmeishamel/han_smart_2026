r"""
self_test/test_schema_logic.py

common/schema.py의 핵심 판단 로직(classify_risk, build_detection_event,
build_map_detection_event)이 의도한 대로 동작하는지 확인하는 자체 테스트.

- ROS2, Jetson, PostgreSQL, 인터넷 연결 전부 필요 없습니다.
- 그냥 파이썬만 있으면 실행됩니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_schema_logic.py

모든 줄에 "OK"가 뜨면 통과입니다. AssertionError가 나면 그 줄이 실패한 겁니다.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (
    ALWAYS_CAUTION_OBJECTS,
    ALWAYS_DANGER_OBJECTS,
    DISTANCE_BASED_OBJECTS,
    INSERT_DETECTION_EVENT_SQL,
    INSERT_PATROL_LOG_SQL,
    INSERT_RESPONSE_PLAN_SQL,
    MAX_VALID_DISTANCE_M,
    RESET_TABLES,
    UNIQUE_INDEX_DDL,
    _try_create_unique_index,
    detection_event_params,
    build_detection_event,
    build_map_detection_event,
    classify_risk,
    is_distance_valid,
    patrol_log_params,
    purge_old_logs,
    reset_operational_data,
    response_plan_params,
)


class FakeCursor:
    """DB 없이 어떤 SQL이 나가는지만 확인하기 위한 최소 스텁."""

    def __init__(self, rowcount=0):
        self.statements = []
        self.rowcount = rowcount

    def execute(self, query, params=None):
        self.statements.append((" ".join(query.split()), params))


def check(desc, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}")
    if not condition:
        raise AssertionError(desc)


def main():
    print("=== classify_risk() 테스트 ===")
    risk, issue = classify_risk("fire", 10.0)
    check("fire는 거리 무관 항상 위험", risk == "위험")

    risk, issue = classify_risk("hazardous leak", 0.1)
    check("유해물질 누출도 항상 위험", risk == "위험")

    risk, issue = classify_risk("obstacle", 5.0)
    check("장애물은 항상 주의", risk == "주의")

    risk, issue = classify_risk("human", 1.0)
    check("사람 1.0m는 위험 (임계값 1.5m 미만)", risk == "위험")

    risk, issue = classify_risk("human", 2.0)
    check("사람 2.0m는 주의 (1.5~3.0m 사이)", risk == "주의")

    risk, issue = classify_risk("human", 5.0)
    check("사람 5.0m는 정상 (3.0m 이상)", risk == "정상")

    risk, issue = classify_risk("human", -1.0)
    check("거리 미상(-1)인 사람은 보수적으로 주의 처리", risk == "주의")

    risk, issue = classify_risk("forklift", 1.0)
    check("정의 안 된 객체는 '주의'로 떨어지되 issue에 남음", risk == "주의" and "forklift" in issue)

    print("\n=== 현재 비전 모듈의 탐지 클래스 3종 ===")
    # ai_inference_sender.py 의 text_prompt = ["person with no helmet", "fire", "vehicle"]
    # 예전 정책은 거리 기반 판정 대상이 "human" 하나로 하드코딩돼 있어서,
    # 아래 두 클래스가 통째로 '미분류 -> 주의'로 빠졌고 대응안이 생성되지 않았습니다.
    risk, issue = classify_risk("person with no helmet", 1.1)
    check("안전모 미착용은 거리 무관 항상 위험", risk == "위험")
    check("  한글 사유로 표기됨", "안전모 미착용" in issue and "미분류" not in issue)

    risk, issue = classify_risk("person with no helmet", 9.0)
    check("안전모 미착용은 멀어도 위험 (규정 위반 상태)", risk == "위험")

    risk, issue = classify_risk("vehicle", 1.3)
    check("차량 1.3m는 위험", risk == "위험" and "차량" in issue)

    risk, issue = classify_risk("vehicle", 2.5)
    check("차량 2.5m는 주의", risk == "주의")

    risk, issue = classify_risk("vehicle", 8.0)
    check("차량 8.0m는 정상", risk == "정상")

    print("\n=== 거리값 유효성 (is_distance_valid) ===")
    check("-1.0 은 무효 (시차 계산 실패)", not is_distance_valid(-1.0))
    check("None 은 무효", not is_distance_valid(None))
    check("문자열 등 이상한 값도 무효", not is_distance_valid("abc"))
    check("숫자 문자열은 유효한 거리로 처리", is_distance_valid("1.2"))
    check("0.0 은 유효", is_distance_valid(0.0))
    check(f"{MAX_VALID_DISTANCE_M}m 는 유효 (경계값 포함)", is_distance_valid(MAX_VALID_DISTANCE_M))
    check("300m 는 무효 (실내 공장에서 성립 불가)", not is_distance_valid(300.0))

    risk, issue = classify_risk("vehicle", 300.0)
    check("300m 차량은 '정상'이 아니라 '거리 미상 주의'로 처리",
          risk == "주의" and "거리 미상" in issue)

    risk, issue = classify_risk("vehicle", "1.2")
    check("숫자 문자열 거리도 예외 없이 위험 판정", risk == "위험")

    print("\n=== build_detection_event() 테스트 (NanoOWL UDP 경로) ===")
    event = build_detection_event("A", {"object": "human", "bbox": [10, 20, 30, 40], "distance_meter": 0.5})
    check("zone이 그대로 반영됨", event.zone == "A")
    check("한글 객체명으로 변환됨", event.detected_object == "작업자")
    check("근접 위험 판정", event.risk_level == "위험")
    check("map_x/map_y는 이 경로에서 기본 None", event.map_x is None and event.map_y is None)

    helmet = build_detection_event(
        "A", {"object": "person with no helmet", "bbox": [10, 20, 30, 40], "distance_meter": 1.1})
    check("안전모 미착용이 한글로 변환됨", helmet.detected_object == "안전모 미착용")
    check("안전모 미착용이 '위험'으로 적재됨 (대응안 생성 대상)", helmet.risk_level == "위험")

    event2 = build_detection_event("B", {"object": "unknown_thing"})  # distance_meter, bbox 생략
    check(".get() 덕분에 필드 누락돼도 에러 없이 처리됨", event2.distance == -1.0)

    string_distance = build_detection_event(
        "B", {"object": "vehicle", "distance_meter": "1.2"})
    check("이벤트의 숫자 문자열 거리를 float로 정규화", string_distance.distance == 1.2)

    invalid_distance = build_detection_event(
        "B", {"object": "vehicle", "distance_meter": "abc"})
    check("잘못된 거리 문자열은 -1.0으로 정규화", invalid_distance.distance == -1.0)

    # bbox 키가 있는데 값이 None 이면 det.get("bbox", 기본값)은 None을 그대로
    # 돌려주므로, 예전 코드는 bbox[0] 에서 TypeError로 죽었습니다.
    event3 = build_detection_event("B", {"object": "fire", "bbox": None, "distance_meter": 2.0})
    check("bbox가 None이어도 죽지 않음", event3.box_position == "unknown")
    event4 = build_detection_event("B", {"object": "fire", "bbox": [1], "distance_meter": 2.0})
    check("bbox 길이가 모자라도 죽지 않음", event4.box_position == "unknown")

    print("\n=== build_map_detection_event() 테스트 (SLAM 경로) ===")
    tracked = {"id": 7, "label": "human", "x": 1.23, "y": 4.56, "hit_count": 5, "last_seen": 111.0}
    map_event = build_map_detection_event("C", tracked)
    check("map_x/map_y가 채워짐", map_event.map_x == 1.23 and map_event.map_y == 4.56)
    check("zone 반영됨", map_event.zone == "C")
    check("box_position은 'map'으로 표시됨 (bbox 좌표가 아니므로)", map_event.box_position == "map")

    print("\n=== 운영 데이터 초기화 / 보존 정책 ===")
    cur = FakeCursor()
    reset_operational_data(cur)
    sql = cur.statements[0][0]
    check("초기화는 TRUNCATE 한 문장", len(cur.statements) == 1 and sql.startswith("TRUNCATE"))
    check("순찰 기록 3개 테이블이 대상", all(t in sql for t in RESET_TABLES))
    check("구역 정의(patrol_zones)는 건드리지 않음", "patrol_zones" not in sql)
    check("RAG 지식베이스(safety_chunks)도 건드리지 않음", "safety_chunks" not in sql)
    check("id 시퀀스도 되돌림", "RESTART IDENTITY" in sql)

    cur = FakeCursor(rowcount=5)
    deleted = purge_old_logs(cur, 7)
    check("보존 정리는 테이블마다 DELETE 한 번씩", len(cur.statements) == len(RESET_TABLES))
    check("일수가 파라미터로 바인딩됨 (문자열 결합 아님)",
          all(p == (7,) for _, p in cur.statements))
    check("삭제 건수를 테이블별로 돌려줌", deleted == {t: 5 for t in RESET_TABLES})

    # TIMESTAMP 컬럼과 TIMESTAMPTZ 컬럼은 기준 시각 함수가 달라야 합니다.
    # 섞어 쓰면 세션 시간대에 따라 경계가 몇 시간씩 밀립니다.
    by_table = {s.split()[2]: s for s, _ in cur.statements}
    check("patrol_logs(TIMESTAMP)는 LOCALTIMESTAMP 기준",
          "LOCALTIMESTAMP" in by_table["patrol_logs"])
    check("detection_events(TIMESTAMPTZ)는 now() 기준",
          "now()" in by_table["detection_events"]
          and "LOCALTIMESTAMP" not in by_table["detection_events"])

    try:
        purge_old_logs(FakeCursor(), -1)
        check("음수 일수는 거부해야 함", False)
    except ValueError:
        check("음수 일수는 ValueError", True)

    print("\n=== 적재 SQL (INSERT 중복 구현 제거) ===")
    # 예전에는 INSERT 문이 적재하는 쪽마다 따로 있었고, patrol_logs 에 map_x/map_y
    # 컬럼을 추가했을 때 두 곳 모두 그 컬럼을 빠뜨려서 계속 NULL 이었습니다.
    cols = INSERT_PATROL_LOG_SQL.split("(", 1)[1].split(")", 1)[0]
    log_cols = [c.strip() for c in cols.split(",")]
    params = patrol_log_params(map_event)
    check("patrol_logs INSERT 에 map_x/map_y 가 있음",
          "map_x" in log_cols and "map_y" in log_cols)
    check("  자리표시자 수와 파라미터 수가 일치",
          INSERT_PATROL_LOG_SQL.count("%s") == len(params) == len(log_cols))
    check("  SLAM 경로의 좌표가 실제로 파라미터에 실림",
          params[log_cols.index("map_x")] == 1.23
          and params[log_cols.index("map_y")] == 4.56)

    udp_params = patrol_log_params(event)   # NanoOWL 경로 (map_x/map_y 없음)
    check("UDP 경로는 map_x/map_y 가 None 으로 들어감",
          udp_params[log_cols.index("map_x")] is None)

    # ResponsePlan 은 integration 쪽 dataclass 라 여기서는 최소 스텁으로 흉내냅니다
    # (common.schema 가 integration 을 import 하면 순환 import 가 됩니다).
    class FakePlan:
        zone = "A"
        recommended_action = "즉시 대피"
        confidence = 0.8
        source_event = event
        reference_docs = None            # 근거를 못 찾은 경우
        generation_mode = "keyword+llm"

    plan_params = response_plan_params(FakePlan())
    check("response_plans 자리표시자 수와 파라미터 수가 일치",
          INSERT_RESPONSE_PLAN_SQL.count("%s") == len(plan_params))
    check("reference_docs 가 None 이어도 죽지 않고 빈 문자열",
          plan_params[4] == "")
    check("LLM 실제 생성 경로가 DB 파라미터에 실림",
          plan_params[-1] == "keyword+llm")

    FakePlan.reference_docs = ["지침 3.2", "대피 절차 A"]
    check("근거가 여러 건이면 줄바꿈으로 합쳐짐",
          response_plan_params(FakePlan())[4] == "지침 3.2\n대피 절차 A")

    print("\n=== detection_events 중복 방지 ===")
    # tracker id 는 미니맵이 재시작하면 1부터 다시 시작합니다. 예전에는 id 만으로
    # 중복을 판단해서, 재시작 뒤 새로 잡힌 물체를 "이미 본 1번"으로 오인해 통째로
    # 버렸습니다 (중복 행보다 위험한 기록 누락).
    check("중복이면 조용히 무시 (ON CONFLICT DO NOTHING)",
          "ON CONFLICT DO NOTHING" in INSERT_DETECTION_EVENT_SQL)
    check("session_id 가 INSERT 대상에 포함됨",
          "session_id" in INSERT_DETECTION_EVENT_SQL)
    check("유니크 인덱스가 (session_id, tracker_id) 짝",
          "(session_id, tracker_id)" in UNIQUE_INDEX_DDL)
    check("  빈 session 은 인덱스에서 제외 (기존 행 때문에 생성이 실패하지 않도록)",
          "WHERE session_id <> ''" in UNIQUE_INDEX_DDL)

    tracked_obj = {"id": 7, "label": "human", "x": 1.23, "y": 4.56,
                   "hit_count": 5, "last_seen": 111.0}
    p = detection_event_params("abc123", tracked_obj)
    check("자리표시자 수와 파라미터 수가 일치",
          INSERT_DETECTION_EVENT_SQL.count("%s") == len(p))
    check("session 이 첫 파라미터", p[0] == "abc123")
    check("session 이 None 이어도 빈 문자열로 정규화",
          detection_event_params(None, tracked_obj)[0] == "")
    check("last_seen 은 epoch 그대로 넘기고 SQL 이 to_timestamp() 로 변환",
          p[-1] == 111.0 and "to_timestamp(%s)" in INSERT_DETECTION_EVENT_SQL)

    # 유니크 인덱스 생성이 실패해도 나머지 초기화가 멈추면 안 됩니다
    # (init_all_tables 는 진입점 5곳이 시작할 때마다 호출합니다).
    class ExplodingCursor(FakeCursor):
        def execute(self, query, params=None):
            if "CREATE UNIQUE INDEX" in query:
                raise RuntimeError("중복 행이 이미 있음")
            super().execute(query, params)

    cur = ExplodingCursor()
    _try_create_unique_index(cur)
    check("인덱스 생성 실패가 예외로 번지지 않음", True)

    print("\n=== fake_jetson_sender 의 '기대 위험도' 표가 실제 판정과 맞는지 ===")
    # 이 표는 수동 통합 테스트에서 눈으로 대조하는 기준입니다. 판정 정책을
    # 바꿨을 때 표만 옛날 값으로 남으면, 맞게 동작하는데도 "틀렸다"고 읽거나
    # 그 반대가 됩니다. 그래서 표 자체를 여기서 검증합니다.
    from self_test.fake_jetson_sender import FAKE_PACKETS

    labels = set()
    for note, expect, packet in FAKE_PACKETS:
        got = ", ".join(build_detection_event("A", d).risk_level
                        for d in packet["detections"])
        check(f"{note}", got == expect)
        labels.update(d["object"] for d in packet["detections"])

    # 젯슨의 text_prompt 3종이 전부 들어 있어야 합니다. 예전에는 이 파일이
    # 구버전 라벨만 보내서, 통과해도 실제 통합 경로를 검증하지 못했습니다.
    for real in ("person with no helmet", "fire", "vehicle"):
        check(f"현재 비전 라벨 '{real}' 을 실제로 보냄", real in labels)
    check("미분류 폴백 경로도 함께 확인함",
          any(lb not in ALWAYS_DANGER_OBJECTS
              and lb not in ALWAYS_CAUTION_OBJECTS
              and lb not in DISTANCE_BASED_OBJECTS for lb in labels))

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
