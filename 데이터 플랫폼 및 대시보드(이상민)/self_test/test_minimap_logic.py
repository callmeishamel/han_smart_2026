r"""
self_test/test_minimap_logic.py

minimap_renderer.py의 핵심 판단 로직(raycast_to_obstacle, SpatialObjectTracker)이
ROS2/rclpy 없이도 제대로 동작하는지 확인하는 자체 테스트.

- rclpy, flask, nav_msgs, geometry_msgs가 컴퓨터에 설치돼 있지 않아도 됩니다.
  (이 스크립트가 그 라이브러리들을 "가짜(스텁)"로 흉내내서 import만 통과시킵니다.
   실제 ROS2 통신 부분은 테스트하지 않고, 순수 계산 로직만 검증합니다.)

실행 방법 (프로젝트 루트에서):
    python self_test\test_minimap_logic.py
"""

import os
import sys
import types
import math
import json
import threading
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

# ---- rclpy/flask 등을 가짜 모듈로 등록 (설치 안 되어 있어도 import되게) ----
for modname in ["rclpy", "rclpy.node", "rclpy.qos", "nav_msgs", "nav_msgs.msg",
                "sensor_msgs", "sensor_msgs.msg", "std_msgs", "std_msgs.msg",
                "geometry_msgs", "geometry_msgs.msg", "flask",
                "tf2_ros", "tf2_ros.buffer", "tf2_ros.transform_listener"]:
    sys.modules.setdefault(modname, types.ModuleType(modname))

sys.modules["rclpy.node"].Node = object
sys.modules["nav_msgs.msg"].OccupancyGrid = object
sys.modules["sensor_msgs.msg"].LaserScan = object
# 터틀봇 상태창(배터리·속도)을 위해 이 둘도 구독합니다.
sys.modules["sensor_msgs.msg"].BatteryState = object
sys.modules["nav_msgs.msg"].Odometry = object
sys.modules["std_msgs.msg"].String = object
sys.modules["geometry_msgs.msg"].PoseWithCovarianceStamped = object

# minimap_renderer 는 /map 을 TRANSIENT_LOCAL 로 구독합니다(지도는 SLAM 이 한 번만
# 발행하고 마는 latched 토픽이라, 기본 QoS 로는 늦게 뜬 구독자가 영영 못 받습니다).
# 스텁이 이 세 이름을 갖고 있지 않으면 import 단계에서 테스트가 통째로 죽습니다.
sys.modules["rclpy.qos"].QoSProfile = lambda *a, **kw: None
sys.modules["rclpy.qos"].ReliabilityPolicy = types.SimpleNamespace(
    RELIABLE=object(), BEST_EFFORT=object())
sys.modules["rclpy.qos"].DurabilityPolicy = types.SimpleNamespace(
    TRANSIENT_LOCAL=object(), VOLATILE=object())


class _FakeApp:
    """@app.route / @app.before_request / @app.after_request 를 삼키는 최소 스텁."""

    def _decorator(self, *a, **kw):
        # @app.before_request 처럼 괄호 없이 쓰는 데코레이터는
        # 함수가 바로 인자로 들어온다. @app.route('/path') 형식은
        # 기존처럼 데코레이터 함수를 돌려준다.
        if len(a) == 1 and callable(a[0]) and not kw:
            return a[0]
        return lambda f: f

    route = before_request = after_request = errorhandler = _decorator

    def run(self, *a, **kw):
        raise AssertionError("테스트가 실제 서버를 띄우려 했습니다")


sys.modules["flask"].Flask = lambda *a, **kw: _FakeApp()
sys.modules["flask"].Response = object
sys.modules["flask"].jsonify = lambda *a, **kw: (a[0] if len(a) == 1 else kw)
sys.modules["flask"].request = types.SimpleNamespace(args={}, headers={})
sys.modules["flask"].abort = lambda *a, **kw: None
sys.modules["flask"].send_from_directory = lambda *a, **kw: None

# 원본(이다은)은 TF2로 map->base_link 변환을 직접 조회하므로 이것들도 필요합니다.
class _TransformException(Exception):
    pass


sys.modules["tf2_ros"].TransformException = _TransformException
sys.modules["tf2_ros.buffer"].Buffer = object
sys.modules["tf2_ros.transform_listener"].TransformListener = object

# ---- 검증 대상 ----
#
# 실제로 실행되는 minimap_renderer.py 는 이다은님 폴더에 있는 이것 하나뿐입니다.
# 이 파일은 rclpy/flask 를 스텁으로 바꿔치기해서, ROS2 가 깔려 있지 않은
# 이상민님 PC 에서도 순수 계산 로직만 검증합니다.
#
# 예전에는 이상민 폴더에 rclpy 없이 읽기 위한 사본(edge_video/minimap_renderer.py)이
# 하나 더 있었고 이 테스트가 그쪽을 검증했습니다. 그런데 두 파일은 이미 갈라져
# 있어서(원본은 tf2 로 map->base_footprint 를 조회하는데 사본은 /amcl_pose 구독,
# SpatialObjectTracker 도 원본은 list + next_id, 사본은 dict + count) **통과해도
# 실제로 도는 코드를 전혀 보증하지 못했습니다.** 사본은 삭제했습니다.
# (fake_minimap_server.py 가 minimap_web.html 을 복사하지 않고 상대경로로 읽는 것과
#  같은 원칙입니다 — 사본이 갈라지면 검증이 조용히 무의미해집니다.)
_TARGET = os.path.join(
    os.path.dirname(_PROJECT_ROOT),
    "ROS2_자율주행_및_연동(이다은)", "dashboard_link", "minimap_renderer.py",
)

if not os.path.exists(_TARGET):
    print("[중단] 검증 대상을 찾지 못했습니다:")
    print(f"       {_TARGET}")
    print("       이 테스트는 저장소를 통째로 받아야 돌아갑니다"
          " (이다은님 폴더가 함께 있어야 합니다).")
    raise SystemExit(1)

print("검증 대상: ROS2_자율주행_및_연동(이다은)/dashboard_link/minimap_renderer.py\n")

import importlib.util
spec = importlib.util.spec_from_file_location("minimap_renderer", _TARGET)
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)


def check(desc, condition, detail=""):
    """detail 은 실패했을 때만 보여줍니다 — 통과 목록이 지저분해지지 않게."""
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def test_navigation_target_safety():
    """지도 클릭 목표는 알려진·연결된 안전 영역만 허용해야 한다."""
    print("\n=== 지도 클릭 이동 안전 검증 ===")
    info = types.SimpleNamespace(
        width=7,
        height=7,
        resolution=1.0,
        origin=types.SimpleNamespace(
            position=types.SimpleNamespace(x=0.0, y=0.0)),
    )

    def node_for(data, robot=(1.5, 3.5), blind=None):
        node = mr.MinimapNode.__new__(mr.MinimapNode)
        node.lock = threading.Lock()
        node.latest_map = types.SimpleNamespace(info=info, data=data.reshape(-1).tolist())
        node.map_array = data
        node.robot_pose = robot
        node.robot_yaw = 0.0
        node.pose_seen = time.time()
        node.blind_obstacles = list(blind or [])
        return node

    free = mr.np.zeros((7, 7), dtype=mr.np.int16)
    ok, reason = node_for(free).cell_is_clear(2.5, 3.5, 0.0)
    check("같은 자유 공간의 목표는 허용", ok, reason)

    unknown = free.copy()
    unknown[3, 2] = -1
    ok, _ = node_for(unknown).cell_is_clear(2.5, 3.5, 0.0)
    check("주변이 비어 있어도 목표 셀이 미탐사면 거부", not ok)

    ok, _ = node_for(free).cell_is_clear(-0.01, 3.5, 0.0)
    check("원점 바로 바깥의 음수 좌표를 0번 셀로 오인하지 않음", not ok)

    separated = free.copy()
    separated[:, 3] = 100
    ok, _ = node_for(separated).cell_is_clear(5.5, 3.5, 0.0)
    check("벽 너머의 연결되지 않은 자유 공간은 거부", not ok)

    blind = [{"x": 2.5, "y": 3.5, "radius": 0.2}]
    ok, _ = node_for(free, blind=blind).cell_is_clear(2.5, 3.5, 0.1)
    check("기록된 낮은 장애물 위 목표는 거부", not ok)

    stale = node_for(free)
    stale.pose_seen = time.time() - mr.POSE_STALE_SEC - 1.0
    ok, _ = stale.cell_is_clear(2.5, 3.5, 0.0)
    check("TF가 오래되면 마지막 좌표로 이동 명령을 만들지 않음",
          not ok and stale.robot_pose is None)


def test_navigation_ack_contract():
    """HTTP 응답은 제어기 ACK 의미를 성공/대기/실패로 구분한다."""
    print("\n=== 내비게이션 명령 ACK 검증 ===")
    original_node = mr.minimap_node
    released = []

    def fake_node(result, observed=None, pending=True):
        return types.SimpleNamespace(
            wait_for_navigation_status=lambda *_args: result,
            release_navigation_request=lambda request_id: released.append(request_id),
            get_navigation_result=lambda _request_id: (
                observed, 0.1 if observed is not None else None, pending),
        )

    try:
        mr.minimap_node = fake_node({
            "phase": "canceled", "command": "stop", "message": "stopped"})
        body, status = mr._navigation_http_response("stop-1", {"command": "stop"})
        check("활성 목표를 취소한 stop ACK는 실패(409)가 아니라 성공(200)",
              status == 200 and body.get("ok") is True)

        mr.minimap_node = fake_node(None)
        body, status = mr._navigation_http_response("slow-1", {"command": "start"})
        check("제한 시간 내 ACK가 없으면 성공으로 가장하지 않고 202 pending",
              status == 202 and body.get("ok") is False and body.get("pending") is True)

        request_id = mr._command_request_id({"request_id": "   "})
        check("공백 request_id는 제어기와 어긋나지 않게 새 ID로 바꿈",
              bool(request_id.strip()))
        check("202 뒤 request_id 예약을 유지해 상태 조회가 이어짐",
              released == [])
    finally:
        mr.minimap_node = original_node


def test_navigation_status_lookup_contract():
    """202 응답의 request_id로 명령의 최종 상태까지 조회할 수 있어야 한다."""
    print("\n=== request_id별 내비게이션 상태 조회 계약 ===")
    original_node = mr.minimap_node
    original_token = mr.MINIMAP_TOKEN
    original_headers = mr.request.headers

    node = mr.MinimapNode.__new__(mr.MinimapNode)
    node.lock = threading.Lock()
    node.navigation_condition = threading.Condition(node.lock)
    node.navigation_status = None
    node.navigation_seen = 0.0
    node.navigation_results = {}
    node.navigation_result_seen = {}
    node.navigation_pending = {}
    node.navigation_request_sequence = 0
    node.get_logger = lambda: types.SimpleNamespace(warning=lambda *_args: None)

    def publish(request_id, command, goal_type, phase, state, complete):
        payload = {
            "request_id": request_id,
            "command": command,
            "goal_type": goal_type,
            "phase": phase,
            "state": state,
            "message": phase,
            "complete": complete,
        }
        node._on_navigation_status(types.SimpleNamespace(data=json.dumps(payload)))

    def lookup(request_id):
        body, status = mr.navigation_status_endpoint(request_id)
        check("  응답 request_id가 요청과 같음",
              body.get("request_id") == request_id)
        return body, status

    try:
        mr.minimap_node = node
        mr.MINIMAP_TOKEN = "self-test-token"
        mr.request.headers = {"X-Minimap-Token": "self-test-token"}

        check("stop 요청 ID 예약", node.prepare_navigation_request("stop-1"))
        publish("stop-1", "stop", "patrol", "canceling", "patrolling", False)
        body, status = lookup("stop-1")
        check("canceling은 최종 정지로 가장하지 않고 202",
              status == 202 and body.get("pending") is True
              and body.get("complete") is False and body.get("ok") is False)

        publish("stop-1", "stop", "patrol", "canceled", "idle", True)
        body, status = lookup("stop-1")
        check("stop의 최종 canceled는 200/ok",
              status == 200 and body.get("complete") is True
              and body.get("pending") is False and body.get("ok") is True)

        check("goto 요청 ID 예약", node.prepare_navigation_request("goto-1"))
        publish("goto-1", "goto", "goto", "accepted", "navigating", False)
        body, status = lookup("goto-1")
        check("goto accepted는 도착 전이므로 202",
              status == 202 and body.get("pending") is True
              and body.get("complete") is False)

        publish("goto-1", "goto", "goto", "succeeded", "idle", True)
        body, status = lookup("goto-1")
        check("goto succeeded는 200/ok",
              status == 200 and body.get("complete") is True
              and body.get("ok") is True)

        for request_id, phase in (("failed-1", "failed"),
                                  ("rejected-1", "rejected")):
            check(f"{phase} 요청 ID 예약",
                  node.prepare_navigation_request(request_id))
            publish(request_id, "goto", "goto", phase, "idle", True)
            body, status = lookup(request_id)
            check(f"{phase}는 조회가 완료되지만 ok=false",
                  status == 200 and body.get("complete") is True
                  and body.get("pending") is False and body.get("ok") is False)

        body, status = lookup("unknown-1")
        check("알 수 없는 request_id는 404",
              status == 404 and body.get("found") is False
              and body.get("ok") is False)
    finally:
        mr.minimap_node = original_node
        mr.MINIMAP_TOKEN = original_token
        mr.request.headers = original_headers


class FakeInfo:
    resolution = 0.1
    width = 100
    height = 100

    class origin:
        class position:
            x = -5.0
            y = -5.0


class FakeGrid:
    info = FakeInfo()
    data = [0] * (100 * 100)  # 전부 빈 공간
    # x=3.0m 지점(격자 인덱스 80)에 세로로 벽 하나를 세워둠
    for gy in range(100):
        data[gy * 100 + 80] = 100


def main():
    print("=== raycast_to_obstacle() 테스트 ===")
    grid = FakeGrid()

    hit = mr.raycast_to_obstacle(grid, (0.0, 0.0), 0.0, max_range=8.0)
    check("정면(0도) 방향으로 3.0m 지점의 벽을 찾음", hit is not None and abs(hit[0] - 3.0) < 0.15)

    hit_none = mr.raycast_to_obstacle(grid, (0.0, 0.0), 1.5708, max_range=8.0)  # 90도 = 위쪽, 벽 없음
    check("벽이 없는 방향은 None 반환 (사람처럼 열린 공간에 있는 대상)", hit_none is None)

    print("\n=== SpatialObjectTracker 테스트 ===")
    tracker = mr.SpatialObjectTracker()

    # 같은 사람이 살짝 흔들리며 4번 검출됨 -> 병합, count=4
    tracker.update_or_add("person", 1.00, 1.00)
    tracker.update_or_add("person", 1.05, 1.02)
    tracker.update_or_add("person", 0.98, 1.03)
    tracker.update_or_add("person", 1.02, 0.99)
    # 멀리 떨어진 person -> 별개 물체, min_hits(3) 미달
    tracker.update_or_add("person", 5.00, 5.00)

    valid = tracker.get_valid_objects()
    check("min_hits(3) 이상인 물체만 1개 나옴 (흔들린 4개는 병합, 먼 1개는 제외)", len(valid) == 1)
    check("병합된 물체의 누적 횟수는 4", valid[0].count == 4)
    check("EMA로 좌표가 평균 근처(1.0, 1.0)로 부드럽게 수렴", abs(valid[0].x - 1.0) < 0.1)

    print("\n=== 지도 장애물 빨간 점 레이어가 제거되었는가 ===")
    # 지도에서 "벽이 아닌 내부 장애물"을 추려 빨간 점으로 뿌리던 기능은 통째로
    # 제거했습니다. occupancy grid 에는 물체 종류 정보가 없어서 벽과 적재물을
    # 모양으로만 구분해야 했고, 그 판정이 빗나가면 벽 위에 빨간 점이 줄줄이
    # 찍혔습니다. 지금은 관제 화면에 지도 + 로봇 + 탐지 마커만 남깁니다.
    check("extract_mapped_obstacles() 가 없다", not hasattr(mr, "extract_mapped_obstacles"))
    check("벽 모양 판정(looks_like_wall) 도 없다", not hasattr(mr, "looks_like_wall"))
    check("노드에 get_mapped_obstacles() 가 없다",
          not hasattr(mr.MinimapNode, "get_mapped_obstacles"))

    # 다만 /scan 기반 실시간 장애물 계산은 남아 있어야 합니다. 화면 표시용이 아니라
    # 젯슨 탐지 각도를 지도 좌표로 옮길 때(작업자 마커) 쓰기 때문입니다.
    check("실시간 /scan 장애물 계산은 남아 있다",
          hasattr(mr, "extract_live_scan_obstacles"))
    check("노드에 get_live_obstacles() 가 있다",
          hasattr(mr.MinimapNode, "get_live_obstacles"))
    print("\n=== 지도 배열을 매번 새로 만들지 않는가 (핫 경로 비용) ===")
    # rclpy 는 OccupancyGrid.data 를 파이썬 **리스트**로 줍니다. 429x428 이면
    # 원소가 18만 개라 배열 변환만 ~3.6ms 입니다. raycast_to_obstacle 은
    # **탐지 UDP 패킷마다** 불리므로, 매번 변환하면 초당 100ms 가까이 날아갑니다.
    check("grid_to_array 헬퍼가 있다", hasattr(mr, "grid_to_array"))

    arr = mr.grid_to_array(grid)
    check("  (height, width) 배열을 만든다", arr is not None and arr.shape == (100, 100),
          str(None if arr is None else arr.shape))
    check("  벽 위치가 보존된다", int(arr[50, 80]) == 100, str(arr[50, 80]))
    check("  형식이 깨지면 None", mr.grid_to_array(None) is None)

    # 미리 만든 배열을 넘겨도 결과가 같아야 한다 (최적화가 값을 바꾸면 안 됨)
    without = mr.raycast_to_obstacle(grid, (0.0, 0.0), 0.0, max_range=8.0)
    with_arr = mr.raycast_to_obstacle(grid, (0.0, 0.0), 0.0, max_range=8.0, data=arr)
    check("배열을 주든 안 주든 결과가 같다", without == with_arr,
          "%s vs %s" % (without, with_arr))

    check("노드가 지도 배열을 캐시한다",
          "map_array" in open(_TARGET, encoding="utf-8").read())
    check("  탐지 스레드가 캐시를 쓴다",
          "get_snapshot_with_array" in open(_TARGET, encoding="utf-8").read())

    print("\n=== 탐지 UDP 패킷 파싱 — 한 건에 스레드가 죽지 않는가 ===")
    # 예전에는 파싱 예외를 (KeyError, ValueError, JSONDecodeError) 로 좁게 잡아서,
    # dict 가 아닌 JSON 하나에 수신 스레드가 통째로 죽었습니다. 지도와 로봇 위치는
    # 다른 스레드가 계속 그리므로 **화면은 멀쩡한데 탐지 마커만 영영 안 생깁니다** —
    # 08-28 에 겪은 "작업자 마커가 안 찍힌다" 와 증상이 똑같아 구분이 어렵습니다.
    check("정상 패킷을 읽는다",
          mr.parse_detection_packet(b'{"label": "person", "angle_offset": 0.12}')
          == ("person", 0.12))
    check("  각도가 문자열이어도 숫자면 받는다",
          mr.parse_detection_packet(b'{"label": "fire", "angle_offset": "-0.5"}')
          == ("fire", -0.5))

    for name, raw in (
        ("dict 가 아닌 JSON (정수)",      b'5'),
        ("dict 가 아닌 JSON (배열)",      b'[1, 2]'),
        ("angle_offset 이 null",          b'{"label": "person", "angle_offset": null}'),
        ("label 이 없음",                 b'{"angle_offset": 0.1}'),
        ("label 이 빈 문자열",            b'{"label": "", "angle_offset": 0.1}'),
        ("각도가 NaN",                    b'{"label": "person", "angle_offset": NaN}'),
        ("JSON 이 아님",                  b'hello'),
        ("UTF-8 이 아님",                 bytes([0xFF, 0xFE])),
        ("빈 패킷",                       b""),
    ):
        check("  " + name + " -> None (죽지 않고 버림)",
              mr.parse_detection_packet(raw) is None)

    src = open(_TARGET, encoding="utf-8").read()
    check("수신 루프가 recvfrom 의 OSError 도 견딘다",
          "except OSError" in src)
    check("  버린 패킷 수를 로그로 알린다", "폐기" in src)

    print("\n=== 카메라 화면 각도 → ROS map 각도 변환 ===")
    # 영상 x는 오른쪽이 +, ROS yaw는 왼쪽(반시계)이 +다.
    check("정면 카메라의 화면 오른쪽(+0.2)은 ROS 시계방향(-0.2)",
          abs(mr.camera_bearing_to_map(0.0, 0.2) - (-0.2)) < 1e-9)
    check("정면 카메라의 화면 왼쪽(-0.2)은 ROS 반시계(+0.2)",
          abs(mr.camera_bearing_to_map(0.0, -0.2) - 0.2) < 1e-9)
    check("로봇이 90도 보는 중에 카메라 중앙은 90도",
          abs(mr.camera_bearing_to_map(math.pi / 2, 0.0) - math.pi / 2) < 1e-9)
    check("180도 반대 장착 보정은 정면을 로봇 뒤쪽으로 바꿈",
          abs(mr.camera_bearing_to_map(0.0, 0.0, math.pi) - math.pi) < 1e-9)

    test_navigation_target_safety()
    test_navigation_ack_contract()
    test_navigation_status_lookup_contract()

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
