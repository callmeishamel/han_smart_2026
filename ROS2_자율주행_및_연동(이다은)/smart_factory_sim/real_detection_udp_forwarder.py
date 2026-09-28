#!/usr/bin/env python3
"""Forward stable real-map detections from ROS domain 30 to the twin UDP."""

import socket

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class RealDetectionUdpForwarder(Node):
    def __init__(self):
        super().__init__('real_detection_udp_forwarder')
        self.declare_parameter('input_topic', '/tracked_detections')
        self.declare_parameter('udp_host', '127.0.0.1')
        self.declare_parameter('udp_port', 9994)
        self.host = self.get_parameter('udp_host').value
        self.port = int(self.get_parameter('udp_port').value)
        self.received = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.create_subscription(
            String, self.get_parameter('input_topic').value,
            self._on_detections, 10)
        self.create_timer(5.0, self._warn_if_empty)
        self.get_logger().info(
            f'실물 지도 탐지 → Twin UDP {self.host}:{self.port}')

    def _on_detections(self, message):
        try:
            self.sock.sendto(message.data.encode('utf-8'),
                             (self.host, self.port))
            self.received = True
        except OSError as exc:
            self.get_logger().error(f'탐지 위치 UDP 전송 실패: {exc}')

    def _warn_if_empty(self):
        if not self.received:
            self.get_logger().warning(
                '/tracked_detections 대기 중입니다. A 관제 스택을 새 코드로 '
                '재시작했는지 확인하세요.')

    def destroy_node(self):
        self.sock.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = RealDetectionUdpForwarder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
