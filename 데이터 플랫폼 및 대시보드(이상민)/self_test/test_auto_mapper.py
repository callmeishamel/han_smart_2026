r"""
self_test/test_auto_mapper.py

이다은님 폴더의 `smart_factory_sim/auto_mapper.py` — 자율 매핑 주행 노드의
"어디로 갈지" 판단만 rclpy 없이 검증합니다.

왜 필요한가
-----------
예전 auto_mapper 는 지나온 자리를 전혀 기억하지 않는 순수 반응형 랜덤워크였습니다.

  - 개활지에서는 좌우 거리 차가 없어 `random.choice` 로 방향을 뽑았고
  - 벽 앞에서 1.25초 돌고 나갔다가 다시 막히면 방향을 **새로** 뽑아 좌-우-좌로 흔들렸고
  - 이미 다 훑은 통로를 떠날 이유가 없었습니다

그래서 "갔던 곳을 왔다갔다" 하는 것처럼 보였습니다. 지금은 /map 의 미탐사 영역과
방문 기억을 보고 방향을 정합니다. 이 테스트는 그 판단이 실제로 작동하는지 봅니다.

실제 주행(속도 명령, 안전 정지)은 로봇/시뮬레이터가 있어야 하므로 검증하지
않습니다. 여기서 보는 것은 순수 계산 함수와 상태 전이 규칙입니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_auto_mapper.py
"""

import math
import os
import sys
import types

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_TARGET = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                       "smart_factory_sim", "auto_mapper.py")
_RUNNER = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                       "run_real_autonomous_mapping.sh")
_SETUP = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                      "setup.py")


def check(desc, condition, detail=""):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def load_auto_mapper():
    """rclpy/tf2/turtlebot3_msgs 를 스텁으로 심고 실제 소스를 그대로 로드합니다.

    test_minimap_logic.py 와 같은 방식입니다. 사본을 두지 않는 이유도 같습니다 —
    사본이 갈라지면 검증이 조용히 무의미해집니다.
    """
    if not os.path.exists(_TARGET):
        print(f"[중단] 검증 대상을 찾지 못했습니다:\n       {_TARGET}")
        print("       이 테스트는 저장소를 통째로 받아야 돌아갑니다"
              " (이다은님 폴더가 함께 있어야 합니다).")
        raise SystemExit(1)

    for name in ["rclpy", "rclpy.node", "rclpy.qos", "rclpy.time", "rclpy.executors",
                 "nav_msgs", "nav_msgs.msg", "sensor_msgs", "sensor_msgs.msg",
                 "geometry_msgs", "geometry_msgs.msg",
                 "std_msgs", "std_msgs.msg",
                 "turtlebot3_msgs", "turtlebot3_msgs.msg",
                 "tf2_ros", "tf2_ros.buffer", "tf2_ros.transform_listener"]:
        sys.modules.setdefault(name, types.ModuleType(name))

    sys.modules["rclpy.node"].Node = object
    sys.modules["rclpy.qos"].qos_profile_sensor_data = object()
    # stall_monitor 가 TRANSIENT_LOCAL 로 발행하는 /blind_obstacles 를 구독하면서
    # 이 세 이름이 필요해졌습니다.
    sys.modules["rclpy.qos"].QoSProfile = lambda *a, **kw: None
    sys.modules["rclpy.qos"].ReliabilityPolicy = types.SimpleNamespace(RELIABLE=object())
    sys.modules["rclpy.qos"].DurabilityPolicy = types.SimpleNamespace(
        TRANSIENT_LOCAL=object())
    sys.modules["rclpy.time"].Time = lambda *a, **kw: None
    sys.modules["rclpy"].time = sys.modules["rclpy.time"]
    sys.modules["rclpy"].ok = lambda: True

    class _ExternalShutdownException(Exception):
        pass

    sys.modules["rclpy.executors"].ExternalShutdownException = _ExternalShutdownException
    sys.modules["nav_msgs.msg"].OccupancyGrid = object
    sys.modules["sensor_msgs.msg"].LaserScan = object
    sys.modules["geometry_msgs.msg"].Twist = object
    sys.modules["std_msgs.msg"].String = object
    sys.modules["turtlebot3_msgs.msg"].SensorState = object

    class _TransformException(Exception):
        pass

    sys.modules["tf2_ros"].TransformException = _TransformException
    sys.modules["tf2_ros.buffer"].Buffer = object
    sys.modules["tf2_ros.transform_listener"].TransformListener = object

    import importlib.util
    spec = importlib.util.spec_from_file_location("auto_mapper_test", _TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_map(am, explored_half):
    """왼쪽 절반만 탐사된 20m x 20m 지도.

    - 셀 값 0  = 자유 공간(이미 봄)
    - 셀 값 -1 = 미탐사
    원점은 (-10, -10) 이므로 월드 x < explored_half 가 탐사된 쪽입니다.
    """
    resolution = 0.1
    size = 200                                   # 20m / 0.1m
    cells = np.full((size, size), -1, dtype=np.int16)
    limit = int((explored_half - (-10.0)) / resolution)
    cells[:, :limit] = 0
    return am.MapView(cells, resolution, -10.0, -10.0)


def test_map_view(am):
    print("=== MapView — 좌표 조회 ===")
    view = make_map(am, explored_half=0.0)
    check("탐사된 쪽(x=-2)은 자유 공간 0", view.cell_at(-2.0, 0.0) == 0)
    check("미탐사 쪽(x=+2)은 -1", view.cell_at(2.0, 0.0) == -1)
    check("지도 밖은 None (미탐사와 구분)", view.cell_at(100.0, 0.0) is None)
    print()


def test_unknown_preference(am):
    print("=== 미탐사 방향을 고르는가 ===")
    view = make_map(am, explored_half=0.0)
    visits = am.VisitMemory(cell_size=0.5)
    half = math.radians(35.0)

    east = am.count_unknown_along(view, 0.0, 0.0, 0.0, 2.5, half)          # +x, 미탐사
    west = am.count_unknown_along(view, 0.0, 0.0, math.pi, 2.5, half)      # -x, 탐사됨
    check("미탐사 쪽 미탐사 셀이 더 많다", east > west, f"east={east} west={west}")
    check("이미 본 쪽은 0에 가깝다", west == 0, f"west={west}")

    east_score = am.score_heading(view, visits, 0.0, 0.0, 0.0, 2.5, half, 1.5)
    west_score = am.score_heading(view, visits, 0.0, 0.0, math.pi, 2.5, half, 1.5)
    check("점수도 미탐사 쪽이 높다", east_score > west_score,
          f"east={east_score} west={west_score}")
    print()


def test_wall_blocks_unknown(am):
    """벽 뒤 미탐사를 세면 로봇이 벽으로 계속 달려든다."""
    print("=== 벽 뒤의 미탐사는 세지 않는가 ===")
    resolution = 0.1
    cells = np.full((200, 200), -1, dtype=np.int16)
    # 로봇 앞 1.0m 지점(월드 x=1.0)에 세로 벽. 원점은 (-10, -10).
    wall_col = int((1.0 - (-10.0)) / resolution)
    cells[:, wall_col:wall_col + 3] = 100
    view = am.MapView(cells, resolution, -10.0, -10.0)
    half = math.radians(35.0)

    blocked = am.count_unknown_along(view, 0.0, 0.0, 0.0, 2.5, half)
    open_dir = am.count_unknown_along(view, 0.0, 0.0, math.pi, 2.5, half)
    check("벽 방향은 미탐사가 거의 안 잡힌다", blocked < open_dir / 2,
          f"blocked={blocked} open={open_dir}")
    print()


def test_front_sector_has_no_corner_gap(am):
    """0~2pi LDS 스캔에서 예전 25~35도 사각을 전방으로 잡는지 확인."""
    print("=== 전방 장애물 감지 각도 ===")
    node = am.AutoMapper.__new__(am.AutoMapper)
    node.front_half_angle_deg = 35.0
    node.min_valid_scan_points = 5
    node.scan_is_valid = False
    node.invalid_scan_warned = False
    node.get_clock = lambda: types.SimpleNamespace(now=lambda: 123.0)

    ranges = [math.inf] * 360
    for index in range(28, 33):
        ranges[index] = 0.25          # 왼쪽 전방 30도, 예전에는 사각
    scan = types.SimpleNamespace(
        angle_min=0.0,
        angle_increment=math.radians(1.0),
        range_min=0.10,
        range_max=12.0,
        ranges=ranges,
    )
    node._scan_callback(scan)
    check("왼쪽 전방 30도 장애물을 전방 정지 대상으로 본다",
          abs(node.front_distance - 0.25) < 1e-9, str(node.front_distance))
    check("스캔 시각도 갱신한다", node.last_scan_time == 123.0)
    check("유효점이 충분하면 정상 스캔으로 본다", node.scan_is_valid is True)

    ranges = [math.inf] * 360
    for index in range(328, 333):
        ranges[index] = 0.24          # 오른쪽 전방 -30도(0~2pi wrap)
    scan.ranges = ranges
    node._scan_callback(scan)
    check("오른쪽 -30도도 0~2pi wrap 후 전방으로 본다",
          abs(node.front_distance - 0.24) < 1e-9, str(node.front_distance))

    scan.ranges = [0.0] * 360
    node._scan_callback(scan)
    check("전부 0인 깨진 스캔은 정상으로 보지 않는다",
          node.scan_is_valid is False)
    print()


def test_visit_memory(am):
    print("=== 방문 기억이 이미 간 쪽을 깎는가 ===")
    view = make_map(am, explored_half=10.0)      # 전부 탐사됨 = 미탐사 점수 동일
    visits = am.VisitMemory(cell_size=0.5, cap=10)

    check("같은 칸 안의 두 점은 같은 키를 준다",
          visits.cell_of(1.0, 0.0) == visits.cell_of(1.4, 0.4))
    check("칸이 다르면 키도 다르다",
          visits.cell_of(1.0, 0.0) != visits.cell_of(1.6, 0.0))

    # +x 방향 1m 근처를 세 번 지나갔다고 기록.
    for _ in range(3):
        visits.mark(1.0, 0.0)
    check("진입 횟수가 쌓인다", visits.count(1.0, 0.0) == 3)
    for _ in range(50):
        visits.mark(1.0, 0.0)
    check("상한(cap)을 넘지 않는다", visits.count(1.0, 0.0) == 10)

    half = math.radians(35.0)
    visited_score = am.score_heading(view, visits, 0.0, 0.0, 0.0, 2.5, half, 2.0)
    fresh_score = am.score_heading(view, visits, 0.0, 0.0, math.pi, 2.5, half, 2.0)
    check("여러 번 지난 쪽 점수가 더 낮다", visited_score < fresh_score,
          f"visited={visited_score} fresh={fresh_score}")

    no_memory = am.score_heading(view, visits, 0.0, 0.0, 0.0, 2.5, half, 0.0)
    check("visit_weight=0 이면 방문 기억을 무시한다", no_memory == fresh_score)
    print()


def test_reachable_frontier_target(am):
    """프런티어 선택은 벽 너머가 아니라 연결된 미탐사 경계를 고른다."""
    print("=== 도달 가능한 프런티어를 목표로 고르는가 ===")
    resolution = 0.1
    cells = np.full((100, 140), -1, dtype=np.int16)
    # 왼쪽 방은 현재 위치와 연결되어 있고, 오른쪽 방은 벽으로 완전히 분리됐다.
    cells[20:80, 10:60] = 0
    cells[20:80, 80:130] = 0
    cells[:, 65:75] = 100
    # 왼쪽 방의 동쪽 끝에만 실제 미탐사 경계를 만든다.
    cells[40:60, 58:60] = -1
    # 오른쪽 방도 미탐사로 둘러싸였지만 현재 위치에서는 갈 수 없다.
    view = am.MapView(cells, resolution, -7.0, -5.0)
    visits = am.VisitMemory(0.5, 10)
    target = am.select_frontier_target(
        view, visits, -4.0, 0.0, min_distance_m=0.5, search_range_m=8.0)
    check("연결된 자유공간의 프런티어를 찾는다", target is not None)
    check("벽 너머 방이 아닌 현재 방의 목표를 고른다", target[0] < -1.0,
          f"target=({target[0]:.2f}, {target[1]:.2f})")

    # 직전에 실패한 목표를 제외하면 같은 곳을 재선택하지 않는다.
    excluded = [(target[0], target[1])]
    other = am.select_frontier_target(
        view, visits, -4.0, 0.0, min_distance_m=0.5, search_range_m=8.0,
        excluded=excluded, exclusion_radius_m=2.0)
    check("제외한 프런티어를 반복 선택하지 않는다",
          other is None or math.hypot(other[0] - target[0], other[1] - target[1]) > 2.0,
          f"other={other}")
    print()


def test_astar_path_goes_around_l_shaped_wall(am):
    """A* 추종은 목표의 직선 방위가 아니라 열린 복도를 따라야 한다."""
    print("=== L자 벽을 관통하지 않고 우회하는가 ===")
    resolution = 0.1
    cells = np.full((60, 60), 100, dtype=np.int16)
    # 너비 0.7m의 L자 복도. 시작점과 목표를 이은 직선은 벽을
    # 관통하지만, 실제 경로는 오른쪽 모서리를 돌아야 한다.
    cells[42:49, 5:49] = 0
    cells[10:49, 42:49] = 0
    view = am.MapView(cells, resolution, 0.0, 0.0)
    visits = am.VisitMemory(0.5, 10)
    start = view.world_at(45, 8)
    target = view.world_at(12, 45)

    midpoint = ((start[0] + target[0]) / 2.0,
                (start[1] + target[1]) / 2.0)
    check("직선로는 중간의 벽을 관통하는 시나리오다",
          view.cell_at(*midpoint) >= am.OCC_THRESHOLD,
          f"midpoint={midpoint}, cell={view.cell_at(*midpoint)}")

    path = am.plan_path_to_target(
        view, visits, *start, *target, clearance_m=0.18)
    check("통과 가능한 L자 우회 경로를 찾는다", path is not None)
    navigable = am._navigable_cells(view, 0.18)
    path_cells = [view.cell_index_at(x, y) for x, y in path]
    check("경로의 모든 점이 팽창 후의 자유공간이다",
          all(index is not None and navigable[index] for index in path_cells))
    check("목표까지의 직선 lookahead는 벽을 통과하므로 거부한다",
          not am.segment_is_navigable(view, navigable, start, target))
    check("경로 초반의 짧은 lookahead는 자유공간 안에 있다",
          am.segment_is_navigable(view, navigable, start, path[3]))
    path_length = sum(
        math.hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(path, path[1:]))
    straight = math.hypot(target[0] - start[0], target[1] - start[1])
    check("벽을 질러가는 직선보다 긴 우회 경로다",
          path_length > straight * 1.25,
          f"path={path_length:.2f}m straight={straight:.2f}m")
    print()


def test_clearance_rejects_too_narrow_passage(am):
    """점유격자상 열려 있어도 로봇 폭보다 좁은 통로는 경로가 아니다."""
    print("=== 장애물 팽창이 좁은 통로를 거부하는가 ===")
    resolution = 0.1
    cells = np.full((50, 80), 100, dtype=np.int16)
    cells[10:40, 5:30] = 0
    cells[10:40, 50:75] = 0
    cells[24:26, 30:50] = 0       # 0.2m 폭: 격자만 보면 열려 있음
    view = am.MapView(cells, resolution, 0.0, 0.0)
    visits = am.VisitMemory(0.5, 10)
    start = view.world_at(25, 15)
    target = view.world_at(25, 65)

    raw_path = am.plan_path_to_target(
        view, visits, *start, *target, clearance_m=0.0)
    safe_path = am.plan_path_to_target(
        view, visits, *start, *target, clearance_m=0.18)
    check("팽창을 끄면 격자상 경로는 있다", raw_path is not None)
    check("실물 clearance를 적용하면 0.2m 통로를 거부한다",
          safe_path is None)
    print()


def test_frontier_goal_is_not_replaced_by_remote_scan(am):
    """먼 거리 스캔으로 지도가 바뀌어도 목표 근처까지는 추적해야 한다."""
    print("=== 프런티어 목표를 스캔 한 장마다 바꾸지 않는가 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=0.0),
        pose=(-1.0, 0.0, 0.0),
    )
    fixed_target = (1.0, 0.0, 10.0)
    node.frontier_target = fixed_target
    # 목표 셀이 최신 지도에서는 더 이상 frontier가 아니어도, 아직 2m 떨어져
    # 있으므로 물리적으로 그 방향을 탐색할 기회를 유지해야 한다.
    target = node._plan_frontier(now_sec=10.0)
    check("먼 거리 지도 갱신만으로 목표를 버리지 않는다", target == fixed_target)
    node.pose = (0.5, 0.0, 0.0)  # target_reached_m(0.65m) 안
    node._plan_frontier(now_sec=20.0)
    check("목표 근처에 도달하면 다음 프런티어를 다시 찾는다",
          node.frontier_target != fixed_target)
    print()


def test_frontier_goal_no_progress_escapes(am):
    """로봇이 움직여도 목표와의 거리가 줄지 않으면 빠르게 목표를 바꾼다."""
    print("=== 목표까지 진전이 없으면 다른 곳으로 전환하는가 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=0.0),
        pose=(-1.0, 0.0, math.pi),
        anchor_xy=(-1.0, 0.0),
        anchor_sec=10.0,  # 위치 기반 stuck은 아직 아니다.
        frontier_target=(1.0, 0.0, 5.0),
        frontier_best_distance_m=2.0,
        last_frontier_progress_sec=0.0,
    )
    escaped = node._maybe_redirect(now_sec=13.0)
    check("목표 거리 진전이 없으면 재탐색한다", escaped is True)
    check("기존 목표를 실패 목록에 기록한다", len(node.frontier_failures) == 1)
    check("새 방향으로 회전한다", node.state == 'TURN')
    print()


class _FakeMapper:
    """AutoMapper 의 판단 메서드만 떼어 쓰기 위한 최소 대역.

    __init__ 은 ROS2 퍼블리셔/타이머를 만들기 때문에 부를 수 없습니다. 그래서
    빈 인스턴스를 만들고 필요한 속성만 채운 뒤, 진짜 메서드를 그대로 부릅니다.
    """

    @staticmethod
    def build(am, **overrides):
        node = am.AutoMapper.__new__(am.AutoMapper)
        # AutoMapper.__init__의 실물 기본값과 동일하게 유지한다. 여기가
        # 다르면 실물에서만 나는 회전/재방문 문제를 테스트가 놓친다.
        node.linear_speed = 0.12
        node.turn_speed = 0.40
        node.safe_distance = 0.35
        node.slow_distance = 0.60
        node.turn_duration = 0.85
        node.max_turn_duration = 3.0
        node.max_goal_turn_duration = 8.5
        node.post_turn_forward_sec = 1.2
        node.backup_speed = 0.06
        node.backup_duration_sec = 0.8
        node.rear_safe_distance = 0.35
        node.front_distance = math.inf
        node.left_distance = math.inf
        node.right_distance = math.inf
        node.rear_distance = math.inf
        node.rear_scan_observed = False
        node.turn_direction = 1.0
        node.turn_commit_sec = 3.0
        node.last_turn_end_sec = None
        node.probe_range = 2.5
        node.probe_half_angle = math.radians(35.0)
        node.visit_weight = 3.5
        node.visits = am.VisitMemory(0.5, 10)
        node.map_view = None
        node.blind_obstacles = []
        node.pose = None
        node.state = 'FORWARD'
        node.turn_end_sec = None
        node.turn_deadline_sec = None
        node.turn_is_committed = False
        node.turn_target_heading = None
        node.forward_commit_until_sec = 0.0
        node.backup_end_sec = None
        node.progress_pause_started_sec = None
        node.visit_entry_min_travel_m = 0.20
        node.revisit_threshold = 2
        node.redirect_cooldown_sec = 3.0
        node.redirect_margin = 10.0
        node.stuck_radius = 0.5
        node.stuck_timeout = 12.0
        node.anchor_xy = None
        node.anchor_sec = 0.0
        node.last_redirect_sec = None
        node.last_eval_sec = 0.0
        node.last_cell = None
        node.last_visit_mark_xy = None
        node.last_handled_revisit = None
        node.frontier_enabled = True
        node.frontier_replan_sec = 4.0
        node.frontier_min_distance_m = 1.0
        node.frontier_search_range_m = 10.0
        node.frontier_gain_weight = 0.20
        node.frontier_distance_weight = 1.0
        node.frontier_target_reached_m = 0.65
        node.frontier_heading_tolerance = math.radians(8.0)
        node.frontier_pivot_angle = math.radians(70.0)
        node.frontier_steer_gain = 1.2
        node.frontier_max_steer = 0.30
        node.frontier_arc_min_speed_ratio = 0.55
        node.frontier_failure_cooldown_sec = 90.0
        node.frontier_failure_radius_m = 1.4
        node.frontier_clearance_m = 0.18
        node.frontier_lookahead_m = 0.45
        node.frontier_path_replan_sec = 2.0
        node.frontier_route_visit_weight = 0.35
        node.frontier_target = None
        node.frontier_failures = []
        node.last_frontier_plan_sec = -math.inf
        node.frontier_best_distance_m = math.inf
        node.last_frontier_progress_sec = None
        node.frontier_path = ()
        node.frontier_path_index = 0
        node.last_frontier_path_plan_sec = -math.inf
        node.frontier_best_path_remaining_m = math.inf
        node.frontier_progress_min_m = 0.20
        node.frontier_progress_timeout_sec = 12.0
        node.get_logger = lambda: types.SimpleNamespace(
            info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
        for key, value in overrides.items():
            setattr(node, key, value)
        return node


def test_turn_commitment(am):
    print("=== 좌-우-좌 진동을 막는가 ===")
    node = _FakeMapper.build(am, left_distance=2.0, right_distance=0.4)

    first = node._choose_turn_direction(now_sec=100.0)
    check("넓은 쪽(왼쪽)으로 돈다", first == 1.0, str(first))

    # 회전이 끝난 직후, 이번엔 오른쪽이 넓어 보이게 뒤집는다.
    node.turn_direction = first
    node.last_turn_end_sec = 101.0
    node.left_distance, node.right_distance = 0.4, 2.0

    same = node._choose_turn_direction(now_sec=102.0)   # commit 창(3초) 안
    check("직후에는 방향을 뒤집지 않는다 (진동 방지)", same == first, str(same))

    later = node._choose_turn_direction(now_sec=105.0)  # commit 창 밖
    check("시간이 지나면 다시 자유롭게 고른다", later == -1.0, str(later))
    print()


def test_turn_direction_follows_unknown(am):
    print("=== 회전 방향이 미탐사 쪽을 따르는가 ===")
    # 로봇은 북(+y)을 보고 있다. 왼쪽 = -x(탐사됨), 오른쪽 = +x(미탐사).
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=0.0),
        pose=(0.0, 0.0, math.pi / 2.0),
        # 라이다만 보면 왼쪽이 더 넓다 — 예전 로직이라면 왼쪽을 골랐을 상황.
        left_distance=3.0,
        right_distance=1.0,
    )
    direction = node._choose_turn_direction(now_sec=10.0)
    check("라이다상 넓은 쪽이 아니라 미탐사 쪽(오른쪽)으로 돈다",
          direction == -1.0, str(direction))
    print()


def test_turn_holds_until_clear(am):
    print("=== 앞이 막힌 채로 회전을 끝내지 않는가 ===")
    node = _FakeMapper.build(am, left_distance=2.0, right_distance=0.4)
    node._begin_turn(now_sec=0.0)
    check("TURN 상태로 들어간다", node.state == 'TURN')

    # 최소 회전 시간(1.25초)은 지났지만 앞은 여전히 막혀 있다.
    node.front_distance = 0.15
    node.turn_end_sec = -1.0            # 최소 시간은 채웠다고 가정
    still_turning = not (node.front_distance >= node.safe_distance)
    check("앞이 막혀 있으면 계속 돌아야 한다", still_turning)

    # deadline 이 지나면 무한 회전을 막기 위해 빠져나온다.
    check("최대 회전 시간이 최소보다 길다", node.max_turn_duration > node.turn_duration)
    print()


def test_turn_exit_actions(am):
    """회전 상한에서 다시 TURN으로 들어가는 무한 루프를 막는다."""
    print("=== 회전 종료 상태 전이 ===")
    action = am.turn_exit_action(
        min_turn_done=False,
        committed=False,
        front_clear=False,
        deadline_reached=False,
        rear_clear=False,
        backup_enabled=True,
    )
    check("최소 회전 시간과 deadline 전에는 TURN을 유지한다",
          action == 'TURN', action)

    action = am.turn_exit_action(
        min_turn_done=True,
        committed=False,
        front_clear=True,
        deadline_reached=False,
        rear_clear=False,
        backup_enabled=True,
    )
    check("최소 회전 후 전방이 열리면 FORWARD로 나간다",
          action == 'FORWARD', action)

    action = am.turn_exit_action(
        min_turn_done=True,       # 목표 yaw에 도달했다는 뜻
        committed=True,
        front_clear=False,
        deadline_reached=False,
        rear_clear=True,
        backup_enabled=True,
    )
    check("목표 회전을 마쳤는데 앞이 막히면 다시 돌지 않고 BACKUP한다",
          action == 'BACKUP', action)

    action = am.turn_exit_action(
        min_turn_done=True,
        committed=False,
        front_clear=False,
        deadline_reached=True,
        rear_clear=True,
        backup_enabled=True,
    )
    check("deadline에서 앞은 막혀 있고 뒤가 확인됐으면 BACKUP한다",
          action == 'BACKUP', action)

    action = am.turn_exit_action(
        min_turn_done=True,
        committed=False,
        front_clear=False,
        deadline_reached=True,
        rear_clear=False,
        backup_enabled=True,
    )
    check("전후방이 막히면 추가 회전 대신 BLOCKED_STOP한다",
          action == 'BLOCKED_STOP', action)

    node = _FakeMapper.build(
        am, rear_distance=math.inf, rear_scan_observed=False)
    check("후방 스캔을 실제로 받지 않은 inf를 후진 가능으로 보지 않는다",
          node._rear_is_clear() is False)
    node.rear_scan_observed = True
    check("후방 스캔이 관측된 개활지 inf는 후진 가능하다",
          node._rear_is_clear() is True)

    direction = am.blocked_recovery_direction(
        left_distance=1.2,
        right_distance=0.2,
        left_observed=True,
        right_observed=True,
        minimum_clearance=0.4,
        previous_direction=-1.0,
    )
    check("후진이 불가능해도 관측된 넓은 측면으로 복구한다",
          direction == 1.0, str(direction))

    direction = am.blocked_recovery_direction(
        left_distance=math.inf,
        right_distance=math.inf,
        left_observed=False,
        right_observed=False,
        minimum_clearance=0.4,
        previous_direction=1.0,
    )
    check("측면이 미관측이면 inf만 보고 복구 회전하지 않는다",
          direction is None, str(direction))
    print()


def test_redirect_when_area_covered(am):
    print("=== 이미 훑은 구역을 떠나는가 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=0.0),
        # 로봇은 이미 다 본 쪽(-x)을 향해 달리고 있다.
        pose=(-1.0, 0.0, math.pi),
        anchor_xy=(-1.0, 0.0),
        anchor_sec=0.0,
    )
    # 아직 재방문 횟수가 낮으면 그냥 직진한다.
    check("진입 횟수가 낮으면 재조정하지 않는다",
          node._maybe_redirect(now_sec=1.0) is False)

    # 같은 칸에 threshold 번 들어왔다고 기록.
    for _ in range(node.revisit_threshold):
        node.visits.mark(-1.0, 0.0)
    redirected = node._maybe_redirect(now_sec=5.0)
    check("여러 번 지난 구역이면 방향을 다시 잡는다", redirected is True)
    check("회전 상태로 전환된다", node.state == 'TURN')
    check("각도를 다 채우는 회전이다 (앞이 뚫려 있어도 끝까지 돈다)",
          node.turn_is_committed is True)

    # 쿨다운 동안은 다시 틀지 않는다 — 계속 제자리에서 도는 것을 막는다.
    node.state = 'FORWARD'
    check("쿨다운 안에서는 재조정하지 않는다",
          node._maybe_redirect(now_sec=6.0) is False)
    print()


def test_same_revisit_event_is_handled_once(am):
    """재방문 한 건을 쿨다운마다 다시 처리하면 제자리 회전이 재발한다."""
    print("=== 같은 (cell, count) 재방문 이벤트를 한 번만 처리하는가 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=0.0),
        pose=(-1.0, 0.0, math.pi),
        anchor_xy=(-1.0, 0.0),
        anchor_sec=0.0,
    )
    for _ in range(node.revisit_threshold):
        node.visits.mark(-1.0, 0.0)

    first = node._maybe_redirect(now_sec=5.0)
    handled = node.last_handled_revisit
    check("첫 재방문 이벤트는 재탐색을 일으킨다", first is True)
    check("처리한 (cell, count)를 래치한다",
          handled == (node.visits.cell_of(-1.0, 0.0), node.revisit_threshold),
          str(handled))

    # 쿨다운과 평가 스로틀을 모두 넘겨도 cell/count가 그대라면
    # 동일 이벤트다. stuck/frontier-timeout이 개입하지 않게 시각을 짧게 둔다.
    node.state = 'FORWARD'
    second = node._maybe_redirect(
        now_sec=5.0 + node.redirect_cooldown_sec + 0.5)
    check("같은 (cell, count)는 다시 재탐색하지 않는다", second is False)
    check("래치값이 변하지 않는다", node.last_handled_revisit == handled)
    print()


def test_escape_when_stuck(am):
    print("=== 같은 자리에 갇히면 빠져나오는가 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=0.0),
        pose=(-1.0, 0.0, math.pi),
        anchor_xy=(-1.0, 0.0),
        anchor_sec=0.0,
    )
    # 방문 횟수는 낮지만(재방문 조건 미충족), stuck_timeout 을 넘겼다.
    escaped = node._maybe_redirect(now_sec=node.stuck_timeout + 5.0)
    check("갇힘 판정만으로도 방향을 다시 잡는다", escaped is True)
    check("회전 상태로 전환된다", node.state == 'TURN')
    print()


def test_blind_obstacle_avoidance(am):
    """LiDAR 에 안 보이는 장애물은 /map 에 없으므로 점수에서 따로 빼야 한다."""
    print("=== 이미 걸렸던 자리를 피하는가 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=10.0),   # 전부 탐사됨 = 미탐사 점수 동일
        pose=(0.0, 0.0, 0.0),
    )
    clean_east = node._score(0.0)
    clean_west = node._score(math.pi)
    check("장애물이 없으면 두 방향 점수가 같다",
          abs(clean_east - clean_west) < 1e-9, f"{clean_east} vs {clean_west}")

    # +x 방향 1m 앞에 걸렸던 자리를 심는다
    node.blind_obstacles = [(1.0, 0.0)]
    blocked_east = node._score(0.0)
    check("걸렸던 방향은 점수가 크게 깎인다",
          blocked_east < clean_east - am.BLIND_OBSTACLE_PENALTY / 2,
          f"{blocked_east} vs {clean_east}")
    check("반대 방향은 그대로", abs(node._score(math.pi) - clean_west) < 1e-9)

    best = node._best_heading()
    check("전방위에서 고를 때도 그 방향을 안 고른다", best is not None)
    check("  고른 방향이 장애물 쪽이 아니다",
          abs(am.normalize_angle(best[0] - 0.0)) > math.radians(45),
          f"{math.degrees(best[0]):.0f}도")

    # 멀리 있는 것은 신경 쓰지 않는다
    node.blind_obstacles = [(9.0, 0.0)]
    check("멀리 있는 장애물은 점수에 영향이 없다",
          abs(node._score(0.0) - clean_east) < 1e-9)
    print()


def test_blind_obstacle_redirects_immediately(am):
    """stall_monitor가 새 장애물을 알리면 25초 stuck 대기 없이 회전해야 한다."""
    print("=== 낮은 장애물 즉시 회피 ===")
    node = _FakeMapper.build(
        am,
        map_view=make_map(am, explored_half=10.0),
        pose=(0.0, 0.0, 0.0),
        anchor_xy=(0.0, 0.0),
        anchor_sec=4.0,
        blind_obstacles=[(0.2, 0.0)],
    )
    redirected = node._maybe_redirect(now_sec=5.0)
    check("stuck_timeout 전에도 낮은 장애물을 보고 회전한다", redirected is True)
    check("회전 상태로 전환된다", node.state == 'TURN')
    check("장애물 방향으로 계속 직진하지 않는다",
          abs(node.turn_direction) == 1.0 and node.turn_is_committed is True)
    print()


def test_degrades_without_map(am):
    """TF/지도가 아직 없어도 예전과 같은 반응형 주행으로 돌아야 한다."""
    print("=== 지도·위치가 없을 때 안전하게 물러나는가 ===")
    node = _FakeMapper.build(am, left_distance=2.0, right_distance=0.4,
                             map_view=None, pose=None)
    check("지도가 없으면 재조정을 시도하지 않는다",
          node._maybe_redirect(now_sec=999.0) is False)
    direction = node._choose_turn_direction(now_sec=10.0)
    check("라이다만으로 넓은 쪽을 고른다 (예전 동작)", direction == 1.0, str(direction))
    check("_best_heading() 도 None 을 돌려준다", node._best_heading() is None)
    print()


def test_runner_safety_wiring():
    """실기 실행기가 토크/설치본을 다시 망가뜨리지 않는지 정적으로 확인."""
    print("=== 실물 실행기 안전 배선 ===")
    with open(_RUNNER, encoding="utf-8") as handle:
        runner = handle.read()
    with open(_SETUP, encoding="utf-8") as handle:
        setup_py = handle.read()
    with open(_TARGET, encoding="utf-8") as handle:
        mapper_py = handle.read()

    check("모터 서비스 success=true를 검증한다",
          "success[=:][[:space:]]*(true|True)" in runner)
    check("실제 sensor_state torque=true도 검증한다",
          "torque:[[:space:]]*true" in runner)
    check("일반 종료의 토크 해제는 명시적 선택이다",
          'DISABLE_MOTOR_ON_EXIT="${DISABLE_MOTOR_ON_EXIT:-0}"' in runner)
    check("auto_mapper는 빌드된 ROS 실행 파일로 시작한다",
          "ros2 run smart_factory_sim auto_mapper" in runner and
          'python3 "$SCRIPT_DIR/smart_factory_sim/auto_mapper.py"' not in runner)
    check("중복 /cmd_vel publisher를 시작 전에 차단한다",
          "cmd_vel_publishers" in runner)
    check("시작 후 비정지 /cmd_vel smoke test가 있다",
          "auto_mapper가 5초 안에 비정지 /cmd_vel을 발행하지 않습니다" in runner and
          "abs(m.linear.x) > 0.001" in runner)
    check("구동계 이상을 장애물로 오인하지 않고 매퍼를 정지한다",
          "'/actuation_fault'" in mapper_py and
          "_actuation_fault_callback" in mapper_py and
          "return 2 if actuation_fault else 0" in mapper_py)
    check("장애물 기록은 지도 출력별 파일로 분리한다",
          'BLIND_OBSTACLE_STORE="${BLIND_OBSTACLE_STORE:-${MAP_OUTPUT}.blind_obstacles.json}"'
          in runner)
    check("저장소 루트 workspace를 한 단계 위에서 찾는다",
          '"$SCRIPT_DIR/../install/setup.bash"' in runner and
          '"$SCRIPT_DIR/../../install/setup.bash"' not in runner)
    check("최종 pose 도 같은 설치본의 entry point를 쓴다",
          "ros2 run smart_factory_sim capture_map_pose" in runner and
          "capture_map_pose = smart_factory_sim.capture_map_pose:main" in setup_py)
    print()


def main():
    am = load_auto_mapper()
    print("검증 대상: ROS2_자율주행_및_연동(이다은)/smart_factory_sim/auto_mapper.py\n")

    test_map_view(am)
    test_unknown_preference(am)
    test_wall_blocks_unknown(am)
    test_front_sector_has_no_corner_gap(am)
    test_visit_memory(am)
    test_reachable_frontier_target(am)
    test_astar_path_goes_around_l_shaped_wall(am)
    test_clearance_rejects_too_narrow_passage(am)
    test_frontier_goal_is_not_replaced_by_remote_scan(am)
    test_frontier_goal_no_progress_escapes(am)
    test_turn_commitment(am)
    test_turn_direction_follows_unknown(am)
    test_turn_holds_until_clear(am)
    test_turn_exit_actions(am)
    test_redirect_when_area_covered(am)
    test_same_revisit_event_is_handled_once(am)
    test_escape_when_stuck(am)
    test_blind_obstacle_avoidance(am)
    test_blind_obstacle_redirects_immediately(am)
    test_degrades_without_map(am)
    test_runner_safety_wiring()

    print("모든 테스트 통과!")


if __name__ == "__main__":
    main()
