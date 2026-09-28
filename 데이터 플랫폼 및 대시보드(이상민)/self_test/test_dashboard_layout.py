r"""
self_test/test_dashboard_layout.py

대시보드 화면 구조(v7)가 의도대로 조립되는지 확인하는 자체 테스트.

Streamlit / pandas / psycopg2 / PostgreSQL 없이 돕니다. 가짜 streamlit 모듈을
sys.modules 에 꽂아 넣고 smart_factory_dashboard_v3.py 를 그냥 import 하면,
모듈 최상위 코드가 위에서 아래로 실행되면서 화면을 그립니다. 그 호출 순서와
인자를 기록해서 검사합니다.

검사하는 것 (v7에서 고친 항목 + v8에서 고친 항목)
--------------------------------------------------
v7 (배치)
1. 경보/구역 상태가 영상보다 **위에** 그려지는가        (스크롤 없이 보이는가)
2. 미니맵이 st.columns 밖에서 그려지는가                (900px 폭 확보)
3. 영상은 반대로 columns 안에 있는가                    (자리 절약)
4. DB에서 온 문자열이 HTML로 나갈 때 이스케이프되는가   (XSS)
5. 위험이 없으면 경보 배너가 안 뜨는가                  (상시 경고 = 무시되는 경고)
6. 구역 상태 판정(_zone_style)이 5가지 경우 모두 맞는가

v8 (데이터가 화면에 도달하는 방식)
7. 미연결 구역이 있으면 별도 배너가 뜨는가              (로봇 사망이 조용하지 않게)
8. 상태 바에 '주의' 칸이 있는가                         (숫자 합이 구역 수와 맞게)
9. DB 조회가 실패해도 트레이스백 대신 '상태 불명'인가   (정보 없음 != 이상 없음)
10. 감지 요약이 연속 구간을 접는 쿼리를 쓰는가          (LIMIT 30 = 최근 1초 문제)
11. 상세 조회가 구역 선택을 각 보기에 넘기는가         (대응안 이력의 죽은 라디오)
12. 대응안 카드에 상대 시각이 붙고, 오래되면 색이 빠지는가

v9~v11 (사용성)
13. 경보 확인/소리/화면 구성/조회 범위/CSV
14. 영상 클릭 확대, 지도 버튼 확대 — 확대 중에도 경보는 안 가려지는가
15. 대응안 패널이 중요한 몇 건을 고르는가, 이전 기록은 따로 들어가는가

실행 방법 (프로젝트 루트에서):
    python self_test\test_dashboard_layout.py
"""

import datetime as _dt
import os
import sys
import types

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()


# ==========================================
# 기록장 — 무엇이 어떤 중첩 깊이에서 그려졌는지
# ==========================================
class Rec:
    events = []          # (종류, 내용) 순서대로
    depth = 0            # st.columns 컨텍스트 안이면 1 이상

    @classmethod
    def add(cls, kind, payload=""):
        cls.events.append((kind, str(payload), cls.depth))

    @classmethod
    def kinds(cls):
        return [k for k, _, _ in cls.events]

    @classmethod
    def index_of(cls, kind, needle):
        for i, (k, p, _) in enumerate(cls.events):
            if k == kind and needle in p:
                return i
        return -1

    @classmethod
    def html(cls):
        return "\n".join(p for k, p, _ in cls.events if k == "markdown")


# ==========================================
# 가짜 streamlit
# ==========================================
class _SessionState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


class _Container:
    """st.columns() 가 돌려주는 칸. with 로 쓰면 깊이가 1 늘어납니다."""

    def __enter__(self):
        Rec.depth += 1
        return self

    def __exit__(self, *exc):
        Rec.depth -= 1
        return False

    # cols[i].markdown(...) 처럼 with 없이 바로 쓰는 경로
    def markdown(self, body, **kw):
        Rec.add("markdown", body)

    def caption(self, body, **kw):
        Rec.add("caption", body)


def _make_streamlit():
    st = types.ModuleType("streamlit")

    st.secrets = {
        "db_user": "u", "db_password": "p", "db_host": "h",
        "db_port": "5432", "db_name": "d",
    }
    # SCENARIO["session"] 으로 세션 상태를 미리 심습니다. 대시보드는 값이 이미
    # 있으면 기본값으로 덮어쓰지 않으므로, 앱을 재시작하지 않고 사이드바를
    # 바꾼 것과 같은 상황을 만들 수 있습니다.
    st.session_state = _SessionState(SCENARIO.get("session") or {})
    # 확대 상태는 쿼리 파라미터에 있습니다(HTML <img> 클릭이 session_state 를
    # 건드릴 수 없기 때문). dict 하나면 대시보드가 쓰는 get/[]=/in/del 을 다 씁니다.
    st.query_params = dict(SCENARIO.get("query") or {})

    def cache_resource(fn):
        fn.clear = lambda: None
        return fn
    st.cache_resource = cache_resource

    def fragment(func=None, run_every=None):
        # 자동 갱신 주기는 화면 구조와 무관하므로 함수를 그대로 돌려줍니다.
        if func is None:
            return lambda f: f
        return func
    st.fragment = fragment

    st.set_page_config = lambda **kw: None
    st.title = lambda body, **kw: Rec.add("title", body)
    st.subheader = lambda body, **kw: Rec.add("subheader", body)
    st.caption = lambda body, **kw: Rec.add("caption", body)
    st.markdown = lambda body, **kw: Rec.add("markdown", body)
    st.info = lambda body, **kw: Rec.add("info", body)
    st.warning = lambda body, **kw: Rec.add("warning", body)
    st.error = lambda body, **kw: Rec.add("error", body)
    st.dataframe = lambda df, **kw: Rec.add("dataframe", "")
    # 실제 미니맵이 테스트 중 응답하면 로봇 상태 조각이 metric 경로까지
    # 실행된다. 외부 서버 유무와 무관하게 화면 구조 테스트가 같아야 한다.
    st.metric = lambda label, value, delta=None, **kw: Rec.add(
        "metric", f"{label}:{value}:{delta}")

    def button(label, on_click=None, args=(), **kw):
        # 누르지 않은 상태가 기본. SCENARIO["click"] 에 key 를 넣으면 눌린 것으로
        # 보고, Streamlit 과 같은 순서로 **콜백을 먼저** 실행합니다. 실제 Streamlit
        # 은 콜백을 돌린 뒤에 조각을 다시 그리므로, 효과는 다음 그리기에 보입니다.
        Rec.add("button", kw.get("key", label))
        clicked = kw.get("key") == SCENARIO.get("click")
        if clicked and on_click is not None:
            on_click(*args)
        return clicked
    st.button = button

    def download_button(label, data=None, file_name="", **kw):
        Rec.add("download", file_name)
        return False
    st.download_button = download_button

    def stop():
        raise AssertionError("st.stop() 호출됨 — 초기화 단계에서 죽었습니다")
    st.stop = stop

    def columns(spec, **kw):
        n = spec if isinstance(spec, int) else len(spec)
        Rec.add("columns", str(spec))
        return [_Container() for _ in range(n)]
    st.columns = columns

    def checkbox(label, value=False, **kw):
        # SCENARIO["checked"] 에 레이블을 넣으면 켜진 상태로 돌려줍니다.
        return True if label in SCENARIO.get("checked", ()) else value
    st.checkbox = checkbox

    def radio(label, options, key=None, format_func=None, **kw):
        Rec.add("radio", label)
        if key and key in st.session_state:
            return st.session_state[key]
        value = list(options)[0]
        if key:
            st.session_state[key] = value
        return value
    st.radio = radio

    def slider(label, min_value=None, max_value=None, value=None, key=None, **kw):
        Rec.add("slider", label)
        if key and key in st.session_state:
            return st.session_state[key]
        if key:
            st.session_state[key] = value
        return value
    st.slider = slider

    sidebar = types.SimpleNamespace(
        title=lambda body, **kw: Rec.add("sidebar", body),
        markdown=lambda body, **kw: Rec.add("sidebar", body),
        caption=lambda body, **kw: Rec.add("sidebar", body),
        text_input=lambda label, value="", **kw: value,
        checkbox=checkbox,
        button=button,
        radio=radio,
        slider=slider,
    )
    st.sidebar = sidebar

    # streamlit.components.v1.iframe / .html
    components = types.ModuleType("streamlit.components")
    v1 = types.ModuleType("streamlit.components.v1")
    v1.iframe = lambda src, height=None, scrolling=False: Rec.add("iframe", src)
    v1.html = lambda body, height=None, **kw: Rec.add("html", body)
    components.v1 = v1

    return st, components, v1


# ==========================================
# 가짜 psycopg2 — 실제 SQL 을 보고 알맞은 행을 돌려줍니다
# ==========================================
NOW = _dt.datetime(2026, 8, 24, 14, 32, 7)


class _OperationalError(Exception):
    pass


class _InterfaceError(Exception):
    pass

# 이 두 값은 DB에서 온 문자열이 그대로 HTML로 나가면 어떻게 되는지 보기 위한
# 미끼입니다. 화면 코드가 html.escape() 를 빠뜨리면 그대로 태그가 됩니다.
EVIL_OBJECT = '<img src=x onerror="alert(1)">'
EVIL_ACTION = "</div><script>alert('plan')</script>"

SCENARIO = {
    "danger": True, "plans": True, "db_fail": False, "init_fail": False,
    "plan_age": 12.0,
    "last_danger_id": 4210,   # fetch_summary 가 돌려줄 최신 위험 로그 id
    "checked": (),            # 켜진 것으로 볼 사이드바 체크박스 레이블
    "click": None,            # 눌린 것으로 볼 버튼 key
    "session": None,          # 미리 심어둘 session_state 값
    "query": None,            # 미리 심어둘 쿼리 파라미터 (확대 상태)
}

# 실행된 SQL 을 전부 모아둡니다 (어떤 쿼리를 쓰는지 검사하기 위해).
QUERIES = []
# (쿼리, 파라미터) 쌍. LIMIT 이나 기간처럼 %s 로 넘어가는 값은 쿼리 문자열에
# 안 남으므로, 그 값을 검사하려면 여기를 봐야 합니다.
PARAMS = []


def _zone_rows():
    if SCENARIO["danger"]:
        return [
            {"zone": "A", "risk_level": "정상", "detected_object": "작업자",
             "log_time": NOW, "age_seconds": 1.0},
            {"zone": "B", "risk_level": "위험", "detected_object": EVIL_OBJECT,
             "log_time": NOW, "age_seconds": 0.5},
            {"zone": "C", "risk_level": "주의", "detected_object": "장애물",
             "log_time": NOW, "age_seconds": 40.0},   # 10초 넘음 -> 미연결
        ]
    return [
        {"zone": "A", "risk_level": "정상", "detected_object": "작업자",
         "log_time": NOW, "age_seconds": 1.0},
    ]


class _Cursor:
    def __init__(self):
        self._rows = []
        self._one = None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        QUERIES.append(q)
        PARAMS.append((q, tuple(params or ())))

        if SCENARIO["init_fail"]:
            raise _OperationalError("초기 연결 단계부터 서버가 응답하지 않습니다")

        # 구체적인 것부터 봅니다. 감지 요약 쿼리에도 COUNT(*) 가 들어 있어서
        # 순서를 뒤집으면 위험 건수 쿼리로 오인됩니다.
        if "GROUP BY grp" in q:
            self._rows = []          # 감지 요약 (pandas 스텁이라 표는 안 그림)
        elif "DISTINCT ON (zone)" in q:
            self._rows = _zone_rows()
        elif "FROM response_plans" in q and "CASE risk_level" not in q:
            # 기록 화면(최신순) 조회. 여기서 행을 돌려주면 표를 그리려고 pandas 를
            # 건드리는데, 이 테스트는 pandas 를 빈 모듈로 스텁합니다. 표가 실제로
            # 그려지는지는 test_dashboard_tables.py 가 진짜 pandas 로 확인합니다.
            # 여기서는 화면 뼈대(제목/필터/닫기)와 쿼리 모양만 봅니다.
            self._rows = []
        elif "FROM response_plans" in q:
            # 패널(우선순위 정렬) 조회. 3건까지 받아서 첫 건이 크게, 나머지는
            # 한 줄 요약입니다.
            self._rows = [{
                "created_at": NOW, "zone": "B", "detected_object": "화재",
                "risk_level": "위험", "recommended_action": EVIL_ACTION,
                "reference_docs": "소방안전관리 지침 3.2\n대피 절차 A", "confidence": 0.85,
                "generation_mode": "keyword+llm",
                "age_seconds": SCENARIO["plan_age"],
            }, {
                "created_at": NOW, "zone": "A", "detected_object": "지게차",
                "risk_level": "위험", "recommended_action": "경로 이탈 확인",
                "reference_docs": "중장비 운영 지침 2.1", "confidence": 0.7,
                "generation_mode": "keyword",
                "age_seconds": 180.0,
            }, {
                "created_at": NOW, "zone": "C", "detected_object": "안전모 미착용",
                "risk_level": "주의", "recommended_action": "PPE 착용 안내",
                "reference_docs": "", "confidence": None,
                "generation_mode": "unknown",
                "age_seconds": 900.0,
            }] if SCENARIO["plans"] else []
        elif "COUNT(*)" in q:
            # 위험 건수 쿼리 = fetch_summary 의 첫 질의. DB 장애 시나리오는
            # 여기서 끊습니다 (init_all_tables 는 통과시켜야 st.stop() 을 피함).
            if SCENARIO["db_fail"]:
                raise _OperationalError("서버가 응답하지 않습니다")
            self._one = {
                "count": 3 if SCENARIO["danger"] else 0,
                "db_now": NOW,
                "last_danger_id": SCENARIO["last_danger_id"] if SCENARIO["danger"] else None,
            }
        else:
            self._rows = []          # 원본 로그, DDL 등
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Conn:
    closed = 0
    autocommit = False

    def cursor(self, **kw):
        return _Cursor()

    def commit(self):
        pass


def _make_psycopg2():
    m = types.ModuleType("psycopg2")
    m.connect = lambda **kw: _Conn()
    # 모듈 밖에서도 같은 클래스를 던져야 get_cursor() 의 except 에 걸립니다.
    m.OperationalError = _OperationalError
    m.InterfaceError = _InterfaceError

    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    m.extras = extras
    return m, extras


# ==========================================
def check(desc, condition):
    print(f"[{'OK ' if condition else 'FAIL'}] {desc}")
    if not condition:
        raise AssertionError(desc)


def load_dashboard():
    """가짜 모듈을 꽂고 대시보드를 새로 import 합니다."""
    Rec.events = []
    Rec.depth = 0
    QUERIES.clear()
    PARAMS.clear()

    st, components, v1 = _make_streamlit()
    psy, extras = _make_psycopg2()

    sys.modules["streamlit"] = st
    sys.modules["streamlit.components"] = components
    sys.modules["streamlit.components.v1"] = v1
    sys.modules["psycopg2"] = psy
    sys.modules["psycopg2.extras"] = extras
    # pandas 는 함수 안에서만 쓰이고, 이 테스트는 표를 그리는 경로를 타지
    # 않으므로(로그 0건 -> st.info) 빈 모듈로 충분합니다.
    sys.modules.setdefault("pandas", types.ModuleType("pandas"))

    for name in list(sys.modules):
        if name.startswith(("dashboard", "edge_video")):
            del sys.modules[name]

    import importlib
    return importlib.import_module("dashboard.smart_factory_dashboard_v3")


def main():
    print("=== 위험 상황 시나리오 ===")
    SCENARIO["danger"] = True
    SCENARIO["plans"] = True
    SCENARIO["db_fail"] = False
    SCENARIO["plan_age"] = 12.0
    mod = load_dashboard()

    # <style> 블록에도 sfp-* 문자열이 들어 있으므로, 실제로 그려진 태그만
    # 찾도록 class=" 까지 붙여서 검색합니다.
    i_banner = Rec.index_of("markdown", 'class="sfp-banner"')
    i_card = Rec.index_of("markdown", 'class="sfp-card ')
    i_bar = Rec.index_of("markdown", 'class="sfp-bar"')
    i_video = Rec.index_of("subheader", "실시간 순찰 영상")
    i_map = Rec.index_of("subheader", "순찰 위치 지도")
    i_detail = Rec.index_of("subheader", "상세 조회")

    check("경보 배너가 그려짐", i_banner >= 0)
    check("구역 카드가 그려짐", i_card >= 0)
    check("사이드바에 Jetson 스피커 출력 슬라이더가 그려짐",
          any(k == "slider" and "스피커 출력" in p for k, p, _d in Rec.events))

    print("\n--- 배치 순서 (v7의 핵심 수정) ---")
    check("경보 배너가 구역 카드보다 먼저", i_banner < i_card)
    check("구역 카드가 상태 바보다 먼저", i_card < i_bar)
    check("구역 상태가 영상보다 위 (스크롤 없이 보임)", i_bar < i_video)
    check("영상이 미니맵보다 위", i_video < i_map)
    check("상세 조회가 맨 아래", i_map < i_detail)

    print("\n--- 미니맵 폭 확보 ---")
    # v5~v11 까지는 "미니맵은 st.columns 밖(깊이 0)이어야 한다"가 규칙이었습니다.
    # 지도 옆에 AI 대응안을 두면서 지도도 칸 안으로 들어갔으므로, 규칙을
    # "지도가 그 줄의 대부분을 차지하는가"로 바꿉니다. 지키려던 것은 애초에
    # 깊이가 아니라 폭이었습니다 — 좁아지면 minimap_web.html 이 접힙니다.
    iframes = [(p, d) for k, p, d in Rec.events if k == "iframe"]
    check("미니맵 iframe 이 정확히 하나", len(iframes) == 1)

    idx_iframe = next(i for i, (k, _p, _d) in enumerate(Rec.events) if k == "iframe")
    enclosing = None
    for k, payload, depth in reversed(Rec.events[:idx_iframe]):
        if k == "columns" and depth == 0:
            enclosing = payload
            break
    check("미니맵을 감싼 columns 를 찾음", enclosing is not None)

    import ast
    spec = ast.literal_eval(enclosing)
    weights = [float(w) for w in spec] if isinstance(spec, (list, tuple)) else None
    check("  지도 줄이 2단 구성", weights is not None and len(weights) == 2)
    map_share = weights[0] / sum(weights)
    check("  지도가 그 줄의 60%% 이상을 차지 (실제 %.0f%%)" % (map_share * 100),
          map_share >= 0.6)

    videos = [(p, d) for k, p, d in Rec.events if k == "markdown" and "<img src=" in p]
    check("영상은 columns 안에 있음 (깊이 1)", any(d == 1 for _, d in videos))

    print("\n--- DB 문자열 이스케이프 (XSS) ---")
    body = Rec.html()
    check("미끼 문자열이 화면에 실제로 들어감", "alert(1)" in body or "alert(&#x27;plan&#x27;)" in body)
    check("탐지 객체명의 <img 태그가 이스케이프됨", "<img src=x onerror=" not in body)
    check("  대신 &lt; 로 escape 되어 남음", "&lt;img src=x" in body)
    check("대응안의 <script> 가 이스케이프됨", "<script>" not in body)
    check("  대응안 카드 자체는 그려짐", 'class="sfp-plan"' in body)
    check("근거 문서 여러 줄이 한 줄로 합쳐짐", "지침 3.2 · 대피 절차 A" in body)
    check("확신도가 백분율로 표시됨", "확신도 85%" in body)
    check("실제 LLM 채택 여부가 카드에 표시됨", "LLM 생성 · 키워드 RAG" in body)
    check("규칙 폴백도 LLM과 구분됨", "규칙 폴백 · 키워드 RAG" in body)

    print("\n--- _zone_style() 판정 ---")
    zs = mod._zone_style
    check("행이 없으면 데이터 없음", zs(None) == ("sfp-off", "데이터 없음"))
    check("10초 초과면 위험도와 무관하게 미연결",
          zs({"age_seconds": 40, "risk_level": "위험"})[0] == "sfp-off")
    check("위험", zs({"age_seconds": 1, "risk_level": "위험"}) == ("sfp-danger", "위험"))
    check("주의", zs({"age_seconds": 1, "risk_level": "주의"}) == ("sfp-warn", "주의"))
    check("정상", zs({"age_seconds": 1, "risk_level": "정상"}) == ("sfp-ok", "정상"))
    check("모르는 등급은 정상 취급이 아니라 정상 스타일로만 떨어짐",
          zs({"age_seconds": 1, "risk_level": "???"})[0] == "sfp-ok")

    print("\n--- C 구역은 40초째 무응답 -> 미연결로 표시 ---")
    check("미연결 카드가 존재", 'class="sfp-card sfp-off"' in body)
    check("무응답 초 수가 카드에 표시됨", "40초째 응답 없음" in body)

    print("\n--- (v8-7) 미연결 배너 — 로봇이 죽는 것도 사건입니다 ---")
    check("미연결 배너가 뜸", 'class="sfp-offline-banner"' in body)
    check("위험 배너와 다른 태그 (색으로 구분)", 'class="sfp-banner"' in body)
    check("몇 대인지 표시", "순찰 로봇 미연결 1대" in body)
    check("끊긴 구역을 지목", "C 구역 10초 이상 응답 없음" in body)

    print("\n--- (v8-8) 상태 바 숫자 ---")
    i_bar_html = [p for k, p, _ in Rec.events if k == "markdown" and 'class="sfp-bar"' in p]
    bar = i_bar_html[0] if i_bar_html else ""
    check("주의 칸이 생김", "🟡 주의" in bar)
    check("위험/정상/미연결도 그대로", all(x in bar for x in ("🔴", "🟢", "⚫", "🕐")))

    print("\n--- (v8-10) 감지 요약이 연속 구간을 접는가 ---")
    summary_q = [q for q in QUERIES if "GROUP BY grp" in q]
    check("요약 쿼리가 실행됨", len(summary_q) == 1)
    check("  gaps-and-islands 로 구간을 만듦", "ROW_NUMBER() OVER" in summary_q[0])
    check("  issue 로는 묶지 않음 (거리가 박혀 있어 매 프레임 달라짐)",
          "GROUP BY grp, zone, detected_object, risk_level" in summary_q[0])
    check("  구간을 구역별로 셈 ('전체'에서 다른 구역이 끼어들어도 안 쪼개지게)",
          "PARTITION BY zone ORDER BY" in summary_q[0])
    check("  동률이 나와도 순번이 흔들리지 않게 id 로 tie-break",
          "log_time DESC, id DESC" in summary_q[0])
    check("  거리 -1(측정 실패)은 최근접에서 제외",
          "FILTER (WHERE distance >= 0)" in summary_q[0])
    check("  원본을 통째로 훑지 않고 선택한 범위의 상한까지만",
          f"LIMIT {mod.DETAIL_SPANS[mod.DEFAULT_SPAN]['scan_rows']}" in summary_q[0])
    check("  시간으로도 자름 (화면에 적힌 '최근 N분'이 정직하도록)",
          "log_time >= LOCALTIMESTAMP - (%s * INTERVAL '1 minute')" in summary_q[0])

    print("\n--- (v8-11) 상세 조회 기본값과 구역 선택 ---")
    check("첫 화면은 전체 구역", mod.st.session_state["selected_zone"] == mod.ZONE_ALL)
    check("첫 화면은 감지 요약", mod.st.session_state["detail_view"] == mod.VIEW_SUMMARY)
    check("구역 선택지에 '전체'가 있음", mod.ZONE_OPTIONS[0] == mod.ZONE_ALL)
    check("상세 조회는 patrol_logs 2종만 (대응안은 자체 기록 화면으로 분리)",
          mod.DETAIL_VIEWS == [mod.VIEW_SUMMARY, mod.VIEW_RAW])
    check("대응안 조회가 구역을 받음 (v7까지 죽은 라디오였음)",
          "zone" in mod.fetch_plans.__code__.co_varnames)

    print("\n--- (v8-12) 대응안 카드 시각 ---")
    check("상대 시각이 붙음", "12초 전" in body)
    check("최근 대응안은 경보색 유지", 'class="sfp-plan"' in body)

    print("\n=== 오래된 대응안 시나리오 ===")
    SCENARIO["plan_age"] = 7200.0        # 2시간 전
    mod_old = load_dashboard()
    body_old = Rec.html()
    check("상대 시각이 시간 단위로", mod_old._ago(7200) == "2시간 전")
    check("오래된 대응안은 현재 경보 카드에서 숨김",
          'class="sfp-plan stale"' not in body_old)
    check("지난 대응안은 '이전 기록'에서 볼 수 있음",
          any(k == "button" and p == "plan_history_btn"
              for k, p, _ in Rec.events))

    print("\n=== DB 장애 시나리오 (v8-9) ===")
    SCENARIO["plan_age"] = 12.0
    SCENARIO["db_fail"] = True
    load_dashboard()
    body_fail = Rec.html()

    check("st.stop() 으로 죽지 않음 (여기까지 왔다는 것 자체가 증거)", True)
    check("상태 불명 배너가 뜸", 'class="sfp-mute-banner"' in body_fail)
    check("정상(초록)으로 보이지 않음", 'class="sfp-card sfp-ok"' not in body_fail)
    check("미연결(로봇 문제)로도 보이지 않음", 'class="sfp-offline-banner"' not in body_fail)
    check("구역 카드가 '상태 불명'", 'class="sfp-card sfp-unknown"' in body_fail)
    check("위험 0건이라고 단정하지 않음", "위험 <b>0</b>건" not in body_fail)
    check("영상/미니맵은 계속 나옴 (DB와 무관한 경로)",
          any(k == "iframe" for k, _, _ in Rec.events))

    print("\n=== DB 초기화 실패 시나리오 ===")
    SCENARIO["db_fail"] = False
    SCENARIO["init_fail"] = True
    load_dashboard()
    body_init_fail = Rec.html()
    check("초기화 실패에도 st.stop() 없이 화면 렌더링", True)
    check("초기화 실패 안내가 표시됨",
          any(k == "warning" and "데이터베이스 초기화 실패" in str(p)
              for k, p, _ in Rec.events))
    check("초기화 실패에도 영상/미니맵이 계속 나옴",
          any(k == "iframe" for k, _, _ in Rec.events))
    check("DB 상태를 정상으로 오인하지 않음", 'class="sfp-card sfp-ok"' not in body_init_fail)

    print("\n=== (v9) 경보 확인(acknowledge) ===")
    SCENARIO["init_fail"] = False
    SCENARIO["db_fail"] = False
    SCENARIO["plan_age"] = 12.0
    mod = load_dashboard()
    check("확인 전에는 배너가 뜸", 'class="sfp-banner"' in Rec.html())
    check("배너 옆에 확인 버튼", any(k == "button" and p == "ack_button"
                                     for k, p, _ in Rec.events))

    check("반환값이 아니라 on_click 콜백으로 처리 — 반환값으로 하면 배너가 이미 "
          "그려진 뒤라 다음 틱까지 남습니다", callable(mod._acknowledge))

    # 버튼을 누릅니다. Streamlit 은 콜백을 돌린 "뒤에" 조각을 다시 그리므로,
    # 여기서도 콜백이 실행된 상태에서 한 번 더 그려서 확인합니다.
    SCENARIO["click"] = "ack_button"
    mod = load_dashboard()
    check("확인 시점의 위험 id 가 기록됨",
          mod.st.session_state["ack_danger_id"] == SCENARIO["last_danger_id"])
    check("소리 기준도 같이 올라감 (확인 직후 소리만 한 번 더 나는 일 방지)",
          mod.st.session_state["played_danger_id"] == SCENARIO["last_danger_id"])

    SCENARIO["click"] = None
    Rec.events = []
    mod._render_status()          # 콜백 이후의 재실행
    body_ack = Rec.html()
    check("재실행에서 배너가 사라짐", 'class="sfp-banner"' not in body_ack)
    check("확인 상태가 화면에 남음", "위험 확인됨" in body_ack)
    check("구역 카드의 빨강은 그대로 ('봤다'이지 '해결됐다'가 아님)",
          'class="sfp-card sfp-danger"' in body_ack)

    # 확인한 뒤 새 위험이 들어오면 다시 떠야 합니다.
    keep_ack = SCENARIO["last_danger_id"]
    SCENARIO["last_danger_id"] = keep_ack + 1
    Rec.events = []
    mod._render_status()
    check("확인 이후의 새 위험은 배너가 다시 뜸", 'class="sfp-banner"' in Rec.html())
    SCENARIO["last_danger_id"] = keep_ack

    print("\n=== (v9) 소리 경보 ===")
    SCENARIO["last_danger_id"] = keep_ack
    load_dashboard()
    check("기본값(꺼짐)에서는 소리를 내지 않음",
          not any(k == "html" for k, _, _ in Rec.events))

    SCENARIO["checked"] = ("소리 경보",)
    mod = load_dashboard()
    sounds = [p for k, p, _ in Rec.events if k == "html"]
    check("켜면 새 위험에 한 번 울림", len(sounds) == 1)
    check("  파일이 아니라 코드에서 구운 WAV", "data:audio/wav;base64," in sounds[0])
    check("  자동재생", "autoplay" in sounds[0])
    check("  울린 id 를 기록 (같은 경보로 매 초 다시 울리지 않게)",
          mod.st.session_state["played_danger_id"] == SCENARIO["last_danger_id"])

    Rec.events = []
    mod._render_status()          # 같은 위험으로 한 번 더 그리기
    check("같은 위험이면 다시 울리지 않음",
          not any(k == "html" for k, _, _ in Rec.events))

    Rec.events = []
    SCENARIO["last_danger_id"] = keep_ack + 5
    mod._render_status()
    check("새 위험이 들어오면 다시 울림", any(k == "html" for k, _, _ in Rec.events))
    SCENARIO["checked"] = ()
    SCENARIO["last_danger_id"] = keep_ack

    print("\n=== (v9) 화면 구성 / 범위 / CSV ===")
    mod = load_dashboard()
    check("기본 화면 구성은 '전체'",
          mod.st.session_state["layout_mode"] == mod.LAYOUT_FULL)
    check("기본 범위는 최근 5분", mod.st.session_state["detail_span"] == mod.SPAN_5M)
    check("범위 라디오가 그려짐", any(k == "radio" and p == "범위"
                                       for k, p, _ in Rec.events))
    check("표에 CSV 내려받기가 붙음 — 데이터가 없으면 표가 없으니 함수로 확인",
          callable(mod._download_csv))
    check("넓은 범위는 갱신 주기가 길어짐",
          mod.DETAIL_SPANS[mod.SPAN_30M]["refresh"]
          > mod.DETAIL_SPANS[mod.SPAN_1M]["refresh"])
    check("넓은 범위는 스캔 상한도 커짐",
          mod.DETAIL_SPANS[mod.SPAN_30M]["scan_rows"]
          > mod.DETAIL_SPANS[mod.SPAN_1M]["scan_rows"])

    # '관제 집중' — 상세 조회를 접고 미니맵을 낮춥니다.
    SCENARIO["session"] = {"layout_mode": mod.LAYOUT_FOCUS}
    mod = load_dashboard()
    SCENARIO["session"] = None
    i_map = Rec.index_of("subheader", "순찰 위치 지도")
    i_detail = Rec.index_of("subheader", "상세 조회")
    check("집중 모드에서 상세 조회가 접힘", i_detail == -1)
    check("  지도는 그대로 나옴", i_map >= 0)
    check("  집중 모드에서도 지도가 한 칸 안에 하나만 그려짐",
          len([1 for k, _, _ in Rec.events if k == "iframe"]) == 1)
    check("  접혔다는 안내가 있음",
          any(k == "caption" and "관제 집중" in p for k, p, _ in Rec.events))

    print("\n=== (v10) 클릭 확대 ===")
    mod = load_dashboard()
    check("기본은 확대 아님", mod._expanded_panel() is None)
    check("영상이 확대 링크로 감싸져 있음",
          f'href="?{mod.EXPAND_PARAM}={mod.EXPAND_VIDEO}"' in Rec.html())
    check("  같은 탭에서 열림 (target=_self 가 없으면 새 탭)",
          'target="_self"' in Rec.html())
    check("  커서로도 확대임을 알림", "cursor:zoom-in" in Rec.html())
    check("지도는 링크로 감싸지 않음 — iframe 클릭은 지도 자체 조작",
          not any(k == "iframe" and "href" in p for k, p, _ in Rec.events))
    check("영상/지도 모두 버튼 경로도 있음",
          {"expand_video_btn", "expand_map_btn"}
          <= {p for k, p, _ in Rec.events if k == "button"})

    # --- 영상 확대 ---
    SCENARIO["query"] = {mod.EXPAND_PARAM: mod.EXPAND_VIDEO}
    mod = load_dashboard()
    body_v = Rec.html()
    check("영상 확대가 인식됨", mod._expanded_panel() == mod.EXPAND_VIDEO)
    check("영상이 columns 밖 = 본문 전체 폭",
          any("<img src=" in p and d == 0 for k, p, d in Rec.events if k == "markdown"))
    check("  세로가 화면을 넘지 않게 잘림", "max-height:78vh" in body_v)
    check("  잘리지 않고 축소됨 (object-fit:contain)", "object-fit:contain" in body_v)
    check("  다시 누르면 닫히는 링크", 'href="?"' in body_v)
    check("  커서가 축소 표시", "cursor:zoom-out" in body_v)
    check("지도는 접힘", not any(k == "iframe" for k, _, _ in Rec.events))
    check("상세 조회도 접힘", Rec.index_of("subheader", "상세 조회") == -1)
    check("경보/구역 상태는 그대로 — 확대가 경보를 가리면 사고가 됩니다",
          'class="sfp-card ' in body_v and 'class="sfp-bar"' in body_v)
    check("닫기 버튼이 있음", any(k == "button" and p == "expand_close"
                                   for k, p, _ in Rec.events))

    # --- 지도 확대 ---
    SCENARIO["query"] = {mod.EXPAND_PARAM: mod.EXPAND_MAP}
    mod = load_dashboard()
    iframes = [(p, d) for k, p, d in Rec.events if k == "iframe"]
    check("지도 확대가 인식됨", mod._expanded_panel() == mod.EXPAND_MAP)
    check("지도 iframe 이 하나", len(iframes) == 1)
    check("  여전히 columns 밖 (폭은 어떤 모드에서도 안 줄임)", iframes[0][1] == 0)
    check("영상은 접힘",
          not any(k == "markdown" and "<img src=" in p for k, p, _ in Rec.events))
    check("경보/구역 상태는 그대로", 'class="sfp-card ' in Rec.html())

    # --- 이상한 값은 무시 ---
    SCENARIO["query"] = {mod.EXPAND_PARAM: ";drop table"}
    mod = load_dashboard()
    check("모르는 확대 값은 무시하고 평소 화면", mod._expanded_panel() is None)
    check("  영상과 지도가 둘 다 나옴",
          any(k == "iframe" for k, _, _ in Rec.events)
          and any(k == "markdown" and "<img src=" in p for k, p, _ in Rec.events))
    SCENARIO["query"] = None

    # --- 버튼 경로 ---
    SCENARIO["click"] = "expand_map_btn"
    mod = load_dashboard()
    check("버튼을 누르면 쿼리 파라미터가 설정됨",
          mod.st.query_params.get(mod.EXPAND_PARAM) == mod.EXPAND_MAP)
    SCENARIO["click"] = None
    SCENARIO["query"] = {mod.EXPAND_PARAM: mod.EXPAND_MAP}
    SCENARIO["click"] = "expand_close"
    mod = load_dashboard()
    check("닫기를 누르면 파라미터가 지워짐",
          mod.EXPAND_PARAM not in mod.st.query_params)
    SCENARIO["click"] = None
    SCENARIO["query"] = None

    print("\n=== (v11) 대응안 패널 + 이전 기록 ===")
    mod = load_dashboard()
    body_panel = Rec.html()
    check("첫 건은 큰 카드", 'class="sfp-plan"' in body_panel)
    check("나머지는 한 줄 요약", 'class="sfp-plan-row' in body_panel)
    check("  요약 줄에는 권장 조치 문장을 넣지 않음 (세 줄이 다 문장이면 못 고름)",
          body_panel.count('class="a"') == 1
          and "경로 이탈 확인" not in body_panel)
    check("  요약 줄에 상대 시각", "3분 전" in body_panel)
    check("  5분 넘은 지난 대응안은 현재 패널에서 숨김",
          "15분 전" not in body_panel)
    check("  요약 줄도 위험/주의를 색으로 구분",
          'class="sfp-plan-row danger"' in body_panel)
    check("  오래된 것을 회색 현재 경보로 남기지 않음",
          'class="sfp-plan-row"' not in body_panel)
    check("이전 기록 버튼이 있음",
          any(k == "button" and p == "plan_history_btn" for k, p, _ in Rec.events))
    check("패널은 3건까지만", mod.PLAN_PANEL_LIMIT == 3)

    panel_q = [q for q in QUERIES if "FROM response_plans" in q]
    check("패널 조회가 우선순위 정렬을 씀", any("CASE risk_level" in q for q in panel_q))
    check("  신선한 것 안에서만 심각도로 줄세움 (오래된 건 -1로 눕힘)",
          any("ELSE -1 END DESC" in q for q in panel_q))
    check("  그 다음은 최신순", any("ELSE -1 END DESC, created_at DESC" in
                                     " ".join(q.split()) for q in panel_q))

    print("\n--- 기록 화면으로 들어가기 ---")
    SCENARIO["click"] = "plan_history_btn"
    mod = load_dashboard()
    check("버튼을 누르면 기록 화면 주소가 됨",
          mod.st.query_params.get(mod.EXPAND_PARAM) == mod.EXPAND_PLANS)

    SCENARIO["click"] = None
    SCENARIO["query"] = {mod.EXPAND_PARAM: mod.EXPAND_PLANS}
    mod = load_dashboard()
    check("기록 화면이 열림", Rec.index_of("subheader", "AI 대응안 기록") >= 0)
    check("  자체 구역/기간 필터", {"구역", "기간"} <=
          {p for k, p, _ in Rec.events if k == "radio"})
    check("  기간 기본값은 24시간",
          mod.st.session_state["plan_span"] == mod.PLAN_SPAN_24H)
    check("  구역 기본값은 전체 — 상세 조회 필터를 물려받지 않음",
          mod.st.session_state["plan_zone"] == mod.ZONE_ALL)
    check("  닫기 버튼", any(k == "button" and p == "plan_history_close"
                              for k, p, _ in Rec.events))
    check("영상/지도/상세 조회는 접힘",
          not any(k == "iframe" for k, _, _ in Rec.events)
          and Rec.index_of("subheader", "상세 조회") == -1)
    check("경보/구역 상태는 그대로", 'class="sfp-card ' in Rec.html())

    hist_q = [q for q in QUERIES if "FROM response_plans" in q]
    check("기록 조회는 최신순 (우선순위 정렬 아님)",
          any("ORDER BY created_at DESC" in q for q in hist_q))
    # LIMIT 은 %s 파라미터라 쿼리 문자열에 숫자가 안 남습니다. 실제로 넘어간
    # 값은 PARAMS 에서 봅니다.
    hist_params = [p for q, p in PARAMS
                   if "FROM response_plans" in q and "ORDER BY created_at DESC" in q]
    check("  기록은 패널보다 많이 가져옴",
          bool(hist_params) and hist_params[-1][-1] == mod.PLAN_HISTORY_LIMIT
          and mod.PLAN_HISTORY_LIMIT > mod.PLAN_PANEL_LIMIT)
    check("  기간이 분 단위로 걸림",
          any("created_at >= LOCALTIMESTAMP" in q for q in hist_q))
    check("  기본 24시간이 분으로 넘어감",
          mod.PLAN_SPAN_MINUTES[mod.PLAN_SPAN_24H] in hist_params[-1])

    print("\n--- '전체 기록' 은 시간 조건을 아예 안 검 ---")
    SCENARIO["session"] = {"plan_span": mod.PLAN_SPAN_ALL}
    mod = load_dashboard()
    hist_all = [q for q in QUERIES if "FROM response_plans" in q
                and "ORDER BY created_at DESC" in q]
    check("전체 기록이면 WHERE 가 없음",
          all("created_at >= LOCALTIMESTAMP" not in q for q in hist_all))
    check("  patrol_logs 에는 '전체' 선택지를 주지 않음 (30fps라 위험)",
          mod.PLAN_SPAN_ALL not in mod.DETAIL_SPAN_OPTIONS)
    SCENARIO["session"] = None
    SCENARIO["query"] = None

    print("\n=== 평온한 시나리오 (위험 0건, 대응안 없음) ===")
    SCENARIO["danger"] = False
    SCENARIO["plans"] = False
    SCENARIO["db_fail"] = False
    SCENARIO["click"] = None
    SCENARIO["checked"] = ()
    load_dashboard()
    body2 = Rec.html()

    check("위험이 없으면 경보 배너가 안 뜸", 'class="sfp-banner"' not in body2)
    check("구역 카드는 그대로 그려짐", 'class="sfp-card ' in body2)
    check("로그가 없는 구역은 '데이터 없음'", "데이터 없음" in body2)
    check("한 번도 수신 없는 구역은 '수신 이력 없음'으로 따로 안내",
          "수신 이력 없음" in body2)
    check("대응안이 없으면 안내 문구", "현재 활성 경보가 없습니다" in body2)
    check("미니맵은 여전히 그려짐", any(k == "iframe" for k, _, _ in Rec.events))

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
