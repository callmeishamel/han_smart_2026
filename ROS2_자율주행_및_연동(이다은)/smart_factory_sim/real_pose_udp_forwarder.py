#!/usr/bin/env python3
"""Forward the real robot's map TF pose from ROS domain 30 to local UDP.

ROS 2 domains intentionally isolate the real robot and Gazebo twin.  This
small one-way bridge is the explicit, auditable connection between them.  It
only reads the ``map -> base_footprint`` transform and transmits x/y/yaw to
localhost; it cannot send a velocity or other command to the real robot.
"""

import json
import math
import socket
import time

import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener


def yaw_from_quaternion(x, y, z, w):
    """Return yaw in radians from a ROS quaternion."""
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class RealPoseUdpForwarder(Node):
    def __init__(self):
        super().__init__('real_pose_udp_forwarder')
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('robot_frame', 'base_footprint')
        self.declare_parameter('udp_host', '127.0.0.1')
        self.declare_parameter('udp_port', 9995)
        self.declare_parameter('max_rate_hz', 15.0)
        self.host = self.get_parameter('udp_host').value
        self.port = int(self.get_parameter('udp_port').value)
        rate = float(self.get_parameter('max_rate_hz').value)
        self.min_period = 1.0 / max(rate, 0.1)
        self.map_frame = self.get_parameter('map_frame').value
        self.robot_frame = self.get_parameter('robot_frame').value
        self.last_wait_log = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.create_timer(self.min_period, self._send_pose)
        self.get_logger().info(
            f'실물 TF {self.map_frame}→{self.robot_frame} → '
            f'UDP {self.host}:{self.port} ({rate:.1f}Hz)')

    def _send_pose(self):
        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame, self.robot_frame, Time(),
                timeout=Duration(seconds=0.03))
        except TransformException:
            now = time.monotonic()
            if now - self.last_wait_log >= 5.0:
                self.get_logger().warning(
                    f'실물 TF {self.map_frame}→{self.robot_frame} 대기 중입니다.')
                self.last_wait_log = now
            return

        translation = transform.transform.translation
        rotation = transform.transform.rotation
        yaw = yaw_from_quaternion(rotation.x, rotation.y,
                                  rotation.z, rotation.w)
        values = (translation.x, translation.y, yaw)
        if not all(math.isfinite(value) for value in values):
            self.get_logger().warning('유효하지 않은 실물 위치를 건너뜁니다.')
            return
        packet = {
            'source': 'real_map_tf',
            'sent_at': time.time(),
            'x': translation.x,
            'y': translation.y,
            'yaw': yaw,
        }
        try:
            payload = json.dumps(packet, separators=(',', ':')).encode()
            self.sock.sendto(payload, (self.host, self.port))
        except OSError as exc:
            self.get_logger().error(f'실물 위치 UDP 전송 실패: {exc}')

    def destroy_node(self):
        self.sock.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = RealPoseUdpForwarder()
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
