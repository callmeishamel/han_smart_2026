"""Cartographer용 TurtleBot3 odometry 위생 중계기.

일부 OpenCR/TurtleBot3 조합은 기동 직후 stamp=0인 /odom을 한 번 발행한다.
Cartographer에 /odom을 직접 연결하면 시간 역행으로 trajectory가 중단될 수 있어
기존 설정은 odometry를 모두 끌 수밖에 없었다. 이 노드는 0/non-increasing
stamp과 숫자가 깨진 pose만 버리고 정상 odometry를 /odom_filtered로 전달한다.
"""

import math
import time

import rclpy
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
# Humble에는 rclpy.exceptions.RCLError가 없고 pybind 모듈에만 있다.
# 다른 rclpy 배포판에서 이 내부 경로가 바뀌어도 정상 종료 처리는 계속 가능하게 둔다.
try:
    from rclpy._rclpy_pybind11 import RCLError
except ImportError:  # pragma: no cover - 배포판 호환 폴백
    RCLError = RuntimeError
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy


class OdomFilterRelay(Node):
    def __init__(self):
        super().__init__('odom_filter_relay')
        self.declare_parameter('input_topic', '/odom')
        self.declare_parameter('output_topic', '/odom_filtered')
        input_topic = str(self.get_parameter('input_topic').value)
        output_topic = str(self.get_parameter('output_topic').value)

        # BEST_EFFORT request는 OpenCR가 reliable/best-effort 중 어느 쪽으로
        # 발행해도 연결된다. Cartographer에 내보내는 출력은 reliable로 둔다.
        input_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=50,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )
        output_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=50,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.publisher = self.create_publisher(Odometry, output_topic, output_qos)
        self.subscription = self.create_subscription(
            Odometry, input_topic, self._relay, input_qos)
        self._last_stamp_ns = None
        self._dropped_count = 0
        self._last_drop_log_monotonic = 0.0
        self.get_logger().info(
            f'Odometry filter ready: {input_topic} -> {output_topic} '
            '(zero/non-increasing/invalid samples filtered)')

    @staticmethod
    def _stamp_ns(msg: Odometry) -> int:
        return int(msg.header.stamp.sec) * 1_000_000_000 + int(
            msg.header.stamp.nanosec)

    @staticmethod
    def _finite_pose(msg: Odometry) -> bool:
        position = msg.pose.pose.position
        orientation = msg.pose.pose.orientation
        twist_linear = msg.twist.twist.linear
        twist_angular = msg.twist.twist.angular
        values = (
            position.x, position.y, position.z,
            orientation.x, orientation.y, orientation.z, orientation.w,
            twist_linear.x, twist_linear.y, twist_linear.z,
            twist_angular.x, twist_angular.y, twist_angular.z,
        )
        return all(math.isfinite(float(value)) for value in values)

    def _drop(self, reason: str):
        self._dropped_count += 1
        now = time.monotonic()
        if now - self._last_drop_log_monotonic >= 5.0:
            self.get_logger().warning(
                f'Dropped odometry sample: {reason} '
                f'(total={self._dropped_count})')
            self._last_drop_log_monotonic = now

    def _relay(self, msg: Odometry):
        stamp_ns = self._stamp_ns(msg)
        if stamp_ns <= 0:
            self._drop('zero/negative timestamp')
            return
        if self._last_stamp_ns is not None and stamp_ns <= self._last_stamp_ns:
            self._drop(
                f'non-increasing timestamp {stamp_ns} <= {self._last_stamp_ns}')
            return
        if not self._finite_pose(msg):
            self._drop('non-finite pose/twist')
            return

        self._last_stamp_ns = stamp_ns
        self.publisher.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = OdomFilterRelay()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException, RCLError):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
