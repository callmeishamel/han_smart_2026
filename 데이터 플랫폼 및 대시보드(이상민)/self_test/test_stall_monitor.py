r"""
self_test/test_stall_monitor.py

이다은님 폴더 `smart_factory_sim/stall_monitor.py` — LiDAR 에 안 보이는 낮은
장애물을 "못 나가는 것"으로 알아채는 로직을 ROS2 없이 검증합니다.

왜 필요한가
-----------
2D LiDAR 는 바닥에서 약 0.18m 한 높이만 봅니다. **그보다 낮은 것은 영영 안
보입니다** — 문턱, 케이블 트레이, 파렛트 하단, 낮은 받침대. 로봇은 앞이 비어
있다고 믿고 계속 밀어붙이고 바퀴만 헛돕니다.

센서가 못 보면 행동으로 알아내야 합니다. "전진 명령을 주고 있는데 지도상 위치가
몇 초째 그대로다" 가 곧 "앞에 뭔가 있다" 입니다.

여기서 가장 중요한 건 **헛알람을 안 내는 것**입니다. 멀쩡한 통로를 장애물로
찍어 버리면 순찰 경로에서 그 길이 영영 빠집니다. 그래서 "안 울린다" 쪽 검증이
더 많습니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_stall_monitor.py
"""

import json
import math
import os
import sys
import tempfile
import types

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_TARGET = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                       "smart_factory_sim", "stall_monitor.py")


def check(desc, condition, detail=""):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def load_monitor():
    if not os.path.exists(_TARGET):
        print(f"[중단] 검증 대상을 찾지 못했습니다:\n       {_TARGET}")
        raise SystemExit(1)

    for name in ["rclpy", "rclpy.node", "rclpy.qos", "rclpy.time", "rclpy.executors",
                 "nav_msgs", "nav_msgs.msg", "geometry_msgs", "geometry_msgs.msg",
                 "std_msgs", "std_msgs.msg", "nav2_msgs", "nav2_msgs.msg",
                 "turtlebot3_msgs", "turtlebot3_msgs.msg",
                 "tf2_ros", "tf2_ros.buffer", "tf2_ros.transform_listener"]:
        sys.modules.setdefault(name, types.ModuleType(name))

    sys.modules["rclpy.node"].Node = object
    sys.modules["rclpy.time"].Time = lambda *a, **kw: None
    sys.modules["rclpy"].time = sys.modules["rclpy.time"]
    sys.modules["rclpy"].ok = lambda: True
    sys.modules["rclpy.qos"].QoSProfile = lambda *a, **kw: None
    sys.modules["rclpy.qos"].ReliabilityPolicy = types.SimpleNamespace(RELIABLE=object())
    sys.modules["rclpy.qos"].DurabilityPolicy = types.SimpleNamespace(
        TRANSIENT_LOCAL=object())
    sys.modules["rclpy.qos"].qos_profile_sensor_data = object()

    class _ExternalShutdownException(Exception):
        pass

    sys.modules["rclpy.executors"].ExternalShutdownException = _ExternalShutdownException
    sys.modules["nav_msgs.msg"].OccupancyGrid = object
    sys.modules["nav_msgs.msg"].Odometry = object
    sys.modules["geometry_msgs.msg"].Twist = object
    sys.modules["std_msgs.msg"].String = object
    sys.modules["nav2_msgs.msg"].CostmapFilterInfo = object
    sys.modules["turtlebot3_msgs.msg"].SensorState = object

    class _TransformException(Exception):
        pass

    sys.modules["tf2_ros"].TransformException = _TransformException
    sys.modules["tf2_ros.buffer"].Buffer = object
    sys.modules["tf2_ros.transform_listener"].TransformListener = object

    import importlib.util
    spec = importlib.util.spec_from_file_location("stall_monitor_test", _TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def feed(detector, seconds, pose_fn, cmd, step=0.2):
    """seconds 동안 step 간격으로 표본을 넣고, 처음 발화한 결과를 돌려준다."""
    fired = None
    ticks = int(seconds / step) + 1
    for index in range(ticks):
        now = index * step
        hit = detector.update(now, pose_fn(now), cmd)
        if hit is not None and fired is None:
            fired = hit
    return fired


def test_detects_stall(sm):
    print("=== 못 나가면 알아채는가 ===")
    detector = sm.StallDetector(window_sec=3.0, min_move_m=0.05)
    # 전진 명령을 주는데 위치가 (2, 3) 에서 꿈쩍도 않는다. 로봇은 +x 를 향함.
    hit = feed(detector, 5.0, lambda t: (2.0, 3.0, 0.0), cmd=0.08)
    check("걸림을 알아챈다", hit is not None)
    check("  장애물을 로봇 앞쪽에 찍는다", hit[0] > 2.0 and abs(hit[1] - 3.0) < 1e-6,
          str(hit))
    check("  거리는 front_offset 만큼", abs(hit[0] - 2.20) < 1e-6, str(hit[0]))

    # 로봇이 +y 를 보고 있으면 장애물도 +y 쪽
    detector = sm.StallDetector(window_sec=3.0)
    hit = feed(detector, 5.0, lambda t: (2.0, 3.0, math.pi / 2), cmd=0.08)
    check("바라보는 방향으로 찍는다", hit is not None and hit[1] > 3.0, str(hit))

    # 후진 중이면 뒤쪽
    detector = sm.StallDetector(window_sec=3.0)
    hit = feed(detector, 5.0, lambda t: (2.0, 3.0, 0.0), cmd=-0.08)
    check("후진 중이면 뒤쪽으로 찍는다", hit is not None and hit[0] < 2.0, str(hit))
    print()


def test_no_false_alarms(sm):
    """헛알람이 더 위험합니다. 멀쩡한 길을 장애물로 찍으면 순찰에서 빠집니다."""
    print("=== 헛알람을 내지 않는가 ===")

    detector = sm.StallDetector(window_sec=3.0, min_move_m=0.05)
    moving = feed(detector, 6.0, lambda t: (t * 0.1, 0.0, 0.0), cmd=0.08)
    check("잘 가고 있으면 안 울린다", moving is None)

    detector = sm.StallDetector(window_sec=3.0)
    idle = feed(detector, 6.0, lambda t: (2.0, 3.0, 0.0), cmd=0.0)
    check("명령이 없으면(정지 중) 안 울린다", idle is None)

    detector = sm.StallDetector(window_sec=3.0)
    turning = feed(detector, 6.0, lambda t: (2.0, 3.0, t), cmd=0.0)
    check("제자리 회전만 하면 안 울린다", turning is None)

    detector = sm.StallDetector(window_sec=3.0)
    short = feed(detector, 2.0, lambda t: (2.0, 3.0, 0.0), cmd=0.08)
    check("창을 못 채우면 안 울린다 (3초 조건에 2초만)", short is None)

    # 앞뒤로 흔들리다 제자리로 돌아오는 경우 — 처음/끝만 비교하면 놓친다
    detector = sm.StallDetector(window_sec=3.0, min_move_m=0.30)
    def wobble(t):
        return (2.0 + 0.1 * math.sin(t * 3.0), 3.0, 0.0)
    check("작게 흔들리는 것은 '못 나감' 으로 본다",
          feed(detector, 5.0, wobble, cmd=0.08) is not None)

    detector = sm.StallDetector(window_sec=3.0, min_move_m=0.05)
    def creep(t):
        return (2.0 + 0.02 * t, 3.0, 0.0)     # 3초에 6cm — 기준 위
    check("느려도 꾸준히 나가면 안 울린다", feed(detector, 6.0, creep, cmd=0.08) is None)

    detector = sm.StallDetector(window_sec=3.0)
    check("위치를 모르면(TF 없음) 안 울린다",
          feed(detector, 6.0, lambda t: None, cmd=0.08) is None)
    print()


def test_rotation_stall(sm):
    print("=== 회전 명령 미구동도 알아채는가 ===")
    detector = sm.RotationStallDetector(
        window_sec=3.0, min_cmd_speed=0.10,
        min_turn_rad=math.radians(8.0))
    fired = False
    for index in range(26):
        now = index * 0.2
        fired = detector.update(now, (2.0, 3.0, 0.0), 0.4) or fired
    check("제자리 회전 명령인데 yaw가 그대로면 울린다", fired)

    detector = sm.RotationStallDetector(window_sec=3.0)
    fired = False
    for index in range(31):
        now = index * 0.2
        fired = detector.update(now, (2.0, 3.0, 0.2 * now), 0.4) or fired
    check("정상적으로 회전하면 안 울린다", not fired)

    detector = sm.RotationStallDetector(window_sec=3.0)
    fired = False
    for index in range(31):
        now = index * 0.2
        fired = detector.update(now, (2.0, 3.0, math.pi), 0.0) or fired
    check("각속도 명령이 없으면 안 울린다", not fired)

    detector = sm.RotationStallDetector(window_sec=3.0)
    fired = False
    for index in range(31):
        now = index * 0.2
        # +pi/-pi 경계를 지나도 실제 회전량을 올바르게 계산해야 한다.
        yaw = math.atan2(math.sin(3.10 + 0.2 * now),
                         math.cos(3.10 + 0.2 * now))
        fired = detector.update(now, (2.0, 3.0, yaw), 0.4) or fired
    check("yaw 래핑 경계에서도 정상 회전을 오인하지 않는다", not fired)
    print()


def test_cooldown(sm):
    print("=== 같은 자리를 도배하지 않는가 ===")
    detector = sm.StallDetector(window_sec=3.0, cooldown_sec=15.0)
    fires = []
    for index in range(150):                   # 30초
        now = index * 0.2
        hit = detector.update(now, (2.0, 3.0, 0.0), 0.08)
        if hit is not None:
            fires.append(now)
    check("30초 동안 2번만 울린다 (쿨다운 15초)", len(fires) == 2, str(fires))
    check("  간격이 쿨다운 이상", fires[1] - fires[0] >= 15.0, str(fires))
    print()


def test_encoder_evidence(sm):
    print("=== 엔코더로 구동계 이상을 장애물과 구분하는가 ===")
    # (monotonic_sec, left_encoder, right_encoder, torque)
    moving = [(0.0, 100, 200, True), (3.0, 140, 245, True)]
    frozen = [(0.0, 100, 200, True), (3.0, 100, 200, True)]
    one_wheel = [(0.0, 100, 200, True), (3.0, 140, 200, True)]
    torque_off = [(0.0, 100, 200, True), (3.0, 140, 245, False)]

    check("양쪽 엔코더가 변했을 때만 낮은 장애물 후보",
          sm.classify_encoder_evidence(moving, 0.0, 3.0) == 'encoder_motion')
    check("양쪽 엔코더가 그대로면 구동계 이상",
          sm.classify_encoder_evidence(frozen, 0.0, 3.0) == 'no_encoder_motion')
    check("한쪽 바퀴만 멈춰도 구동계 이상",
          sm.classify_encoder_evidence(one_wheel, 0.0, 3.0) == 'one_wheel_no_motion')
    check("토크가 꺼진 경우는 장애물로 저장하지 않음",
          sm.classify_encoder_evidence(torque_off, 0.0, 3.0) == 'torque_disabled')
    check("센서 표본이 없으면 안전하게 구동계 이상",
          sm.classify_encoder_evidence([], 0.0, 3.0) == 'sensor_state_missing')
    print()


def test_obstacle_map(sm):
    print("=== 장애물 목록 관리 ===")
    store = sm.BlindObstacleMap(merge_distance_m=0.40, radius_m=0.20)

    first = store.add(1.0, 1.0, now=100.0)
    check("새 항목이 생긴다", len(store.obstacles) == 1 and first.hits == 1)

    store.add(1.2, 1.05, now=101.0)
    check("가까운 것은 합쳐진다", len(store.obstacles) == 1, str(len(store.obstacles)))
    check("  걸린 횟수가 는다", store.obstacles[0].hits == 2)
    check("  위치가 조금 당겨진다",
          1.0 < store.obstacles[0].x < 1.2, str(store.obstacles[0].x))

    store.add(5.0, 5.0, now=102.0)
    check("먼 것은 새로 생긴다", len(store.obstacles) == 2)

    removed = store.prune(max_age_sec=10.0, now=200.0)
    check("오래된 것은 지운다", removed == 2 and not store.obstacles, str(removed))

    store.add(3.0, 3.0, now=300.0)
    check("max_age_sec=0 이면 영구 보관",
          store.prune(max_age_sec=0.0, now=1e9) == 0 and len(store.obstacles) == 1)

    check("전부 지우기", store.clear() == 1 and not store.obstacles)
    print()


def test_persistence(sm):
    """문턱은 치우지 않는 한 계속 거기 있습니다. 재시작마다 잊으면 매번 들이받습니다."""
    print("=== 재시작해도 기억하는가 ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "blind.json")

        store = sm.BlindObstacleMap()
        store.add(1.5, -2.5, now=500.0)
        store.add(4.0, 0.0, now=501.0)
        check("파일로 저장된다", store.save(path) and os.path.exists(path))

        again = sm.BlindObstacleMap()
        check("다시 읽힌다", again.load(path) == 2)
        check("  좌표가 보존된다",
              abs(again.obstacles[0].x - 1.5) < 1e-9
              and abs(again.obstacles[0].y + 2.5) < 1e-9)
        check("  시각도 보존된다", abs(again.obstacles[0].first_seen - 500.0) < 1e-9)

        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        check("사람이 읽고 고칠 수 있는 JSON", "obstacles" in payload
              and len(payload["obstacles"]) == 2)

        check("없는 파일은 0 (예외 아님)",
              sm.BlindObstacleMap().load(os.path.join(tmp, "없음.json")) == 0)
        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("{ 이건 JSON 이 아닙니다")
        check("깨진 파일도 0 (예외 아님)", sm.BlindObstacleMap().load(broken) == 0)
    print()


def test_keepout_mask(sm):
    print("=== Nav2 keepout 마스크 ===")
    import numpy as np

    store = sm.BlindObstacleMap(radius_m=0.20)
    store.add(1.0, 1.0)

    info = types.SimpleNamespace(
        width=100, height=100, resolution=0.05,
        origin=types.SimpleNamespace(
            position=types.SimpleNamespace(x=0.0, y=0.0)))
    mask = store.to_mask(info)

    check("마스크 크기가 지도와 같다", mask.shape == (100, 100), str(mask.shape))
    check("장애물 자리는 100 (통행 금지)", mask[20, 20] == 100, str(mask[20, 20]))
    check("먼 곳은 0 (통행 가능)", mask[80, 80] == 0)
    check("반경만큼만 칠한다 — 지도를 통째로 막지 않는다",
          0 < int((mask > 0).sum()) < 200, str(int((mask > 0).sum())))

    empty = sm.BlindObstacleMap().to_mask(info)
    check("장애물이 없으면 전부 0", empty is not None and int(empty.sum()) == 0)

    bad = types.SimpleNamespace(
        width=0, height=0, resolution=0.0,
        origin=types.SimpleNamespace(position=types.SimpleNamespace(x=0.0, y=0.0)))
    check("지도 정보가 깨졌으면 None", store.to_mask(bad) is None)
    print()


def test_first_map_publishes_keepout(sm):
    """초기화 때 만들지 못한 영속 장애물 마스크를 첫 지도에서 발행한다."""
    print("=== 첫 지도 수신 시 keepout 발행 ===")
    published = []
    node = types.SimpleNamespace(
        latest_map=None,
        _publish_mask=lambda: published.append(True),
    )
    map_msg = object()

    sm.StallMonitorNode._on_map(node, map_msg)

    check("수신한 지도를 보관한다", node.latest_map is map_msg)
    check("첫 지도에서 keepout 마스크를 발행한다", published == [True])
    print()


def test_wiring():
    """감지만 하고 아무도 안 보면 소용이 없습니다. 소비하는 쪽을 확인합니다."""
    print("=== 배선 ===")
    base = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)")

    with open(_TARGET, encoding="utf-8") as f:
        monitor = f.read()
    check("**/cmd_vel 을 발행하지 않는다** (Nav2/auto_mapper 와 안 싸우도록)",
          "create_publisher(Twist" not in monitor)
    check("  /cmd_vel 은 구독만 한다", "create_subscription(Twist, '/cmd_vel'" in monitor)
    check("  선속도와 각속도를 모두 감시한다",
          "self.cmd_angular = float(msg.angular.z)" in monitor
          and "RotationStallDetector" in monitor)
    check("  결과를 /blind_obstacles 로 알린다",
          "create_publisher(String, '/blind_obstacles'" in monitor)
    check("  엔코더 미구동은 /actuation_fault로 알린다",
          "create_publisher(String, '/actuation_fault'" in monitor)
    check("  Nav2 keepout 마스크도 낸다", "/keepout_filter_mask" in monitor)
    check("  나중에 시작해도 저장 /map을 받는다",
          "create_subscription(OccupancyGrid, '/map', self._on_map, map_qos)" in monitor
          and "durability=DurabilityPolicy.TRANSIENT_LOCAL" in monitor)
    check("  Cartographer volatile /map도 계속 받는다",
          "create_subscription(OccupancyGrid, '/map', self._on_map, 1)" in monitor)
    check("  nav2_msgs 가 없어도 죽지 않는다",
          "except ImportError" in monitor and "CostmapFilterInfo = None" in monitor)

    with open(os.path.join(base, "smart_factory_sim", "patrol_planner.py"),
              encoding="utf-8") as f:
        planner = f.read()
    check("순찰 계획이 그 자리를 피한다", "carve_blind_obstacles" in planner)
    check("  도달 가능성 판정보다 먼저 파낸다",
          planner.index("carve_blind_obstacles(grid, safe")
          < planner.index("usable = reachable_mask"))

    with open(os.path.join(base, "smart_factory_sim", "auto_mapper.py"),
              encoding="utf-8") as f:
        mapper = f.read()
    check("자율 매핑이 그 방향을 피한다", "_blind_penalty" in mapper)
    check("  감점이 미탐사 점수보다 크다", "BLIND_OBSTACLE_PENALTY = 120.0" in mapper)

    with open(os.path.join(base, "dashboard_link", "minimap_renderer.py"),
              encoding="utf-8") as f:
        minimap = f.read()
    check("미니맵이 목록을 받는다", "_on_blind_obstacles" in minimap)
    check("  화면으로 내보낸다", '"blind_obstacles"' in minimap)
    check("  계획에도 넘긴다", "blind_obstacles=self.get_blind_obstacles()" in minimap)

    with open(os.path.join(base, "setup.py"), encoding="utf-8") as f:
        setup = f.read()
    check("ros2 run 으로 실행할 수 있다", "stall_monitor = smart_factory_sim" in setup)

    with open(os.path.join(base, "run_real_autonomous_mapping.sh"),
              encoding="utf-8") as f:
        mapping_runner = f.read()
    check("실물 자율매핑 실행기가 낮은 장애물 감시도 함께 시작한다",
          'ros2 run smart_factory_sim stall_monitor' in mapping_runner)
    check("  Jetson의 /cmd_vel 구독자를 시작 전에 확인한다",
          'cmd_vel_subscribers=' in mapping_runner
          and '/^(Subscription|Subscriber) count:/' in mapping_runner)

    with open(os.path.join(base, "config", "nav2_params.yaml"),
              encoding="utf-8") as f:
        nav2_params = f.read()
    check("로컬·글로벌 코스트맵이 모두 keepout 필터를 사용한다",
          nav2_params.count('filters: ["keepout_filter"]') == 2)
    check("두 필터가 stall_monitor의 정보 토픽을 구독한다",
          nav2_params.count('filter_info_topic: "/costmap_filter_info"') == 2)
    print()


def main():
    sm = load_monitor()
    print("검증 대상: ROS2_자율주행_및_연동(이다은)/smart_factory_sim/stall_monitor.py\n")

    test_detects_stall(sm)
    test_no_false_alarms(sm)
    test_rotation_stall(sm)
    test_cooldown(sm)
    test_encoder_evidence(sm)
    test_obstacle_map(sm)
    test_persistence(sm)
    test_keepout_mask(sm)
    test_first_map_publishes_keepout(sm)
    test_wiring()

    print("모든 테스트 통과!")


if __name__ == "__main__":
    main()
