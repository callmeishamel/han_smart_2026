#!/usr/bin/env python3
"""Gazebo mock 이벤트를 대시보드/RAG UDP 계약으로 전달한다.

event_detector가 만든 /detected_events는 ROS2 웹 대시보드와 RViz에 쓰는
지도 좌표 이벤트다. 데이터 플랫폼의 기존 파이프라인은 Jetson NanoOWL의
UDP 형식(9998)을 받으므로, 이 노드가 두 경로를 연결한다. 실물 Jetson을
대체하는 용도만이며, Jetson 코드나 실제 UDP 수신 형식을 바꾸지 않는다.
"""

import json
import socket
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class MockEventBridge(Node):
    """/detected_events의 새 mock 이벤트를 NanoOWL UDP payload로 변환한다."""

    def __init__(self):
        super().__init__('mock_event_bridge')
        self.declare_parameter('udp_host', '127.0.0.1')
        self.declare_parameter('udp_port', 9998)
        self.declare_parameter('resend_seconds', 30.0)

        self.udp_host = self.get_parameter('udp_host').value
        self.udp_port = int(self.get_parameter('udp_port').value)
        self.resend_seconds = float(self.get_parameter('resend_seconds').value)
        self.last_sent = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.create_subscription(String, '/detected_events', self._on_event, 10)
        self.get_logger().info(
            f'Gazebo mock 이벤트를 UDP {self.udp_host}:{self.udp_port}로 전달합니다.')

    def _on_event(self, msg):
        try:
            event = json.loads(msg.data)
            event_id = str(event['id'])
            label = str(event['label'])
            distance = float(event.get('distance_meter', -1.0))
            map_x = float(event['x'])
            map_y = float(event['y'])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning('형식이 잘못된 /detected_events 메시지를 건너뜁니다.')
            return

        now = time.monotonic()
        if now - self.last_sent.get(event_id, float('-inf')) < self.resend_seconds:
            return

        # build_detection_event()가 요구하는 object/bbox/distance_meter를 맞춘다.
        # map_x/map_y/source는 기존 수신기가 무시해도 안전한 확장 필드다.
        payload = {
            'timestamp': time.time(),
            'source': 'gazebo_mock',
            'detections': [{
                'object': label,
                'bbox': [0, 0, 1, 1],
                'distance_meter': distance,
                'map_x': map_x,
                'map_y': map_y,
                'event_id': event_id,
            }],
        }
        try:
            self.sock.sendto(json.dumps(payload).encode('utf-8'),
                             (self.udp_host, self.udp_port))
            self.last_sent[event_id] = now
            self.get_logger().info(
                f'UDP 전달: {label} ({distance:.2f}m, map {map_x:.2f},{map_y:.2f})')
        except OSError as exc:
            self.get_logger().error(f'UDP 전달 실패: {exc}')

    def destroy_node(self):
        self.sock.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = MockEventBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception:
                pass


if __name__ == '__main__':
    main()
