r"""
self_test/fake_minimap_server.py

이다은님 노트북(ROS2 + SLAM)이나 실제 minimap_renderer.py 없이도
event_logger.py와 대시보드 미니맵이 제대로 동작하는지 확인하기 위한 가짜 서버.

실제 minimap_renderer.py와 같은 엔드포인트를 같은 형식으로 흉내냅니다
(Flask도 필요 없이 표준 라이브러리만 사용).

    GET /            minimap_web.html 서빙 (대시보드 iframe이 부르는 주소)
    GET /map_data    가짜 occupancy grid (digital_twin_1.world 의 랙 배치)
    GET /state       웨이포인트를 도는 로봇 위치 + 탐지 목록
    GET /detections  event_logger.py 가 폴링하는 기존 엔드포인트 (형식 그대로)
    GET /health      상태 확인

사용법 1 — event_logger.py 테스트 (기존과 동일)
-----------------------------------------------
1. 이 스크립트를 켭니다.
       python self_test\fake_minimap_server.py

2. 다른 터미널에서 event_logger.py를 이 서버를 보도록 실행합니다.
       . .\set_env.ps1
       $env:DAEUN_LAPTOP_IP = "127.0.0.1"
       python edge_video\event_logger.py

3. event_logger.py 터미널에 "이벤트 기록: ..." 로그가 찍히면 성공입니다.
4. DB에서 확인:
       SELECT * FROM detection_events ORDER BY id DESC LIMIT 5;

사용법 2 — 대시보드 미니맵 화면 테스트
--------------------------------------
1. 이 스크립트를 켭니다.
2. 대시보드를 127.0.0.1을 보도록 실행합니다.
       $env:DAEUN_LAPTOP_IP = "127.0.0.1"
       streamlit run dashboard\smart_factory_dashboard_v3.py
3. "실시간 순찰 영상 및 위치 지도" 섹션의 미니맵에 지도와 움직이는 로봇이
   나오면 성공입니다. 브라우저에서 http://127.0.0.1:8091/ 로 직접 열어도 됩니다.

주의: 여기서 나오는 지도/로봇/탐지는 전부 가짜입니다.
"""

import base64
import hashlib
import json
import math
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

PORT = 8091

# 실제로 서빙되는 미니맵 페이지는 이다은님 폴더에 하나만 존재합니다.
# 여기로 복사해두면 두 벌이 갈라지므로, 상대경로로 그 파일을 그대로 읽습니다.
_HERE = os.path.dirname(os.path.abspath(__file__))
MINIMAP_HTML = os.path.normpath(os.path.join(
    _HERE, "..", "..",
    "ROS2_자율주행_및_연동(이다은)", "dashboard_link", "minimap_web.html",
))

# 시간이 지날수록 hit_count가 늘어나는 것처럼 흉내내서,
# event_logger.py의 "새로운 tracker_id만 기록" 로직도 같이 테스트할 수 있게 함
_start_time = time.time()

FREE, WALL, UNKNOWN = 0, 1, 2

# ---------------------------------------------------------------------------
# 가짜 지도 — digital_twin_1.world 의 벽/랙 배치를 그대로 옮긴 것
# ---------------------------------------------------------------------------
MAP_RES = 0.05
MAP_OX, MAP_OY = -1.2, -0.8
MAP_W = int(round(12.6 / MAP_RES))   # 252
MAP_H = int(round(9.0 / MAP_RES))    # 180


def _build_map() -> bytearray:
    cells = bytearray([UNKNOWN]) * (MAP_W * MAP_H)

    def put(x, y, v):
        col = int(round((x - MAP_OX) / MAP_RES))
        row = int(round((y - MAP_OY) / MAP_RES))
        if 0 <= col < MAP_W and 0 <= row < MAP_H:
            cells[row * MAP_W + col] = v

    def box(cx, cy, w, h, v):
        steps_x = int(w / MAP_RES) + 1
        steps_y = int(h / MAP_RES) + 1
        for i in range(steps_x):
            for j in range(steps_y):
                put(cx - w / 2 + i * MAP_RES, cy - h / 2 + j * MAP_RES, v)

    box(5, 3.5, 11.8, 7.8, FREE)          # 방 내부
    box(5, 7.5, 12, 0.2, WALL)            # 북쪽 벽
    box(5, -0.5, 12, 0.2, WALL)           # 남쪽 벽
    box(-1, 3.5, 0.2, 8.2, WALL)          # 서쪽 벽
    box(11, 3.5, 0.2, 8.2, WALL)          # 동쪽 벽
    for rx, ry in [(2, 2), (2, 5), (5, 2), (5, 5), (8, 2), (8, 5)]:
        box(rx, ry, 1, 1.5, WALL)         # 랙 6개
    return cells


_MAP_CELLS = _build_map()
_MAP_RAW = bytes(_MAP_CELLS)
_MAP_VERSION = "%s-%dx%d" % (hashlib.sha1(_MAP_RAW).hexdigest()[:16], MAP_W, MAP_H)
_MAP_B64 = base64.b64encode(_MAP_RAW).decode("ascii")

# patrol_node.py 의 실제 웨이포인트
_WAYPOINTS = [(0.5, 3.5), (3.5, 1.0), (3.5, 6.0), (6.5, 6.0), (6.5, 1.0), (9.5, 3.5)]
_SEG_SECONDS = 6.0


def _robot_now():
    """웨이포인트 사이를 일정 속도로 오가는 가짜 로봇 위치."""
    elapsed = time.time() - _start_time
    total = len(_WAYPOINTS) * _SEG_SECONDS
    pos = (elapsed % total) / _SEG_SECONDS
    seg = int(pos)
    t = pos - seg
    ax, ay = _WAYPOINTS[seg]
    bx, by = _WAYPOINTS[(seg + 1) % len(_WAYPOINTS)]
    return {
        "x": ax + (bx - ax) * t,
        "y": ay + (by - ay) * t,
        "yaw": math.atan2(by - ay, bx - ax),
    }


# 실제 minimap_renderer.py 와 같은 방식 — 프로세스가 뜰 때마다 새 값.
SESSION_ID = hashlib.sha1(f"{time.time()}:{os.getpid()}".encode()).hexdigest()[:12]


def _detections_now():
    """실제 /detections 응답과 같은 형식. 랙 표면에 붙은 위치로 잡아둔다.

    라벨은 젯슨이 실제로 보내는 것과 같아야 합니다. 미니맵이 받는 각도 패킷의
    label 은 ai_inference_sender.py 의 text_prompt 에서 그대로 오기 때문입니다.

        text_prompt = ["person with no helmet", "fire", "vehicle"]

    예전에는 여기가 "no safety helmet" / "person" 이라, common/schema.py 의
    classify_risk() 가 둘 다 "미분류 -> 주의"로 떨어뜨렸습니다. 그러면 안전모
    미착용이 화면에서 '주의'로 보여서, 실제 동작(위험)과 다른 인상을 줍니다.
    """
    elapsed = time.time() - _start_time
    now = time.time()
    return [
        {"id": 1, "label": "fire", "x": 7.50, "y": 2.05,
         "hit_count": min(3 + int(elapsed // 5), 20), "last_seen": now - 12},
        {"id": 2, "label": "person with no helmet", "x": 2.52, "y": 5.10,
         "hit_count": 6, "last_seen": now - 41},
        {"id": 3, "label": "vehicle", "x": 5.52, "y": 4.85,
         "hit_count": 4, "last_seen": now - 3},
    ]


class FakeMinimapHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path                     # ?t=토큰 이 붙어도 경로만 본다
        query = dict(
            kv.split("=", 1) for kv in parts.query.split("&") if "=" in kv
        )

        if path == "/":
            self._serve_page()
        elif path == "/map_data":
            self._json(self._map_payload(query.get("v")))
        elif path == "/state":
            self._json({
                "ready": True,
                "stamp": time.time(),
                "robot": _robot_now(),
                "detections": _detections_now(),
            })
        elif path == "/detections":
            # event_logger.py 가 쓰는 형식. session 은 미니맵 프로세스가 뜰 때마다
            # 새로 만드는 값으로, event_logger 가 (session, id) 로 중복을 판단합니다.
            # 이 가짜 서버를 재시작하면 값이 바뀌므로 재시작 동작도 확인됩니다.
            self._json({"session": SESSION_ID, "detections": _detections_now()})
        elif path == "/health":
            self._json({"map_ready": True, "pose_ready": True})
        else:
            self.send_response(404)
            self.end_headers()

    def _map_payload(self, client_version):
        payload = {
            "ready": True,
            "version": _MAP_VERSION,
            "w": MAP_W,
            "h": MAP_H,
            "res": MAP_RES,
            "ox": MAP_OX,
            "oy": MAP_OY,
        }
        if client_version != _MAP_VERSION:
            payload["cells"] = _MAP_B64
        return payload

    def _serve_page(self):
        try:
            with open(MINIMAP_HTML, "rb") as f:
                body = f.read()
        except OSError as exc:
            msg = (
                "minimap_web.html 을 찾지 못했습니다.\n\n"
                f"찾은 경로: {MINIMAP_HTML}\n"
                f"원인: {exc}\n\n"
                "이 저장소를 통째로 받았는지 확인하세요. 이 서버는 이다은님 폴더의\n"
                "minimap_web.html 을 상대경로로 읽습니다(사본을 만들지 않기 위해서)."
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 실제 서버와 동일하게, iframe 임베드를 허용한다
        self.send_header("Content-Security-Policy", "frame-ancestors *")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 매 요청마다 로그 찍으면 시끄러우니 조용히


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), FakeMinimapHandler)
    print(f"가짜 미니맵 서버 시작: http://127.0.0.1:{PORT}/")
    print(f"  미니맵 페이지 : http://127.0.0.1:{PORT}/")
    print(f"  탐지 목록    : http://127.0.0.1:{PORT}/detections")
    print(f"  지도 {MAP_W}x{MAP_H} 셀 ({MAP_W * MAP_RES:.1f}m x {MAP_H * MAP_RES:.1f}m)")
    print("  ※ 지도·로봇·탐지 모두 가짜 데이터입니다.")
    print("Ctrl+C로 종료합니다.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
