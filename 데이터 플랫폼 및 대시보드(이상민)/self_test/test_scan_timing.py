"""LDS scan timestamp 교정과 중복 차단을 ROS 없이 검증한다."""

import importlib.util
import math
import os
import sys
import types


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(
    REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
    "smart_factory_sim", "scan_qos_relay.py")
ROS_ROOT = os.path.join(REPO_ROOT, "ROS2_자율주행_및_연동(이다은)")


def check(description, condition):
    print(f"[{'OK ' if condition else 'FAIL'}] {description}")
    if not condition:
        raise AssertionError(description)


def load_relay():
    for name in [
            "rclpy", "rclpy.node", "rclpy.qos", "rclpy.executors",
            "rclpy._rclpy_pybind11", "sensor_msgs", "sensor_msgs.msg"]:
        sys.modules.setdefault(name, types.ModuleType(name))

    sys.modules["rclpy.node"].Node = object
    sys.modules["rclpy.qos"].HistoryPolicy = types.SimpleNamespace(
        KEEP_LAST=object())
    sys.modules["rclpy.qos"].ReliabilityPolicy = types.SimpleNamespace(
        BEST_EFFORT=object(), RELIABLE=object())
    sys.modules["rclpy.qos"].QoSProfile = lambda *args, **kwargs: None
    sys.modules["rclpy.executors"].ExternalShutdownException = RuntimeError
    sys.modules["rclpy._rclpy_pybind11"].RCLError = RuntimeError
    sys.modules["sensor_msgs.msg"].LaserScan = object

    spec = importlib.util.spec_from_file_location(
        "scan_qos_relay_test", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(sec, nanosec=0):
    return types.SimpleNamespace(sec=sec, nanosec=nanosec)


def message(sec, nanosec=0):
    return types.SimpleNamespace(
        header=types.SimpleNamespace(stamp=stamp(sec, nanosec)),
        ranges=[])


def main():
    relay = load_relay()

    print("=== LiDAR header stamp 분류 ===")
    now = 10_000_000_000
    check("현재와 0.1초 차이는 정상",
          relay.scan_stamp_status(now, now - 100_000_000, 0.35, 0.10) == "ok")
    check("1초 오래된 LDS scan은 stale",
          relay.scan_stamp_status(
              now, now - 1_000_000_000, 0.35, 0.10) == "stale")
    check("미래 시각도 잘못된 시계로 분류",
          relay.scan_stamp_status(
              now, now + 200_000_000, 0.35, 0.10) == "future")
    check("0 stamp는 invalid",
          relay.scan_stamp_status(now, 0, 0.35, 0.10) == "invalid")

    print("\n=== 매핑 신뢰 거리 필터 ===")
    ranges = [0.12, 4.99, 5.0, 5.01, 12.0, math.inf, math.nan]
    masked = relay.mask_ranges_beyond(ranges, 5.0)
    check("임계 초과 유한 반환점만 두 개 제거", masked == 2)
    check("임계 이하의 근거리 반환점은 유지",
          ranges[:3] == [0.12, 4.99, 5.0])
    check("먼 반환점은 Cartographer가 hit/miss로 쓰지 못하게 NaN 처리",
          math.isnan(ranges[3]) and math.isnan(ranges[4]))
    check("기존 inf/NaN 미반환값은 유지",
          math.isinf(ranges[5]) and math.isnan(ranges[6]))

    disabled = [7.0]
    check("임계값 0이면 필터 비활성화",
          relay.mask_ranges_beyond(disabled, 0.0) == 0 and disabled == [7.0])

    print("\n=== stale scan 교정 ===")
    published = []
    warnings = []
    now_obj = types.SimpleNamespace(
        nanoseconds=10_000_000_000,
        to_msg=lambda: stamp(10, 0))
    node = types.SimpleNamespace(
        _last_stamp_ns=None,
        _dropped_stamp_count=0,
        _last_drop_log_monotonic=0.0,
        _restamped_count=0,
        _last_restamp_log_monotonic=0.0,
        restamp_stale=True,
        max_stamp_age_sec=0.35,
        max_future_sec=0.10,
        max_mapping_range_m=5.0,
        _stamp_ns=relay.ScanQosRelay._stamp_ns,
        get_clock=lambda: types.SimpleNamespace(now=lambda: now_obj),
        get_logger=lambda: types.SimpleNamespace(warning=warnings.append),
        publisher=types.SimpleNamespace(publish=published.append),
    )
    stale = message(9, 0)
    relay.ScanQosRelay._relay(node, stale)
    check("1초 오래된 scan을 현재 시각으로 교정",
          stale.header.stamp.sec == 10 and node._restamped_count == 1)
    check("교정한 scan은 폐기하지 않고 발행", published == [stale])
    check("운영자가 LDS_MODEL/시계를 확인할 경고를 남김",
          warnings and "LDS_MODEL" in warnings[0])

    duplicate = message(9, 0)
    relay.ScanQosRelay._relay(node, duplicate)
    check("원본 stamp가 같은 중복 scan은 차단",
          len(published) == 1 and node._dropped_stamp_count == 1)

    print("\n=== Nav2 scan 토픽 배선 ===")
    params = open(os.path.join(ROS_ROOT, "config", "nav2_params.yaml"),
                  encoding="utf-8").read()
    check("Nav2 공통 입력은 /scan_nav", "scan_topic: \"scan_nav\"" in params)
    for launch_name in ("simulation.launch.py", "digital_twin.launch.py"):
        launch = open(os.path.join(ROS_ROOT, "launch", launch_name),
                      encoding="utf-8").read()
        check(f"{launch_name}이 scan_qos_relay를 시작",
              "executable='scan_qos_relay'" in launch)
        check(f"{launch_name}이 /scan을 /scan_nav로 연결",
              "'input_topic': '/scan'" in launch
              and "'output_topic': '/scan_nav'" in launch)
        check(f"{launch_name}의 relay는 시뮬레이션 시간 사용",
              "'use_sim_time': True" in launch)
        check(f"{launch_name}이 Keepout 마스크 발행기를 시작",
              "executable='stall_monitor'" in launch)

    mapping_launch = open(
        os.path.join(ROS_ROOT, "launch", "real_cartographer.launch.py"),
        encoding="utf-8").read()
    check("실물 매핑 launch가 신뢰 거리를 relay에 전달",
          "'max_mapping_range_m': mapping_trust_range_m" in mapping_launch)

    print("\n모든 LiDAR 시각 검증 통과!")


if __name__ == "__main__":
    main()
