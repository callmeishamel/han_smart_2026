r"""
self_test/test_patrol_planner.py

이다은님 폴더 `smart_factory_sim/patrol_planner.py` — SLAM 지도에서 순찰 경로를
만드는 로직을 ROS2 없이 검증합니다 (numpy + OpenCV 만 씁니다).

왜 필요한가
-----------
`patrol_node.py` 의 기본 웨이포인트는 `digital_twin_1.world`(시뮬레이션) 좌표라,
실물 지도에 쓰면 지도 밖이거나 벽 속입니다. 그러면 Nav2 가 모든 목표를 ABORTED 로
돌려주는데 patrol_node 는 재시도 후 다음 지점으로 넘어가므로 **로그만 보면 순찰이
도는 것처럼 보입니다.** 그래서 지도에서 직접 경로를 만들도록 했고, 그 계산이
맞는지 여기서 봅니다.

실제 주행은 로봇/Nav2 가 있어야 하므로 검증하지 않습니다. 여기서 보는 것은
"어디에 설 것인가"와 "어떤 순서로 돌 것인가" 입니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_patrol_planner.py
"""

import math
import os
import re
import sys

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_TARGET = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                       "smart_factory_sim", "patrol_planner.py")

RES = 0.05          # 실제 지도와 같은 해상도


def check(desc, condition, detail=""):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def load_planner():
    if not os.path.exists(_TARGET):
        print(f"[중단] 검증 대상을 찾지 못했습니다:\n       {_TARGET}")
        print("       이 테스트는 저장소를 통째로 받아야 돌아갑니다.")
        raise SystemExit(1)
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("[건너뜀] opencv-python 이 없어 이 테스트는 돌릴 수 없습니다.")
        raise SystemExit(0)

    import importlib.util
    spec = importlib.util.spec_from_file_location("patrol_planner_test", _TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# 지도 만들기 — 미터 단위로 그리고 격자로 옮긴다
# ----------------------------------------------------------------------
class MapBuilder:
    """빈 방 하나를 만들고 벽을 그려 넣는다. 바깥은 미탐사(-1)."""

    def __init__(self, width_m, height_m, res=RES, origin=(0.0, 0.0)):
        self.res = res
        self.origin = origin
        self.cols = int(width_m / res)
        self.rows = int(height_m / res)
        # 전부 미탐사에서 시작해서, 방 안쪽만 자유공간으로 판다.
        self.cells = np.full((self.rows, self.cols), -1, dtype=np.int16)
        self.fill(0, 0, width_m, height_m, 0)
        # 방 테두리는 벽
        self.wall(0, 0, width_m, 0.2)
        self.wall(0, height_m - 0.2, width_m, 0.2)
        self.wall(0, 0, 0.2, height_m)
        self.wall(width_m - 0.2, 0, 0.2, height_m)

    def _box(self, x, y, w, h):
        c0 = max(0, int((x - self.origin[0]) / self.res))
        r0 = max(0, int((y - self.origin[1]) / self.res))
        c1 = min(self.cols, int((x + w - self.origin[0]) / self.res))
        r1 = min(self.rows, int((y + h - self.origin[1]) / self.res))
        return r0, r1, c0, c1

    def fill(self, x, y, w, h, value):
        r0, r1, c0, c1 = self._box(x, y, w, h)
        self.cells[r0:r1, c0:c1] = value
        return self

    def wall(self, x, y, w, h):
        return self.fill(x, y, w, h, 100)

    def grid(self, pp):
        return pp.GridInfo(self.cells, self.res, self.origin[0], self.origin[1])


def open_room(pp, width_m=12.0, height_m=8.0):
    return MapBuilder(width_m, height_m).grid(pp)


def comb_room(pp):
    """빗(comb) 모양 — 아래쪽만 트인 막다른 통로 3개.

    직선거리와 실제 이동거리가 크게 갈리는 구조입니다. 옆 통로의 점은 직선으로
    4m 지만, 실제로는 아래로 내려갔다 올라와야 해서 14m 입니다.
    """
    builder = MapBuilder(12.0, 8.0)
    builder.wall(4.0, 2.0, 0.3, 6.0)
    builder.wall(8.0, 2.0, 0.3, 6.0)
    return builder.grid(pp)


def two_rooms(pp):
    """가운데 벽이 끝까지 막힌 방 두 개 (문이 없다)."""
    builder = MapBuilder(12.0, 8.0)
    builder.wall(6.0, 0.0, 0.4, 8.0)
    return builder.grid(pp)


# ----------------------------------------------------------------------
def test_grid_basics(pp):
    print("=== 격자 좌표 ===")
    grid = open_room(pp)
    row, col = grid.cell_of(3.0, 2.0)
    x, y = grid.cell_center(row, col)
    check("좌표 -> 칸 -> 좌표 왕복이 반 칸 안", abs(x - 3.0) < RES and abs(y - 2.0) < RES,
          f"({x:.3f}, {y:.3f})")

    free = pp.free_mask(grid)
    check("방 안은 자유공간", free[grid.cell_of(6.0, 4.0)] > 0)
    check("벽은 자유공간이 아님", free[grid.cell_of(0.05, 4.0)] == 0)

    clearance = pp.clearance_map(grid, free)
    middle = clearance[grid.cell_of(6.0, 4.0)]
    edge = clearance[grid.cell_of(0.4, 4.0)]
    check("방 한가운데가 벽 근처보다 여유가 크다", middle > edge + 1.0,
          f"중앙 {middle:.2f}m / 가장자리 {edge:.2f}m")
    print()


def test_unknown_is_obstacle(pp):
    """미탐사 옆에 순찰 지점을 두면, 나중에 벽으로 밝혀졌을 때 목표가 벽 속이 된다."""
    print("=== 미탐사를 장애물로 보는가 ===")
    builder = MapBuilder(12.0, 8.0)
    builder.fill(2.0, 2.0, 3.0, 3.0, -1)          # 방 안에 미탐사 구멍
    grid = builder.grid(pp)

    free = pp.free_mask(grid)
    check("미탐사 칸은 자유공간이 아니다", free[grid.cell_of(3.5, 3.5)] == 0)

    clearance = pp.clearance_map(grid, free)
    near_hole = clearance[grid.cell_of(5.2, 3.5)]
    check("미탐사 바로 옆은 여유가 작다", near_hole < 0.5, f"{near_hole:.2f}m")
    print()


def test_rejects_rectangle_from_wrong_free_threshold(pp):
    """미탐사 회색이 0으로 바뀐 지도에서 캔버스 전체를 순찰하면 안 된다."""
    print("=== 미탐사 오인 사각형 지도 차단 ===")
    cells = np.zeros((120, 160), dtype=np.int16)
    # 실제 벽은 안쪽에 남아 있지만, 잘못된 free_thresh 때문에 벽 바깥 여백도 0.
    cells[20:100, 25] = 100
    cells[20:100, 135] = 100
    cells[20, 25:136] = 100
    cells[99, 25:136] = 100
    grid = pp.GridInfo(cells, RES, 0.0, 0.0)

    check("외곽이 거의 전부 자유 셀이면 임계값 오류로 감지",
          pp.has_suspicious_free_border(grid))
    plan = pp.plan_patrol_route(grid, zone_count=3)
    check("사각형 전체에 순찰 노드를 만들지 않고 거부", not plan.ok)
    check("수정할 free_thresh 값을 안내",
          "free_thresh" in plan.reason and "0.196" in plan.reason, plan.reason)

    valid = cells.copy()
    valid[:20, :] = -1
    valid[100:, :] = -1
    valid[:, :25] = -1
    valid[:, 136:] = -1
    check("정상적인 미탐사 외곽은 오탐하지 않음",
          not pp.has_suspicious_free_border(pp.GridInfo(valid, RES, 0.0, 0.0)))
    print()


def test_reachability(pp):
    print("=== 갈 수 없는 방을 빼는가 ===")
    grid = two_rooms(pp)
    free = pp.free_mask(grid)
    safe = pp.clearance_map(grid, free) >= 0.25

    left = pp.reachable_mask(safe, grid.cell_of(2.0, 4.0))
    check("왼쪽 방에서 출발하면 왼쪽만 남는다", left[grid.cell_of(2.0, 4.0)])
    check("  오른쪽 방은 제외된다", not left[grid.cell_of(10.0, 4.0)])

    right = pp.reachable_mask(safe, grid.cell_of(10.0, 4.0))
    check("오른쪽에서 출발하면 반대로 된다",
          right[grid.cell_of(10.0, 4.0)] and not right[grid.cell_of(2.0, 4.0)])

    plan = pp.plan_patrol_route(grid, robot_xy=(2.0, 4.0), zone_count=2)
    check("계획이 성공한다", plan.ok, plan.reason)
    check("모든 지점이 로봇이 있는 방 안에 있다",
          all(w.x < 6.0 for w in plan.waypoints),
          str([(round(w.x, 1), round(w.y, 1)) for w in plan.waypoints]))
    check("갈 수 없는 영역을 뺐다고 알려준다",
          any("이어지지 않는" in w for w in plan.warnings), str(plan.warnings))
    print()


def test_waypoint_quality(pp):
    print("=== 지점이 설 만한 자리인가 ===")
    grid = open_room(pp)
    plan = pp.plan_patrol_route(grid, zone_count=3, min_clearance_m=0.25)
    check("계획 성공", plan.ok, plan.reason)
    check("지점이 2개 이상", len(plan.waypoints) >= 2, str(len(plan.waypoints)))

    free = pp.free_mask(grid)
    clearance = pp.clearance_map(grid, free)
    for wp in plan.waypoints:
        row, col = grid.cell_of(wp.x, wp.y)
        check(f"  ({wp.x:.1f}, {wp.y:.1f}) 는 벽에서 0.25m 이상",
              clearance[row, col] >= 0.25, f"{clearance[row, col]:.2f}m")

    zones_hit = {w.zone for w in plan.waypoints}
    zone_names = {z["name"] for z in plan.zones}
    check("모든 구역에 지점이 하나 이상", zones_hit == zone_names,
          f"{sorted(zones_hit)} vs {sorted(zone_names)}")
    print()


def test_spacing(pp):
    print("=== 붙어 있는 지점을 합치는가 ===")
    make = pp.Waypoint
    # 구역 A 안에 거의 같은 자리 두 개 + 멀리 떨어진 B
    crowd = [make(1.0, 1.0, 0.0, "A", 0.9), make(1.1, 1.05, 0.0, "A", 0.5),
             make(8.0, 6.0, 0.0, "B", 0.8)]
    kept, merged = pp.enforce_spacing(crowd, 1.5)
    check("겹친 지점을 하나로 줄인다", merged == 1 and len(kept) == 2, f"merged={merged}")
    check("  여유가 큰 쪽을 남긴다",
          any(abs(w.clearance_m - 0.9) < 1e-6 for w in kept))

    # 구역이 통째로 사라지면 안 된다 — 간격보다 구역 커버리지가 우선
    tight = [make(1.0, 1.0, 0.0, "A", 0.9), make(1.2, 1.0, 0.0, "B", 0.5)]
    kept, _ = pp.enforce_spacing(tight, 1.5)
    check("간격을 어겨서라도 모든 구역은 들른다",
          {w.zone for w in kept} == {"A", "B"}, str([w.zone for w in kept]))
    print()


def test_geodesic_distance(pp):
    """직선거리로 순서를 정하면 **벽을 뚫고 가는 순서**가 나온다.

    이 테스트가 이 모듈의 핵심 주장입니다.
    """
    print("=== 거리를 지도 위로 재는가 (직선거리가 아니라) ===")
    grid = comb_room(pp)
    free = pp.free_mask(grid)
    usable = pp.reachable_mask(pp.clearance_map(grid, free) >= 0.25,
                               grid.cell_of(6.0, 1.0))

    make = pp.Waypoint
    # 옆 통로의 두 지점. 직선으로는 4m 지만 아래로 돌아가야 한다.
    aisle_left = make(2.0, 6.5, 0.0, "A", 1.0)
    aisle_mid = make(6.0, 6.5, 0.0, "B", 1.0)
    bottom = make(10.5, 1.0, 0.0, "C", 1.0)
    points = [aisle_left, aisle_mid, bottom]

    matrix, warnings = pp.geodesic_distance_matrix(
        grid, usable, points, pp.DEFAULT_COARSE_CELL_M)
    check("모든 구간의 실제 경로를 찾았다", not warnings, str(warnings))

    straight = math.hypot(aisle_left.x - aisle_mid.x, aisle_left.y - aisle_mid.y)
    real = matrix[0, 1]
    check("막힌 옆 통로는 직선거리보다 훨씬 멀다", real > straight * 2.5,
          f"직선 {straight:.1f}m / 실제 {real:.1f}m")
    print(f"       (옆 통로: 직선 {straight:.1f}m 인데 실제로는 {real:.1f}m 를 돌아가야 합니다)")

    check("거리 행렬은 대칭", np.allclose(matrix, matrix.T))
    check("자기 자신까지는 0", np.allclose(np.diag(matrix), 0.0))
    print()


def test_ordering(pp):
    print("=== 순회 순서 ===")
    # 사각형 네 꼭짓점. 대각선으로 가로지르는 순서는 손해다.
    matrix = np.array([
        [0.0, 1.0, 1.4, 1.0],
        [1.0, 0.0, 1.0, 1.4],
        [1.4, 1.0, 0.0, 1.0],
        [1.0, 1.4, 1.0, 0.0],
    ])
    order = pp.order_waypoints(matrix, 0)
    check("모든 지점을 한 번씩 방문", sorted(order) == [0, 1, 2, 3], str(order))
    length = sum(matrix[order[i], order[(i + 1) % 4]] for i in range(4))
    check("둘레(4.0)를 찾는다 — 대각선으로 가로지르지 않는다",
          abs(length - 4.0) < 1e-6, f"{length:.2f}")

    check("지점이 하나면 그대로", pp.order_waypoints(np.zeros((1, 1)), 0) == [0])
    print()


def test_geodesic_beats_straight(pp):
    """측지거리 순서가 직선거리 순서보다 실제 이동거리가 짧은가.

    단순한 지도에서는 두 순서가 같아질 수 있습니다(대칭이라 어느 쪽으로 돌아도
    같은 거리). 그래서 "더 낫거나 같다" 를 봅니다 — 나빠지면 회귀입니다.
    """
    print("=== 측지거리 순서가 손해를 보지 않는가 ===")
    grid = comb_room(pp)
    plan = pp.plan_patrol_route(grid, robot_xy=(10.5, 1.0), zone_count=3)
    check("빗 모양 지도에서도 계획 성공", plan.ok, plan.reason)

    free = pp.free_mask(grid)
    usable = pp.reachable_mask(pp.clearance_map(grid, free) >= 0.25,
                               grid.cell_of(10.5, 1.0))
    real, _ = pp.geodesic_distance_matrix(grid, usable, plan.waypoints,
                                          pp.DEFAULT_COARSE_CELL_M)

    size = len(plan.waypoints)
    straight = np.zeros((size, size))
    for i in range(size):
        for j in range(size):
            straight[i, j] = math.hypot(plan.waypoints[i].x - plan.waypoints[j].x,
                                        plan.waypoints[i].y - plan.waypoints[j].y)

    def tour(order):
        return sum(real[order[i], order[(i + 1) % size]] for i in range(size))

    naive = tour(pp.order_waypoints(straight, 0))
    smart = tour(list(range(size)))       # plan 은 이미 측지거리 순서로 정렬돼 있다
    check("측지거리 순서가 직선거리 순서보다 짧거나 같다", smart <= naive + 1e-6,
          f"측지 {smart:.1f}m / 직선 {naive:.1f}m")
    print(f"       (실제 이동거리: 측지거리 순서 {smart:.1f}m / 직선거리 순서 {naive:.1f}m)")
    print()


def test_yaw(pp):
    print("=== 다음 지점을 바라보는가 ===")
    grid = open_room(pp)
    plan = pp.plan_patrol_route(grid, zone_count=3)
    wps = plan.waypoints
    for i, wp in enumerate(wps):
        nxt = wps[(i + 1) % len(wps)]
        expected = math.atan2(nxt.y - wp.y, nxt.x - wp.x)
        diff = abs(math.atan2(math.sin(wp.yaw - expected), math.cos(wp.yaw - expected)))
        check(f"  지점 {i + 1} 의 yaw 가 다음 지점을 향한다", diff < 1e-6,
              f"{math.degrees(diff):.2f}도 차이")
    print()


def test_output_format(pp):
    print("=== patrol_node 가 받을 수 있는 형식인가 ===")
    grid = open_room(pp)
    plan = pp.plan_patrol_route(grid, zone_count=3)

    flat = plan.flat_waypoints()
    check("x, y, yaw 3개씩 묶인다", len(flat) % 3 == 0 and len(flat) > 0, str(len(flat)))
    check("모두 실수", all(isinstance(v, float) for v in flat))

    payload = plan.as_dict()
    for key in ("ok", "waypoints", "zones", "total_distance_m", "warnings"):
        check(f"  응답에 {key} 가 있다", key in payload)
    first = payload["waypoints"][0]
    for key in ("x", "y", "yaw", "zone"):
        check(f"  지점에 {key} 가 있다", key in first)
    check("한 바퀴 거리가 0보다 크다", payload["total_distance_m"] > 0,
          str(payload["total_distance_m"]))
    print()


def test_failure_modes(pp):
    """계획을 못 짜는 건 흔한 상황입니다. 예외로 죽으면 관제 화면이 함께 죽습니다."""
    print("=== 못 만들 때 조용히 실패하는가 ===")
    check("지도가 없으면 ok=False", pp.plan_patrol_route(None).ok is False)

    solid = MapBuilder(4.0, 4.0)
    solid.wall(0.0, 0.0, 4.0, 4.0)                # 전부 벽
    plan = pp.plan_patrol_route(solid.grid(pp))
    check("자유공간이 없으면 ok=False", plan.ok is False)
    check("  이유를 말해준다", bool(plan.reason), plan.reason)

    narrow = MapBuilder(1.0, 1.0)                  # 테두리 벽만 남아 통로가 없음
    plan = pp.plan_patrol_route(narrow.grid(pp), min_clearance_m=0.5)
    check("여유 공간이 없으면 ok=False", plan.ok is False)
    check("  예외가 아니라 값으로 돌려준다", isinstance(plan, pp.PatrolPlan))
    print()


def test_determinism(pp):
    """같은 지도면 같은 경로가 나와야 합니다. 매번 달라지면 관제하는 사람이
    "왜 오늘은 다르게 도나" 를 계속 묻게 됩니다."""
    print("=== 같은 지도면 같은 경로인가 ===")
    grid = open_room(pp)
    first = pp.plan_patrol_route(grid, zone_count=3)
    second = pp.plan_patrol_route(grid, zone_count=3)
    check("지점 수가 같다", len(first.waypoints) == len(second.waypoints))
    check("좌표가 같다",
          all(abs(a.x - b.x) < 1e-9 and abs(a.y - b.y) < 1e-9
              for a, b in zip(first.waypoints, second.waypoints)))
    check("거리도 같다", abs(first.total_distance_m - second.total_distance_m) < 1e-9)
    print()


def load_patrol_node():
    """patrol_node.py 를 rclpy 스텁으로 로드한다 (계약 검증용)."""
    import types
    for name in ["rclpy", "rclpy.node", "rclpy.action", "rclpy.qos",
                 "action_msgs", "action_msgs.msg",
                 "geometry_msgs", "geometry_msgs.msg",
                 "nav2_msgs", "nav2_msgs.action", "std_msgs", "std_msgs.msg"]:
        sys.modules.setdefault(name, types.ModuleType(name))

    sys.modules["rclpy.node"].Node = object
    sys.modules["rclpy.action"].ActionClient = object
    sys.modules["rclpy.qos"].QoSProfile = lambda *a, **kw: None
    sys.modules["rclpy.qos"].ReliabilityPolicy = types.SimpleNamespace(RELIABLE=object())
    sys.modules["rclpy.qos"].DurabilityPolicy = types.SimpleNamespace(TRANSIENT_LOCAL=object())
    sys.modules["action_msgs.msg"].GoalStatus = types.SimpleNamespace(
        STATUS_SUCCEEDED=4, STATUS_ABORTED=6, STATUS_CANCELED=5)
    sys.modules["geometry_msgs.msg"].PoseStamped = object
    sys.modules["nav2_msgs.action"].NavigateToPose = object
    sys.modules["std_msgs.msg"].String = type("String", (), {"data": ""})

    target = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                          "smart_factory_sim", "patrol_node.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("patrol_node_test", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_planner_to_node(pp):
    """미니맵이 만든 payload 를 patrol_node 가 실제로 해석하는가.

    두 파일은 JSON 문자열 하나로만 이어져 있습니다. 한쪽이 키 이름을 바꾸면
    **에러 없이 조용히** 순찰이 안 돕니다 — 그래서 계약을 직접 통과시켜 봅니다.
    """
    print("=== 플래너 -> patrol_node JSON 계약 ===")
    import json
    import types

    node_module = load_patrol_node()

    grid = open_room(pp)
    plan = pp.plan_patrol_route(grid, zone_count=3)
    check("계획 성공", plan.ok, plan.reason)

    # minimap_renderer.send_patrol_command('start', plan) 이 만드는 것과 같은 형태
    payload = {"command": "start", "reason": "minimap", "source": "dashboard",
               "loop": True,
               "waypoints": [w.as_dict() for w in plan.waypoints]}
    wire = json.loads(json.dumps(payload))      # 실제로 직렬화를 거친다

    # _apply_route 만 떼어서 부른다 (ROS2 퍼블리셔 없이)
    node = node_module.PatrolNode.__new__(node_module.PatrolNode)
    node.waypoints, node.loop, node.index = [], False, 7
    node.retries, node.consecutive_failures, node.paused = 3, 2, True
    node.route_source, node.current_goal = "none", None
    node.get_logger = lambda: types.SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    node._publish_route = lambda: None

    check("patrol_node 가 경로를 받아들인다", node._apply_route(wire) is True)
    check("  지점 수가 같다", len(node.waypoints) == len(plan.waypoints),
          f"{len(node.waypoints)} vs {len(plan.waypoints)}")
    check("  좌표가 그대로 전달된다",
          all(abs(a[0] - b.x) < 1e-3 and abs(a[1] - b.y) < 1e-3
              for a, b in zip(node.waypoints, plan.waypoints)))
    check("  yaw 도 전달된다",
          all(abs(a[2] - b.yaw) < 1e-3 for a, b in zip(node.waypoints, plan.waypoints)))
    check("  받으면 일시정지가 풀린다", node.paused is False)
    check("  첫 지점부터 다시 시작한다", node.index == 0 and node.retries == 0)
    check("  출처를 기록한다", node.route_source == "dashboard", node.route_source)

    check("빈 waypoints 는 거부한다", node._apply_route({"waypoints": []}) is False)
    check("형식이 깨진 지점도 거부한다",
          node._apply_route({"waypoints": [{"x": 1.0}]}) is False)
    print()


def test_navigation_generation_races():
    """정지/재시도 뒤 도착한 과거 Action 콜백은 새 상태를 건드리지 않는다."""
    print("=== patrol_node 비동기 목표 세대 보호 ===")
    import types

    node_module = load_patrol_node()
    node = node_module.PatrolNode.__new__(node_module.PatrolNode)
    node.shutting_down = False
    node.generation = 2
    node.mode = "idle"
    node.index = 0
    node._active_goal_key = None

    class AcceptedHandle:
        accepted = True

        def __init__(self):
            self.cancel_count = 0

        def cancel_goal_async(self):
            self.cancel_count += 1

    stale_handle = AcceptedHandle()
    stale_future = types.SimpleNamespace(result=lambda: stale_handle)
    node.on_goal_response(
        stale_future,
        generation=1,
        index=0,
        goal_type="patrol",
        request_id="old-request",
        command="start",
        goal={"x": 1.0, "y": 1.0, "yaw": 0.0},
        goal_serial=1,
    )
    check("stop 뒤 늦게 승인된 과거 목표를 즉시 취소", stale_handle.cancel_count == 1)

    # 같은 waypoint를 재시도해도 serial이 다르므로 이전 시도의 feedback은
    # 새 목표의 accepted 상태나 남은 거리를 덮어쓸 수 없다.
    node.generation = 3
    node.mode = "patrolling"
    node.index = 0
    node._active_goal_key = (3, 0, "patrol", 2)
    node.goal_serial = 2
    node.active_request_id = "current-request"
    node.active_command = "start"
    node.active_goal = {"x": 2.0, "y": 2.0, "yaw": 0.0}
    published = []
    node._publish_navigation = lambda **kwargs: published.append(kwargs)
    feedback = types.SimpleNamespace(
        feedback=types.SimpleNamespace(distance_remaining=1.25))

    node.on_feedback(
        feedback, 3, 0, "patrol", "old-request", "start",
        {"x": 1.0, "y": 1.0, "yaw": 0.0}, 1)
    check("같은 지점의 이전 retry feedback을 무시", published == [])

    node.on_feedback(
        feedback, 3, 0, "patrol", "current-request", "start",
        node.active_goal, 2)
    check("현재 목표 feedback만 반영", len(published) == 1
          and published[0].get("distance_remaining") == 1.25)

    node._active_goal_key = None
    node.on_feedback(
        feedback, 3, 0, "patrol", "current-request", "start",
        node.active_goal, 2)
    check("목표 완료 뒤 늦은 feedback이 완료 상태를 되돌리지 않음", len(published) == 1)
    print()


def test_critical_detection_hold():
    """화재/안전모 미착용만 순찰을 3초 중지시키고 중복 프레임은 무시한다."""
    print("=== patrol_node 위험 탐지 3초 정지 ===")
    import json
    import types

    node_module = load_patrol_node()
    parse = node_module.critical_labels_from_status

    critical = json.dumps({"detections": [
        {"object": "fire"},
        {"object": "person with no helmet"},
        {"object": "vehicle"},
    ]})
    check("화재와 안전모 미착용만 정지 대상",
          parse(critical) == {"fire", "person with no helmet"})
    check("일반 사람/차량은 정지 대상이 아님",
          not parse(json.dumps({"detections": [
              {"object": "person"}, {"object": "vehicle"}]})))
    check("깨진 JSON은 안전하게 무시", not parse("{broken"))

    node = node_module.PatrolNode.__new__(node_module.PatrolNode)
    node.mode = "patrolling"
    node._pending_transition = None
    node.critical_hold_seconds = 3.0
    node.critical_rearm_clear_seconds = 3.0
    node._critical_hold_latched = False
    node._critical_last_seen = None
    started = []
    node._begin_critical_hold = lambda labels: started.append(set(labels))
    node.get_logger = lambda: types.SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)

    msg = node_module.String()
    msg.data = json.dumps({"detections": [{"object": "fire"}]})
    node._on_safety_status(msg)
    node._on_safety_status(msg)
    check("같은 탐지가 매 프레임 중지를 재시작하지 않음",
          started == [{"fire"}])

    node._critical_last_seen = (
        node_module.time.monotonic() - node.critical_rearm_clear_seconds - 0.1
    )
    clear_msg = node_module.String()
    clear_msg.data = json.dumps({"detections": []})
    node._on_safety_status(clear_msg)
    node._on_safety_status(msg)
    check("화면에서 3초 사라진 뒤에는 다음 탐지를 다시 처리",
          started == [{"fire"}, {"fire"}])

    # 실제 hold continuation이 3초 타이머를 걸고, 끝나면 resume
    # 명령을 기존 제어기에 넣는지 확인한다.
    hold = node_module.PatrolNode.__new__(node_module.PatrolNode)
    hold.active_goal_type = "patrol"
    hold.active_goal = {"x": 1.0, "y": 2.0, "yaw": 0.0}
    hold.index = 2
    hold.critical_hold_seconds = 3.0
    hold._paused_goal_type = None
    hold._paused_goal = None
    hold._critical_hold_request_id = None
    hold.mode = "patrolling"
    hold.paused = False
    hold.get_logger = node.get_logger
    published = []
    scheduled = []
    transition = {}
    hold._publish_navigation = lambda **kwargs: published.append(kwargs)
    hold.schedule = lambda seconds, callback, **kwargs: scheduled.append(
        (seconds, callback, kwargs))
    hold._begin_transition = lambda command, payload, **kwargs: transition.update(
        command=command, payload=payload, **kwargs)

    hold._begin_critical_hold({"person with no helmet"})
    check("임의 /cmd_vel이 아니라 기존 pause 전환을 사용",
          transition.get("command") == "pause")
    request_id = transition["payload"]["request_id"]
    transition["continuation"](
        7,
        request_id,
        {"mode": "patrolling", "goal_type": "patrol",
         "goal": dict(hold.active_goal)},
        True,
        "canceled",
    )
    check("취소 확인 후 paused 상태", hold.mode == "paused")
    check("정지 타이머는 정확히 3초", scheduled[0][0] == 3.0)

    resumed = []
    hold.active_request_id = request_id
    hold._on_control = lambda resume_msg: resumed.append(json.loads(resume_msg.data))
    scheduled[0][1]()
    check("3초 후 기존 순찰 resume 명령 전달",
          resumed and resumed[0].get("command") == "resume")
    print()


def test_wiring():
    """모듈이 맞아도 배선이 빠지면 아무것도 달라지지 않는데,
    위 테스트는 전부 통과합니다. 그래서 소스에서 직접 확인합니다."""
    print("=== patrol_node / minimap 배선 ===")
    base = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)")

    with open(os.path.join(base, "smart_factory_sim", "patrol_node.py"),
              encoding="utf-8") as f:
        node = f.read()
    check("patrol_node 가 start 명령을 받는다", "command == 'start'" in node)
    check("  stop 명령도 받는다", "command == 'stop'" in node)
    check("  경로를 통째로 갈아끼운다", "_apply_route" in node)
    check("  새 경로 전에 진행 중 목표를 취소한다", "_cancel_current_goal" in node)
    check("  경로가 없으면 idle 로 알린다", '"idle"' in node)
    check("  경로 없이도 죽지 않는다", "if not self.waypoints:" in node)
    check("  /safety_status 화재·안전모 탐지를 직접 받는다",
          "'/safety_status'" in node and "_on_safety_status" in node)
    check("  위험 탐지 정지 기본값은 3초",
          "declare_parameter('critical_hold_seconds', 3.0)" in node)

    with open(os.path.join(base, "dashboard_link", "minimap_renderer.py"),
              encoding="utf-8") as f:
        minimap = f.read()
    check("미니맵이 플래너를 쓴다", "patrol_planner" in minimap)
    check("  /patrol_plan 미리보기가 있다", "/patrol_plan" in minimap)
    check("  start 로 경로를 실어 보낸다",
          re.search(r"send_patrol_command\(\s*['\"]start['\"]\s*,\s*plan", minimap)
          is not None)
    check("  구역은 미니맵이 나눈 것을 그대로 넘긴다",
          "zone_raster=raster" in minimap)
    check("  제어는 토큰 없이 열리지 않는다",
          "MINIMAP_GOTO_ENABLED or not MINIMAP_TOKEN" in minimap)
    print()


def main():
    pp = load_planner()
    print("검증 대상: ROS2_자율주행_및_연동(이다은)/smart_factory_sim/patrol_planner.py\n")

    test_grid_basics(pp)
    test_unknown_is_obstacle(pp)
    test_rejects_rectangle_from_wrong_free_threshold(pp)
    test_reachability(pp)
    test_waypoint_quality(pp)
    test_spacing(pp)
    test_geodesic_distance(pp)
    test_ordering(pp)
    test_geodesic_beats_straight(pp)
    test_yaw(pp)
    test_output_format(pp)
    test_failure_modes(pp)
    test_determinism(pp)
    test_contract_planner_to_node(pp)
    test_navigation_generation_races()
    test_critical_detection_hold()
    test_wiring()

    print("모든 테스트 통과!")


if __name__ == "__main__":
    main()
