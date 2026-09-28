"""Best-effort LiDAR scan을 Cartographer/Nav2용 정상 scan으로 중계한다.

LDS-03 드라이버는 SensorData QoS(best-effort)로 /scan을 발행하지만, 일부 Humble
Cartographer 빌드는 /scan을 reliable로만 구독한다. ROS 2는 이 둘을 연결하지 않으므로
중계 노드가 입력은 best-effort, 출력은 reliable로 명시해 전달한다.

실물 LDS-03에서 동일한 header stamp가 두 번 들어오는 경우가 있다. Cartographer는
이를 ``Ignored subdivision ... previous subdivision time``으로 버리는데, 중복
스캔이 연속되면 회전 중 scan matching이 불안정해진다. 이 노드에서 0 시각과
시간이 역행하는 스캔을 Cartographer에 도달하기 전에 차단한다.

일부 LDS-03 드라이버/네트워크 조합은 수신 시각보다 약 1초 오래된
header stamp를 내보낸다. AMCL은 이를 TF 캐시보다 오래됐다고 모두 버리고,
Nav2는 위치가 멈춘 채 복구 회전만 반복한다. ``restamp_stale``를 켜면
이 경우만 현재 ROS 시각으로 교정한다. 기본값은 꺼짐이고, 실물 launch가
명시적으로 켠다.

실물 매핑에서는 신뢰 거리 밖의 유한 반환점을 ``NaN``으로 바꾼다. 거리를
단순히 짧게 잘라 내면 Cartographer가 그 방향을 missing ray(자유 공간)로
쓸 수 있다. 반면 ``NaN`` 빔은 점유 추가와 자유 공간 삭제 모두에서 제외되어,
멀리서 튀는 반사값이 이미 그린 벽을 지우고 더 먼 벽을 만드는 현상을 막는다.
"""

import math
import time

import rclpy
from rclpy.executors import ExternalShutdownException
# Humble에는 rclpy.exceptions.RCLError가 없고 pybind 모듈에만 있다.
# 다른 rclpy 배포판에서 이 내부 경로가 바뀌어도 정상 종료 처리는 계속 가능하게 둔다.
try:
    from rclpy._rclpy_pybind11 import RCLError
except ImportError:  # pragma: no cover - 배포판 호환 폴백
    RCLError = RuntimeError
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan


def scan_stamp_status(now_ns: int, stamp_ns: int,
                      max_age_sec: float, max_future_sec: float) -> str:
    """스캔 header 시각을 ``ok/stale/future/invalid``로 분류한다."""
    if stamp_ns <= 0:
        return 'invalid'
    age_sec = (int(now_ns) - int(stamp_ns)) / 1_000_000_000.0
    if age_sec > float(max_age_sec):
        return 'stale'
    if age_sec < -float(max_future_sec):
        return 'future'
    return 'ok'


def mask_ranges_beyond(ranges, max_mapping_range_m: float) -> int:
    """매핑 신뢰 거리 밖의 반환점을 무효화하고 개수를 반환한다.

    ``inf``/``NaN``은 이미 LaserScan의 미반환값이므로 그대로 둔다.
    0 이하의 임계값은 필터 비활성화로 취급한다.
    """
    limit = float(max_mapping_range_m)
    if not math.isfinite(limit) or limit <= 0.0:
        return 0

    masked = 0
    for index, distance in enumerate(ranges):
        if math.isfinite(distance) and distance > limit:
            ranges[index] = math.nan
            masked += 1
    return masked


class ScanQosRelay(Node):
    def __init__(self):
        super().__init__('scan_qos_relay')
        self.declare_parameter('input_topic', '/scan')
        self.declare_parameter('output_topic', '/scan_reliable')
        self.declare_parameter('queue_depth', 1)
        self.declare_parameter('restamp_stale', False)
        self.declare_parameter('max_stamp_age_sec', 0.35)
        self.declare_parameter('max_future_sec', 0.10)
        self.declare_parameter('max_mapping_range_m', 0.0)
        input_topic = self.get_parameter('input_topic').value
        output_topic = self.get_parameter('output_topic').value
        queue_depth = max(1, int(self.get_parameter('queue_depth').value))
        self.restamp_stale = bool(self.get_parameter('restamp_stale').value)
        self.max_stamp_age_sec = max(
            0.01, float(self.get_parameter('max_stamp_age_sec').value))
        self.max_future_sec = max(
            0.01, float(self.get_parameter('max_future_sec').value))
        self.max_mapping_range_m = float(
            self.get_parameter('max_mapping_range_m').value)
        if not math.isfinite(self.max_mapping_range_m):
            raise ValueError('max_mapping_range_m must be finite')
        self.max_mapping_range_m = max(0.0, self.max_mapping_range_m)

        sensor_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=queue_depth,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )
        reliable_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=queue_depth,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.publisher = self.create_publisher(
            LaserScan, output_topic, reliable_qos)
        self.subscription = self.create_subscription(
            LaserScan, input_topic, self._relay, sensor_qos)
        self._last_stamp_ns = None
        self._dropped_stamp_count = 0
        self._last_drop_log_monotonic = 0.0
        self._restamped_count = 0
        self._last_restamp_log_monotonic = 0.0
        self.get_logger().info(
            f'LiDAR QoS relay ready: {input_topic} (best_effort) -> '
            f'{output_topic} (reliable, depth={queue_depth}, '
            f'restamp_stale={self.restamp_stale}, invalid/duplicate filtered, '
            f'mapping_range_limit={self.max_mapping_range_m or "off"}m)')

    @staticmethod
    def _stamp_ns(msg: LaserScan) -> int:
        return int(msg.header.stamp.sec) * 1_000_000_000 + int(
            msg.header.stamp.nanosec)

    def _relay(self, msg: LaserScan):
        stamp_ns = self._stamp_ns(msg)
        if stamp_ns <= 0 or (
                self._last_stamp_ns is not None
                and stamp_ns <= self._last_stamp_ns):
            self._dropped_stamp_count += 1
            now = time.monotonic()
            if now - self._last_drop_log_monotonic >= 5.0:
                self.get_logger().warning(
                    'Dropped invalid/non-increasing LiDAR stamp '
                    f'(stamp_ns={stamp_ns}, last={self._last_stamp_ns}, '
                    f'total={self._dropped_stamp_count})')
                self._last_drop_log_monotonic = now
            return

        self._last_stamp_ns = stamp_ns
        now_ros = self.get_clock().now()
        timing = scan_stamp_status(
            now_ros.nanoseconds, stamp_ns,
            self.max_stamp_age_sec, self.max_future_sec)
        if self.restamp_stale and timing in {'stale', 'future'}:
            age_sec = (now_ros.nanoseconds - stamp_ns) / 1_000_000_000.0
            msg.header.stamp = now_ros.to_msg()
            self._restamped_count += 1
            now_monotonic = time.monotonic()
            if now_monotonic - self._last_restamp_log_monotonic >= 5.0:
                self.get_logger().warning(
                    f'Restamped {timing} LiDAR data for TF alignment '
                    f'(age={age_sec:+.3f}s, total={self._restamped_count}). '
                    'Check the Jetson LDS_MODEL and system clock if this '
                    'persists.')
                self._last_restamp_log_monotonic = now_monotonic
        mask_ranges_beyond(msg.ranges, self.max_mapping_range_m)
        self.publisher.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = ScanQosRelay()
    try:
        rclpy.spin(node)
    # launch 종료가 rclpy.spin()의 wait set을 먼저 무효화하는 Humble 조합에서는
    # ExternalShutdownException 대신 RCLError가 올라온다. 정상 종료 잡음으로 처리한다.
    except (KeyboardInterrupt, ExternalShutdownException, RCLError):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
