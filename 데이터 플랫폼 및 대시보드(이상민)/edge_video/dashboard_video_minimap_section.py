"""
dashboard_video_minimap_section.py

smart_factory_dashboard_v3.py 안에 통합되는 "영상 + 미니맵 표시" 섹션.

v3 변경 사항
--------------------
v2까지는 <img> 태그를 만들기 "전에" 파이썬(Streamlit 서버 프로세스)이
socket.create_connection()으로 미리 연결을 찔러보고, 그게 성공해야만
<img> 태그를 만들었습니다. 그런데 이 사전 검사가 실제 문제였습니다:

    - 브라우저로 http://젯슨IP:8500/ 에 직접 접속하면 정상적으로 열림
    - 그런데 대시보드에서는 "연결할 수 없습니다" 경고만 뜸

즉 실제 영상 연결(브라우저 <-> 젯슨)은 멀쩡한데, 그 앞을 지키던 파이썬 쪽
사전 검사(1초 타임아웃)가 젯슨이 VLM 추론으로 바쁠 때 응답이 늦어지면서
False를 반환해 <img> 태그 자체를 못 만들게 막고 있었던 것입니다.

v3에서는 이 사전 검사를 없애고, <img> 태그를 항상 만들어서 브라우저가
직접 연결을 시도하게 합니다. (이 원칙은 v4/v5에서도 그대로 유지합니다.)

v4 변경 사항
--------------------
미니맵이 MJPEG 이미지에서 "조작 가능한 페이지"로 바뀌었습니다.

    v3까지: <img src="http://다은IP:8091/minimap_feed">
            서버가 매 프레임 지도 전체를 JPEG로 구워서 보내주는 정지영상.
            확대도, 클릭도, 탐지 목록도 없음.

    v4부터: <iframe src="http://다은IP:8091/">
            minimap_renderer.py 가 서빙하는 minimap_web.html.
            지도는 최초 1회만 받고 이후에는 좌표 JSON만 오가므로 대역폭이
            훨씬 적게 들고, 확대/이동/로봇 추적/탐지 목록 클릭이 됩니다.
            연결이 끊기면 페이지 자체가 "서버 응답 없음" 상태를 표시합니다.

<img> 대신 iframe을 쓰기 때문에 Streamlit의 st.markdown(unsafe_allow_html)
경로로는 안 됩니다(Streamlit이 iframe 태그를 걸러냅니다). 이 용도로 있는
streamlit.components.v1.iframe() 을 씁니다.

기존 MJPEG(/minimap_feed)도 서버에 그대로 살아 있으므로, 되돌리고 싶으면
MINIMAP_LEGACY_MJPEG=1 로 실행하면 v3 방식으로 표시됩니다.

v5 변경 사항 (중요) — 영상/미니맵을 나란히 두지 않습니다
--------------------
v4까지는 render_video_and_minimap() 하나가 st.columns(2)로 영상과 미니맵을
반씩 나눠 가졌습니다. 그런데 minimap_web.html 은 자기 폭이 900px 밑으로
떨어지면 좁은 화면용 레이아웃으로 접히도록 만들어져 있습니다.

    .stage{grid-template-columns:1fr 296px}          /* 지도 | 탐지목록 */
    @media (max-width:900px){ .stage{...1fr; 목록이 지도 아래로} }

1920px 모니터에서 Streamlit wide 레이아웃의 본문 폭은 사이드바와 여백을 빼면
대략 1490px입니다. 그 절반은 약 745px — 즉 **미니맵은 대시보드 안에서 한 번도
설계된 넓은 레이아웃으로 뜬 적이 없었습니다.** 탐지 목록이 지도 밑으로 내려가
44vh를 차지하는 바람에, 520px 높이 중 지도에 남는 건 300px 남짓이었습니다.

그래서 v5에서는 함수를 둘로 쪼갰습니다.

    render_video(ip)     — 영상만. 좁은 칸에 넣어도 문제없음(그냥 MJPEG).
    render_minimap(ip)   — 미니맵만. **전체 폭 한 줄을 통째로 쓰라고** 분리.

대시보드는 미니맵을 st.columns 안이 아니라 본문 한 줄에 그대로 놓습니다.
render_video_and_minimap()은 예전 호출부 호환을 위해 남겨둔 얇은 wrapper입니다.
"""

import html
import os
import time
from urllib.parse import quote

import streamlit as st
import streamlit.components.v1 as components

# common/schema.py 패턴과 동일하게 환경변수로 설정 (하드코딩 금지)
JETSON_IP = os.environ.get("JETSON_IP", "203.0.113.10")
JETSON_VIDEO_PORT = os.environ.get("JETSON_VIDEO_PORT", "8500")

DAEUN_LAPTOP_IP = os.environ.get("DAEUN_LAPTOP_IP", "203.0.113.20")
MINIMAP_PORT = os.environ.get("MINIMAP_PORT", "8091")

# minimap_renderer.py 에서 MINIMAP_TOKEN 을 켰다면 여기에도 같은 값을 넣어야 합니다.
MINIMAP_TOKEN = os.environ.get("MINIMAP_TOKEN", "").strip()

# 터틀봇 상태(배터리·속도·순찰)는 미니맵 서버를 거쳐서 옵니다.
#
# 대시보드는 이상민님 PC 에서 돌고 ROS2 가 없습니다. 반면 미니맵 서버는 이미
# ROS2 에 붙어 있으므로, /battery_state 같은 토픽을 그쪽이 받아 /state 에
# 얹어주는 편이 배선이 하나로 끝납니다. 무거운 계산도 그쪽 노트북이 맡습니다.
#
# 대신 **미니맵 서버가 꺼져 있으면 로봇 상태도 안 보입니다.** 그때 빈 화면 대신
# "미니맵 서버에 연결하지 못했습니다"라고 이유를 밝히는 것이 아래 코드의 목적입니다.
STATUS_URL = f"http://{DAEUN_LAPTOP_IP}:{MINIMAP_PORT}/state"
# 순찰 제어. 이 두 주소는 **실물 로봇을 움직입니다.**
PATROL_URL = f"http://{DAEUN_LAPTOP_IP}:{MINIMAP_PORT}/patrol"
PATROL_PLAN_URL = f"http://{DAEUN_LAPTOP_IP}:{MINIMAP_PORT}/patrol_plan"
STATUS_HEADERS = {"X-Minimap-Token": MINIMAP_TOKEN} if MINIMAP_TOKEN else {}
STATUS_TIMEOUT_SEC = float(os.environ.get("ROBOT_STATUS_TIMEOUT_SEC", "2.0"))
STATUS_CACHE_SEC = float(os.environ.get("ROBOT_STATUS_CACHE_SEC", "2.0"))

# 미니맵 iframe 높이(px). 전체 폭을 쓰게 됐으므로 지도가 가로로 길어졌습니다.
# 너무 낮추면 오른쪽 탐지 목록에서 카드가 2~3장밖에 안 보입니다.
MINIMAP_HEIGHT = int(os.environ.get("MINIMAP_HEIGHT", "560"))

# "크게 보기"에서 쓰는 높이. components.iframe 은 px 만 받고 파이썬은 브라우저
# 창 높이를 모르므로 vh 로 줄 수가 없습니다. 1080p 에서 위쪽 경보/카드를 남기고도
# 넉넉한 값으로 잡았습니다. 노트북(768p)에서 스크롤이 길면 이 환경변수를 낮추세요.
MINIMAP_EXPANDED_HEIGHT = int(os.environ.get("MINIMAP_EXPANDED_HEIGHT", "820"))

# 1 로 두면 v3의 MJPEG 방식으로 되돌립니다.
MINIMAP_LEGACY_MJPEG = os.environ.get("MINIMAP_LEGACY_MJPEG", "").strip() == "1"


# 연결 실패 시 보여줄 자리표시자.
# 예전에는 via.placeholder.com 을 썼는데, 폐쇄망에서는 그 이미지도 못 받아와서
# 깨진 아이콘만 남았습니다. 외부 요청이 없도록 SVG를 그대로 박아 넣습니다.
def _placeholder(text: str) -> str:
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'>"
        "<rect width='100%' height='100%' fill='#222a33'/>"
        "<text x='50%' y='50%' fill='#8a97a6' font-family='sans-serif' "
        "font-size='20' text-anchor='middle' dominant-baseline='middle'>"
        f"{text}</text></svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


def _video_url(ip: str, port: str) -> str:
    # ai_inference_sender.py의 StreamingHandler는 경로 '/' 에서 MJPEG를 서빙함
    # (camera_stream_server.py를 대신 쓰는 경우엔 '/video_feed' 이므로, 그 경우엔
    #  이 함수를 f"http://{ip}:{port}/video_feed" 로 바꿔서 쓰면 됨)
    return f"http://{ip}:{port}/"


def _minimap_base(ip: str, port: str) -> str:
    """토큰이 붙지 않은 주소. 화면에 안내용으로 표시할 때 씁니다."""
    if MINIMAP_LEGACY_MJPEG:
        return f"http://{ip}:{port}/minimap_feed"
    return f"http://{ip}:{port}/"


def _minimap_url(ip: str, port: str) -> str:
    """실제로 불러올 주소. 토큰이 설정돼 있으면 ?t= 로 함께 넘깁니다.
    미니맵 페이지는 이 토큰을 자기 API 요청에도 그대로 이어서 씁니다."""
    url = _minimap_base(ip, port)
    if MINIMAP_TOKEN:
        url += f"?t={quote(MINIMAP_TOKEN)}"
    return url


def render_video(jetson_ip: str = None, show_caption: bool = True,
                 link: str = None, expanded: bool = False):
    """젯슨 카메라 영상(MJPEG)만 그립니다. 좁은 칸에 넣어도 됩니다.

    파이썬(Streamlit 서버) 쪽에서 미리 연결을 확인하지 않고, 브라우저가 직접
    연결을 시도하게 합니다(v3에서 얻은 교훈). 실패하면 onerror로 자리표시자를
    띄웁니다 — 깨진 이미지 아이콘 대신 "영상 연결 실패"라고 읽히게 하기 위함입니다.

    link 를 주면 영상 전체를 그 주소로 가는 링크로 감쌉니다. 대시보드는 여기에
    "?expand=video" 같은 쿼리 파라미터 주소를 넘겨서 **클릭하면 확대**되게 씁니다.
    MJPEG 은 그냥 정지영상 스트림이라 자체 조작이 없으므로, 클릭을 가로채도
    뺏기는 기능이 없습니다. (미니맵은 반대입니다 — 그쪽 클릭은 지도 자신의
    확대/이동 조작이므로 render_minimap 에는 이 인자를 두지 않았습니다.)

    expanded 는 확대 상태에서의 표시만 바꿉니다. 폭은 어차피 100% 라서, 여기서
    할 일은 세로가 화면을 넘지 않게 자르는 것뿐입니다.
    """
    v_ip = jetson_ip or JETSON_IP
    url = _video_url(v_ip, JETSON_VIDEO_PORT)

    style = "width:100%; border-radius:10px; display:block; background:#222a33;"
    if expanded:
        # object-fit:contain 이라 잘리지 않고 축소됩니다. 이게 없으면 세로로 긴
        # 화면에서 영상이 화면 밖까지 늘어나서 오히려 덜 보입니다.
        style += " max-height:78vh; object-fit:contain;"

    img = (
        f'<img src="{url}" style="{style}" '
        f'onerror="this.onerror=null; this.src=\'{_placeholder("영상 연결 실패")}\';">'
    )
    if link:
        # target="_self" 가 없으면 Streamlit 이 새 탭에서 엽니다.
        img = (
            f'<a href="{html.escape(link, quote=True)}" target="_self" '
            f'title="{"클릭하면 원래 크기로" if expanded else "클릭하면 확대"}" '
            f'style="display:block; cursor:zoom-{"out" if expanded else "in"};">'
            f'{img}</a>'
        )

    st.markdown(img, unsafe_allow_html=True)
    if show_caption:
        st.caption(f"젯슨 {url} · 박스는 젯슨이 그려서 보냅니다")


# 직전 응답을 짧게 들고 있습니다.
#
# Streamlit 은 위젯을 건드릴 때마다 스크립트를 처음부터 다시 실행합니다. 캐시가
# 없으면 체크박스 한 번 누를 때마다 미니맵 서버로 요청이 한 번씩 더 나갑니다.
# st.cache_data 를 쓰지 않는 이유는 Streamlit 버전에 따라 없는 경우가 있어서입니다.
_status_cache = {"at": 0.0, "url": None, "value": (None, "")}


def _fetch_robot_status(url: str):
    """미니맵 서버의 /state 에서 로봇 상태만 가져옵니다.

    돌려주는 값은 (상태 dict, 오류 문자열) 입니다. 실패를 예외로 올리지 않는
    이유는, 미니맵이 꺼져 있다고 대시보드 전체가 죽으면 안 되기 때문입니다.
    """
    now = time.monotonic()
    if (_status_cache["url"] == url
            and now - _status_cache["at"] < STATUS_CACHE_SEC):
        return _status_cache["value"]

    result = _request_robot_status(url)
    _status_cache.update(at=now, url=url, value=result)
    return result


def _request_robot_status(url: str):
    try:
        import requests
    except ImportError:
        return None, "requests 패키지가 없어 로봇 상태를 가져올 수 없습니다."
    try:
        resp = requests.get(url, headers=STATUS_HEADERS, timeout=STATUS_TIMEOUT_SEC)
    except Exception as exc:                                   # noqa: BLE001
        return None, f"미니맵 서버에 연결하지 못했습니다 ({type(exc).__name__})."
    if resp.status_code == 403:
        return None, "미니맵 서버가 요청을 거부했습니다 (MINIMAP_TOKEN 을 확인하세요)."
    if resp.status_code != 200:
        return None, f"미니맵 서버 응답 오류 (HTTP {resp.status_code})."
    try:
        return resp.json().get("robot_status") or {}, ""
    except ValueError:
        return None, "미니맵 서버 응답을 이해하지 못했습니다."


def _battery_tone(percent):
    if percent is None:
        return "off"
    if percent <= 15:
        return "bad"
    if percent <= 35:
        return "warn"
    return "ok"


def render_robot_status(daeun_ip: str = None):
    """터틀봇 상태창 — 배터리 · 속도 · 순찰 진행.

    값이 없을 때 0 으로 채우지 않습니다. 배터리를 아직 달지 않았거나
    시뮬레이션이면 /battery_state 자체가 오지 않는데, 그걸 "0%"로 그리면
    방전된 것처럼 보입니다. "값 없음"과 "0%"는 다른 상태입니다.
    """
    m_ip = daeun_ip or DAEUN_LAPTOP_IP
    url = f"http://{m_ip}:{MINIMAP_PORT}/state"
    status, error = _fetch_robot_status(url)

    if error:
        st.warning(error)
        st.caption(f"상태 출처: {url}")
        return

    status = status or {}
    battery = status.get("battery")
    speed = status.get("speed")
    patrol = status.get("patrol")

    # --- 배터리 ---
    percent = (battery or {}).get("percent")
    tone = _battery_tone(percent)
    if battery is None:
        bat_text = "연결 안 됨"
        bat_sub = "터틀봇이 /battery_state 를 보내지 않습니다 (배터리 미장착 또는 시뮬레이션)."
        width = 0
    else:
        charging = " ⚡충전 중" if battery.get("charging") else ""
        volt = battery.get("voltage")
        bat_text = ("—" if percent is None else f"{percent:.0f}%") + charging
        bat_sub = f"{volt:.2f} V" if volt is not None else "전압 정보 없음"
        width = 0 if percent is None else max(0, min(100, percent))

    colors = {"ok": "#2e7d32", "warn": "#ef6c00", "bad": "#c62828", "off": "#607d8b"}
    bar = colors[tone]
    st.markdown(
        f"""
        <div style="border:1px solid #33414f;border-radius:10px;padding:10px 12px;
                    background:#1c242c;">
          <div style="display:flex;justify-content:space-between;align-items:baseline;">
            <span style="color:#8a97a6;font-size:12px;">배터리</span>
            <b style="color:{bar};font-size:18px;">{html.escape(bat_text)}</b>
          </div>
          <div style="height:8px;border-radius:4px;background:#2b3641;margin:7px 0 5px;">
            <div style="height:8px;border-radius:4px;width:{width:.0f}%;background:{bar};"></div>
          </div>
          <div style="color:#8a97a6;font-size:11px;">{html.escape(bat_sub)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # --- 순찰 / 속도 ---
    col_p, col_s = st.columns(2)
    with col_p:
        if patrol is None:
            st.metric("순찰", "연결 안 됨")
        else:
            label = {
                "patrolling": "순찰 중",
                "paused": "일시정지",
                "navigating": "선택 위치 이동",
                "idle": "대기",
            }.get(
                patrol.get("state"), patrol.get("state", "—"))
            total = patrol.get("total") or 0
            index = patrol.get("index") or 0
            st.metric("순찰", label,
                      f"{index}/{total} 지점" if total else None)
    with col_s:
        if speed is None:
            st.metric("속도", "연결 안 됨")
        else:
            st.metric("속도", f"{speed.get('linear', 0.0):.2f} m/s",
                      f"회전 {speed.get('angular', 0.0):.2f} rad/s")

    ages = [status.get("battery_age_sec"), status.get("speed_age_sec"),
            status.get("patrol_age_sec")]
    ages = [a for a in ages if a is not None]
    if ages:
        st.caption(f"마지막 수신 {min(ages):.0f}초 전 · 출처 {m_ip}:{MINIMAP_PORT}")
    else:
        st.caption(f"출처 {m_ip}:{MINIMAP_PORT} · 아직 받은 값이 없습니다")


def full_width_kwargs():
    """st.dataframe 을 본문 폭에 맞추는 인자를 Streamlit 버전에 맞춰 고릅니다.

    1.49 에서 use_container_width= 가 deprecated 되고 width="stretch" 로
    대체됐습니다. requirements.txt 가 버전을 안 박아둬서 새 PC에 설치하면
    최신 버전이 깔리는데, 거기에 옛 인자를 넘기면 경고가 뜨거나(이후 버전에서는)
    아예 죽습니다. 반대로 구버전에 width="stretch" 를 넘기면 폭이 문자열로
    해석되어 표가 깨집니다. 그래서 둘 중 맞는 쪽을 골라 씁니다.

    __version__ 을 못 읽는 환경(self_test 의 가짜 streamlit)에서는 옛 인자로
    떨어집니다 — 그쪽은 표를 실제로 그리지 않으므로 문제가 없습니다.
    """
    import streamlit as st

    try:
        major, minor = (int(part) for part in str(st.__version__).split(".")[:2])
    except (AttributeError, ValueError):
        return {"use_container_width": True}
    return {"width": "stretch"} if (major, minor) >= (1, 49) else {"use_container_width": True}


def _patrol_urls(ip: str):
    base = f"http://{ip}:{MINIMAP_PORT}"
    return base + "/patrol", base + "/patrol_plan"


def _fetch_patrol_plan(url: str):
    """지금 지도로 만든 순찰 경로 미리보기. (계획, 오류문구) 를 돌려줍니다.

    실행은 하지 않습니다. 로봇을 움직이기 전에 "몇 개 지점을 얼마나 도는지" 를
    사람이 먼저 보게 하려는 것입니다.
    """
    try:
        import requests
    except ImportError:
        return None, "requests 패키지가 없습니다."
    try:
        resp = requests.get(url, headers=STATUS_HEADERS, timeout=STATUS_TIMEOUT_SEC)
    except Exception as exc:                                   # noqa: BLE001
        return None, f"미니맵 서버에 연결하지 못했습니다 ({type(exc).__name__})."
    if resp.status_code == 403:
        return None, "미니맵 서버가 요청을 거부했습니다 (MINIMAP_TOKEN 을 확인하세요)."
    if resp.status_code != 200:
        return None, f"미니맵 서버 응답 오류 (HTTP {resp.status_code})."
    try:
        return resp.json(), ""
    except ValueError:
        return None, "미니맵 서버 응답을 이해하지 못했습니다."


def _post_patrol(url: str, command: str):
    """순찰 제어 명령 전송. (True/False/None, 메시지)를 돌려줍니다.

    None은 ROS 메시지는 발행했지만 제한 시간 안에 제어기 ACK를 확인하지
    못한 상태입니다. 실패나 성공으로 단정하지 않고 화면에 경고로 표시합니다.
    """
    try:
        import requests
    except ImportError:
        return False, "requests 패키지가 없습니다."
    try:
        resp = requests.post(url, json={"command": command},
                             headers=STATUS_HEADERS, timeout=STATUS_TIMEOUT_SEC)
    except Exception as exc:                                   # noqa: BLE001
        return False, f"미니맵 서버에 연결하지 못했습니다 ({type(exc).__name__})."

    payload = {}
    try:
        payload = resp.json()
    except ValueError:
        pass

    if resp.status_code == 403:
        return False, ("순찰 제어가 꺼져 있습니다. 미니맵 서버에 MINIMAP_TOKEN 을 "
                       "설정하고, 이 대시보드에도 같은 값을 넣으세요.")
    if resp.status_code == 202 and payload.get("pending"):
        return None, "명령은 전송됐지만 제어기 접수 확인을 기다리는 중입니다."
    if resp.status_code != 200 or not payload.get("ok"):
        return False, payload.get("error") or f"명령 실패 (HTTP {resp.status_code})."

    plan = payload.get("plan")
    if plan:
        return True, (f"순찰을 시작했습니다 — 지점 {len(plan.get('waypoints', []))}개 · "
                      f"한 바퀴 {plan.get('total_distance_m', 0):.1f}m")
    return True, "명령을 보냈습니다."


def render_patrol_control(daeun_ip: str = None):
    """순찰 시작 / 정지 / 재개 패널.

    **페이지가 열릴 때 자동으로 시작하지 않습니다.** Streamlit 은 버튼 하나만
    눌러도 스크립트를 처음부터 다시 실행하므로, 여기서 자동 시작을 걸면 관제자가
    화면을 만질 때마다 로봇에게 "처음부터 다시 돌아라" 를 보내게 됩니다.
    실물 장비를 움직이는 시작·재개 명령은 확인 단계를 한 번 둡니다.
    반면 정지는 추가 확인 없이 즉시 전송합니다.

    (정말로 무인 자동 시작이 필요하면, 대시보드가 아니라 로봇을 띄우는 launch
     스크립트에서 `/patrol` 을 한 번 호출하는 쪽이 맞습니다.)
    """
    import streamlit as st

    m_ip = (daeun_ip or DAEUN_LAPTOP_IP).strip()
    patrol_url, plan_url = _patrol_urls(m_ip)

    status, _err = _fetch_robot_status(f"http://{m_ip}:{MINIMAP_PORT}/state")
    # 미니맵은 순찰 노드가 아직 상태를 한 번도 내보내지 않았을 때
    # "patrol": null 을 반환한다. 이때도 대시보드는 로봇/지도 화면을
    # 계속 보여 주어야 하므로, dict일 때만 state를 읽는다.
    patrol = (status or {}).get("patrol")
    state = patrol.get("state") if isinstance(patrol, dict) else None
    running, paused = state == "patrolling", state == "paused"
    navigating = state == "navigating"

    st.caption("🚓 순찰 제어")

    # 시작 전에는 어떤 경로로 돌지 먼저 보여줍니다.
    if not running and not navigating:
        plan, plan_error = _fetch_patrol_plan(plan_url)
        if plan_error:
            st.caption(f"경로 미리보기 실패 — {plan_error}")
        elif plan and plan.get("ok"):
            st.caption(f"지금 지도 기준 {len(plan.get('waypoints', []))}개 지점 · "
                       f"한 바퀴 {plan.get('total_distance_m', 0):.1f}m")
            for warning in plan.get("warnings", []):
                st.caption(f"⚠ {warning}")
        elif plan:
            st.caption(f"경로를 만들 수 없습니다 — {plan.get('reason', '')}")

    pending = st.session_state.get("patrol_confirm")
    # 이전 버전에서 정지도 확인 대상이었다. 세션에 "stop"이 남아
    # 있어도 다시 확인 화면을 띄우지 않도록 시작·재개만 인정한다.
    if pending not in ("start", "resume"):
        if pending is not None:
            st.session_state["patrol_confirm"] = None
        pending = None

    stop_clicked = False

    if pending:
        st.warning("실물 로봇이 움직입니다. 주변에 사람이 없는지 확인하세요.")
        col_yes, col_no = st.columns(2)
        with col_yes:
            if st.button("네, 진행합니다", key="patrol_confirm_yes",
                         **full_width_kwargs()):
                ok, message = _post_patrol(patrol_url, pending)
                st.session_state["patrol_confirm"] = None
                (st.warning if ok is None else st.success if ok else st.error)(message)
        with col_no:
            if st.button("취소", key="patrol_confirm_no", **full_width_kwargs()):
                st.session_state["patrol_confirm"] = None

        # 확인 화면이 열려 있어도 정지 경로는 가리지 않는다.
        stop_clicked = st.button(
            "■ 순찰 정지", key="patrol_stop_pending_btn",
            help="확인 없이 즉시 Nav2 목표 취소를 요청합니다.",
            **full_width_kwargs(),
        )
    else:
        columns = st.columns(2)
        with columns[0]:
            if running:
                st.button("● 순찰 중", key="patrol_running_state",
                          disabled=True, **full_width_kwargs())
            elif navigating:
                st.button("● 선택 위치 이동 중", key="navigation_running_state",
                          disabled=True, **full_width_kwargs())
            elif paused:
                st.button("▶ 순찰 재개", key="patrol_resume_btn",
                          on_click=lambda: st.session_state.update(patrol_confirm="resume"),
                          **full_width_kwargs())
            else:
                st.button("▶ 순찰 시작", key="patrol_start_btn",
                          on_click=lambda: st.session_state.update(patrol_confirm="start"),
                          help="지금 지도로 경로를 새로 만들어 순찰을 시작합니다.",
                          **full_width_kwargs())
        with columns[1]:
            # 상태 응답이 끊겨도 정지 버튼은 숨기지 않는다. 이 명령은
            # 여러 번 보내도 동일한 Nav2 목표 취소라서 안전하게 재시도할 수 있다.
            stop_clicked = st.button(
                "■ 순찰 정지", key="patrol_stop_btn",
                help="확인 없이 즉시 Nav2 목표 취소를 요청합니다.",
                **full_width_kwargs(),
            )

    if stop_clicked:
        # 정지는 확인용 session_state를 거치지 않고 클릭한 런에서 바로
        # 전송한다. 이래야 확인 대기 중에도 멈출 수 있다.
        st.session_state["patrol_confirm"] = None
        ok, message = _post_patrol(patrol_url, "stop")
        (st.warning if ok is None else st.success if ok else st.error)(message)

    st.caption("※ 정지는 Nav2 목표 취소이며 하드웨어 비상정지가 아닙니다.")


def render_minimap(daeun_ip: str = None, height: int = None, show_caption: bool = True):
    """SLAM 미니맵만 그립니다. **본문 한 줄을 통째로 주세요** (st.columns 안 금지).

    minimap_web.html 은 폭 900px 아래에서 좁은 화면용 레이아웃으로 접힙니다.
    2단 칸에 넣으면 1920px 모니터에서도 745px밖에 안 되어 항상 접힌 채로 뜹니다.
    자세한 경위는 이 파일 상단 v5 주석 참고.
    """
    m_ip = daeun_ip or DAEUN_LAPTOP_IP
    h = height or MINIMAP_HEIGHT

    if MINIMAP_LEGACY_MJPEG:
        # v3 방식 — 되돌리기용
        st.markdown(
            f'<img src="{_minimap_url(m_ip, MINIMAP_PORT)}" '
            f'style="width:100%; border-radius:10px; display:block;" '
            f'onerror="this.onerror=null; this.src=\'{_placeholder("미니맵 연결 실패")}\';">',
            unsafe_allow_html=True,
        )
    else:
        # iframe 안에서 지도 확대/이동, 로봇 추적, 탐지 항목 클릭이 됩니다.
        minimap_url = _minimap_url(m_ip, MINIMAP_PORT)
        if hasattr(st, "iframe"):
            st.iframe(minimap_url, height=h)
        else:  # Streamlit 1.62 미만 호환
            components.iframe(minimap_url, height=h, scrolling=False)

    if show_caption:
        st.caption(
            f"다은님 노트북 {_minimap_base(m_ip, MINIMAP_PORT)} · "
            "화면이 비어 있으면 그쪽에서 minimap_renderer.py 가 실행 중인지 확인하세요."
        )


def render_video_and_minimap(jetson_ip: str = None, daeun_ip: str = None):
    """v4까지의 호출부 호환용 wrapper (영상 | 미니맵 2단 배치).

    새로 짜는 화면에서는 쓰지 마세요. 이 배치는 미니맵을 900px 미만으로
    좁혀서 좁은 화면용 레이아웃으로 접히게 만듭니다. render_video() 와
    render_minimap() 을 따로 호출해서, 미니맵에는 전체 폭을 주세요.
    """
    st.subheader("📹 실시간 순찰 영상 및 위치 지도")
    col_video, col_minimap = st.columns(2)

    with col_video:
        st.caption("카메라 영상 (박스 표시) — 젯슨")
        render_video(jetson_ip)

    with col_minimap:
        st.caption("미니맵 (SLAM 기반) — 다은님 노트북")
        render_minimap(daeun_ip)
