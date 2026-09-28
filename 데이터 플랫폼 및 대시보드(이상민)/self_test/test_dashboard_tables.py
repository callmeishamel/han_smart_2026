r"""
self_test/test_dashboard_tables.py

상세 조회 표 3종(감지 요약 / 원본 로그 / AI 대응안 이력)을 **진짜 pandas 로**
끝까지 그려보는 자체 테스트.

왜 따로 있나
------------
test_dashboard_layout.py 는 pandas 를 빈 모듈로 스텁하고 로그 0건 경로만 탑니다
(그래서 Streamlit/pandas 없이 돌아갑니다). 덕분에 배치·이스케이프는 검증되지만
표를 만드는 코드는 **한 줄도 실행되지 않습니다**. 실제로 이 파일을 만들면서
v8 개발 중에 그 사각지대에서 버그 두 개가 나왔습니다.

  1. age_seconds 가 NULL 인 대응안이 하나라도 섞이면 pandas 가 그 컬럼을
     float 으로 만들면서 None 을 NaN 으로 바꾸고, _ago() 의 int(float(nan)) 이
     ValueError 를 던져 **표 전체가 사라졌습니다**.
  2. confidence 가 NULL 이면 f"{nan:.0%}" 가 예외 없이 "nan%" 를 찍었습니다
     (float(nan) 은 예외를 안 냅니다 — try/except 로는 안 막힙니다).

둘 다 "NULL 이 섞인 실제 데이터"에서만 나오는 종류라, 행 0건 테스트로는 영원히
안 잡힙니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_dashboard_tables.py

pandas 가 없으면 실패로 종료합니다. 핵심 표 로직을 하나도 실행하지
않고 전체 테스트가 통과한 것처럼 보이는 거짓 통과를 막기 위해서입니다.
"""

import datetime as _dt
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_SELF_TEST = os.path.dirname(os.path.abspath(__file__))
if _SELF_TEST not in sys.path:
    sys.path.insert(0, _SELF_TEST)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

try:
    import pandas
except ImportError:
    print("[FAIL] pandas 가 필요합니다: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(1)

# 가짜 streamlit/psycopg2 는 layout 테스트 것을 그대로 씁니다.
import test_dashboard_layout as layout  # noqa: E402

NOW = layout.NOW
check = layout.check


# ==========================================
# DB 가 돌려줄 법한 행. NULL 을 일부러 섞습니다.
# ==========================================
SUMMARY_ROWS = [
    {"first_time": NOW - _dt.timedelta(seconds=8), "last_time": NOW,
     "hits": 241, "zone": "B", "detected_object": "지게차", "risk_level": "위험",
     "min_distance": 1.24, "issue": "지게차 근접 위험 (1.24m)"},
    # 거리 미상 구간 (distance 가 전부 -1 이라 FILTER 후 NULL)
    {"first_time": NOW - _dt.timedelta(seconds=30), "last_time": NOW - _dt.timedelta(seconds=9),
     "hits": 630, "zone": "A", "detected_object": "작업자", "risk_level": "정상",
     "min_distance": None, "issue": None},
    {"first_time": NOW - _dt.timedelta(seconds=44), "last_time": NOW - _dt.timedelta(seconds=31),
     "hits": 12, "zone": "C", "detected_object": "안전모 미착용", "risk_level": "주의",
     "min_distance": 3.0, "issue": "안전모 미착용 감지"},
]

RAW_ROWS = [
    {"log_time": NOW, "zone": "B", "detected_object": "지게차", "distance": 1.24,
     "box_position": "center", "risk_level": "위험", "issue": "지게차 근접 위험 (1.24m)"},
    # 전 컬럼 NULL + 거리 -1(시차 계산 실패)
    {"log_time": None, "zone": "A", "detected_object": None, "distance": -1.0,
     "box_position": None, "risk_level": None, "issue": None},
]

PLAN_ROWS = [
    {"created_at": NOW, "zone": "B", "detected_object": "화재", "risk_level": "위험",
     "recommended_action": "즉시 대피", "reference_docs": "지침 3.2", "confidence": 0.85,
     "generation_mode": "keyword+llm",
     "age_seconds": 12.0},
    {"created_at": None, "zone": "A", "detected_object": None, "risk_level": "주의",
     "recommended_action": None, "reference_docs": None, "confidence": None,
     "generation_mode": "unknown",
     "age_seconds": None},
]


def load():
    """대시보드를 import 하고, pandas 를 진짜 것으로 바꿔 끼웁니다."""
    layout.SCENARIO["danger"] = True
    layout.SCENARIO["plans"] = True
    layout.SCENARIO["db_fail"] = False
    layout.load_dashboard()
    mod = sys.modules["dashboard.smart_factory_dashboard_v3"]
    mod.pd = pandas
    return mod


# st.download_button 이 어떤 (파일명, 바이트) 로 불렸는지 적어두는 기록장입니다.
# 파일을 만들지도, 네트워크를 타지도 않습니다 — 가짜 위젯이 인자를 여기에 담아둘
# 뿐이고 테스트가 끝나면 사라집니다. CSV 인코딩(utf-8-sig)은 틀려도 예외가 나지
# 않고 조용히 넘어가서, 나중에 Excel 로 열었을 때야 한글이 깨진 걸 발견하게
# 됩니다. 그래서 바이트를 직접 들여다봐야 합니다.
CSV_CALLS = []


def draw(mod, render, patch_name, rows, zone, zone_key="selected_zone"):
    """표 하나를 그리고 (프레임, 경고문구) 를 돌려줍니다.

    zone_key 가 갈리는 이유: 상세 조회(patrol_logs)는 selected_zone 을 보고,
    대응안 기록 화면은 자기만의 plan_zone 을 봅니다. 여기서 키를 안 맞추면
    구역 필터가 망가져 있어도 테스트는 통과합니다(기본값이 '전체'라 어차피
    구역 컬럼이 붙기 때문).
    """
    drawn = []
    CSV_CALLS.clear()
    mod.st.dataframe = lambda df, **kw: drawn.append(df)
    mod.st.download_button = (
        lambda label, data=None, file_name="", **kw: CSV_CALLS.append((file_name, data))
    )
    setattr(mod, patch_name, lambda *a, **k: rows)
    mod.st.session_state[zone_key] = zone

    before = len([1 for k, _, _ in layout.Rec.events if k == "warning"])
    render()
    warnings = [p for k, p, _ in layout.Rec.events if k == "warning"][before:]
    if warnings or not drawn:
        return None, warnings

    obj = drawn[-1]
    # Styler 로 감싸져 나오는 것이 정상 경로입니다. .data 가 원본 프레임.
    frame = obj.data if hasattr(obj, "data") else obj
    if hasattr(obj, "to_html"):
        obj.to_html()          # CSS 생성까지 실제로 돌려봅니다
    return frame, []


def cells(frame):
    return [str(v) for row in frame.astype(str).values.tolist() for v in row]


def main():
    mod = load()

    print("=== 감지 요약 ===")
    df, warn = draw(mod, mod._render_log_summary, "fetch_log_summary",
                    SUMMARY_ROWS, mod.ZONE_ALL)
    check("표가 그려짐 (경고 없이)", df is not None and not warn)
    cols = list(df.columns)
    check("전체 구역이면 '구역' 컬럼이 있음", "구역" in cols)
    check("건수/기간/지속이 있음", all(c in cols for c in ("건수", "기간", "지속")))
    check("box_position 은 요약에 없음 (구간으로 접히면 의미가 없음)",
          "카메라 내 위치" not in cols)
    body = cells(df)
    check("241건이 한 줄로 접힘", "241" in body)
    check("위험도에 색 표식", "🔴 위험" in body and "🟡 주의" in body and "🟢 정상" in body)
    check("거리 미상 구간은 '측정불가'", "측정불가" in body)
    check("NULL 이 NaN 으로 새어나오지 않음", "NaN" not in body and "nan" not in body)

    df1, _ = draw(mod, mod._render_log_summary, "fetch_log_summary", SUMMARY_ROWS, "B")
    check("한 구역만 볼 때는 '구역' 컬럼을 뺌", "구역" not in list(df1.columns))

    print("\n=== 원본 로그 ===")
    df, warn = draw(mod, mod._render_raw_logs, "fetch_raw_logs", RAW_ROWS, mod.ZONE_ALL)
    check("표가 그려짐 (경고 없이)", df is not None and not warn)
    body = cells(df)
    check("거리 -1 은 '측정불가'로 바뀜", "측정불가" in body)
    check("전 컬럼 NULL 인 행에서도 안 죽음", len(df) == 2)
    check("NULL 이 NaN 으로 새어나오지 않음", "NaN" not in body and "nan" not in body)

    print("\n=== AI 대응안 기록 화면 ===")
    df, warn = draw(mod, mod._render_plan_history, "fetch_plans", PLAN_ROWS,
                    mod.ZONE_ALL, zone_key="plan_zone")
    check("표가 그려짐 (경고 없이)", df is not None and not warn)
    check("전체 구역이면 '구역' 컬럼이 있음", "구역" in list(df.columns))
    body = cells(df)
    check("경과 컬럼이 상대 시각", "12초 전" in body)
    check("age_seconds 가 NULL 이어도 표가 살아 있음", "시각 불명" in body)
    check("확신도가 백분율", "85%" in body)
    check("LLM 생성 경로가 표에 표시됨",
          any("LLM 생성 · 키워드 RAG" in cell for cell in body))
    check("confidence 가 NULL 이면 'nan%' 가 아니라 '-'",
          "nan%" not in body and "-" in body)

    df_b, _ = draw(mod, mod._render_plan_history, "fetch_plans", PLAN_ROWS,
                   "B", zone_key="plan_zone")
    check("기록 화면은 자기 필터(plan_zone)를 봄 — 상세 조회 필터가 아님",
          "구역" not in list(df_b.columns))

    print("\n=== CSV 내려받기 ===")
    # 전체 구역으로 한 번 더 그려서 그 기록을 씁니다.
    draw(mod, mod._render_plan_history, "fetch_plans", PLAN_ROWS,
         mod.ZONE_ALL, zone_key="plan_zone")
    check("표를 그리면 CSV 버튼도 함께 붙음", len(CSV_CALLS) == 1)
    name, payload = CSV_CALLS[0]
    check("파일명에 무슨 표인지가 드러남", name.startswith("대응안_all_"))
    check("파일명에 시각이 붙음 (여러 번 받아도 안 덮어씀)", name.endswith(".csv")
          and len(name.split("_")[-1]) == len("143207.csv"))
    check("bytes 로 넘김", isinstance(payload, bytes))
    # 여기가 핵심입니다. BOM 이 없으면 한국어 Windows 의 Excel 이 cp949 로 읽어서
    # 한글이 전부 깨집니다. 예외가 안 나므로 눈으로 열어보기 전엔 모릅니다.
    check("utf-8-sig (BOM) 로 인코딩 — Excel 한글 깨짐 방지",
          payload.startswith(b"\xef\xbb\xbf"))
    text = payload.decode("utf-8-sig")
    header = text.splitlines()[0]
    check("헤더가 화면과 같은 한글 컬럼명 (원본 컬럼명이 아님)",
          "생성 시각" in header and "권장 조치" in header
          and "created_at" not in header)
    check("화면에서 가공한 값이 그대로 들어감 (원시 float 이 아님)",
          "85%" in text and "🔴 위험" in text)
    check("NULL 이 'nan' 이 아니라 '-'", "nan" not in text)

    df, _ = draw(mod, mod._render_log_summary, "fetch_log_summary",
                 SUMMARY_ROWS, "B")
    name2, payload2 = CSV_CALLS[0]
    check("구역을 고르면 파일명에 반영됨", name2.startswith("감지요약_B_"))
    check("  내용도 그 구역 표 그대로",
          "구역" not in payload2.decode("utf-8-sig").splitlines()[0])

    print("\n=== 보조 함수 ===")
    check("_ago(None)", mod._ago(None) == "시각 불명")
    check("_ago(NaN)", mod._ago(float("nan")) == "시각 불명")
    check("_ago(음수) — DB 시계가 앞설 때", mod._ago(-3) == "방금")
    check("_ago(42)", mod._ago(42) == "42초 전")
    check("_ago(300)", mod._ago(300) == "5분 전")
    check("_ago(7200)", mod._ago(7200) == "2시간 전")
    check("_ago(200000)", mod._ago(200000) == "2일 전")
    check("_percent(0.85)", mod._percent(0.85) == "85%")
    check("_percent(None)", mod._percent(None) == "-")
    check("_percent(NaN)", mod._percent(float("nan")) == "-")

    print("\n=== Streamlit 버전별 st.dataframe 인자 ===")
    # full_width_kwargs() 는 미니맵 섹션 모듈(edge_video/...)로 옮겨졌고, 함수
    # 안에서 `import streamlit as st` 로 가져옵니다. 대시보드 모듈에는 그 이름만
    # import 돼 있으므로, 버전은 모듈 전역이 아니라 sys.modules 의 가짜
    # streamlit 에 심어야 합니다.
    #
    # (옮긴 이유: 섹션 모듈의 순찰 제어 버튼도 같은 판정이 필요한데, 대시보드가
    #  섹션 모듈을 import 하므로 반대 방향으로는 가져올 수 없습니다. 복사해 두면
    #  Streamlit 버전 판정이 두 벌이 되어 한쪽만 고치는 사고가 납니다.)
    import sys as _sys
    fake_st = _sys.modules["streamlit"]
    had_version = hasattr(fake_st, "__version__")
    real_version = getattr(fake_st, "__version__", None)
    try:
        for version, expected in [("1.33.0", "use_container_width"),
                                  ("1.48.1", "use_container_width"),
                                  ("1.49.0", "width"),
                                  ("1.52.2", "width")]:
            fake_st.__version__ = version
            check(f"{version} -> {expected}", expected in mod._full_width_kwargs())
    finally:
        if had_version:
            fake_st.__version__ = real_version
        else:
            del fake_st.__version__
    check("__version__ 을 못 읽으면 옛 인자로 (self_test 환경)",
          "use_container_width" in mod._full_width_kwargs())

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
