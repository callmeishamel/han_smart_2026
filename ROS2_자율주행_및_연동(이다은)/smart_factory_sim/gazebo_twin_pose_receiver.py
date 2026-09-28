#!/usr/bin/env python3
"""Apply real AMCL poses to the Gazebo twin robot in ROS domain 31."""

import json
import math
import socket
import time

from gazebo_msgs.msg import EntityState
from gazebo_msgs.srv import SetEntityState
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
import rclpy
from rclpy.node import Node


def quaternion_from_yaw(yaw):
    """Return the planar ROS quaternion for yaw in radians."""
    return 0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def parse_pose_packet(raw):
    """Validate a forwarder datagram and return (x, y, yaw), or None."""
    try:
        packet = json.loads(raw.decode('utf-8'))
        values = (float(packet['x']), float(packet['y']), float(packet['yaw']))
    except (UnicodeDecodeError, TypeError, ValueError, KeyError,
            json.JSONDecodeError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


class GazeboTwinPoseReceiver(Node):
    def __init__(self):
        super().__init__('gazebo_twin_pose_receiver')
        self.declare_parameter('udp_host', '127.0.0.1')
        self.declare_parameter('udp_port', 9995)
        self.declare_parameter('model_name', 'turtlebot3_burger')
        self.declare_parameter('model_z', 0.01)
        self.declare_parameter('pose_timeout_seconds', 2.0)
        host = self.get_parameter('udp_host').value
        port = int(self.get_parameter('udp_port').value)
        self.model_name = self.get_parameter('model_name').value
        self.model_z = float(self.get_parameter('model_z').value)
        self.timeout = float(self.get_parameter('pose_timeout_seconds').value)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.latest_pose = None
        self.latest_at = 0.0
        self.pending = None
        # SetModelState is retained for ROS 1 bridge compatibility and can
        # hang on Gazebo Classic/Humble.  SetEntityState is its native ROS 2
        # replacement and returns a success response from this Gazebo build.
        self.client = self.create_client(
            SetEntityState, '/gazebo/set_entity_state')
        self.pose_publisher = self.create_publisher(PoseWithCovarianceStamped,
                                                    '/amcl_pose', 10)
        self.stop_publisher = self.create_publisher(Twist, '/cmd_vel', 10)
        # Wall time is intentional: mirroring must keep working when Gazebo is
        # paused or its /clock has not yet started.
        self.create_timer(0.05, self._tick)
        self.get_logger().info(
            f'UDP {host}:{port} 실물 위치를 Gazebo 모델 {self.model_name}에 반영합니다.')

    def _read_packets(self):
        while True:
            try:
                raw, _address = self.sock.recvfrom(4096)
            except BlockingIOError:
                return
            pose = parse_pose_packet(raw)
            if pose is not None:
                self.latest_pose = pose
                self.latest_at = time.monotonic()

    def _publish_twin_amcl_pose(self, x, y, yaw):
        message = PoseWithCovarianceStamped()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = 'map'
        message.pose.pose.position.x = x
        message.pose.pose.position.y = y
        qx, qy, qz, qw = quaternion_from_yaw(yaw)
        message.pose.pose.orientation.x = qx
        message.pose.pose.orientation.y = qy
        message.pose.pose.orientation.z = qz
        message.pose.pose.orientation.w = qw
        # Position mirror, not a new localization estimate.  Small covariance
        # makes the minimap display this as the authoritative twin pose.
        message.pose.covariance[0] = 0.001
        message.pose.covariance[7] = 0.001
        message.pose.covariance[35] = 0.001
        self.pose_publisher.publish(message)

    def _tick(self):
        # This publisher only exists in isolated twin domain 31.  It prevents
        # stale patrol/teleop commands from moving the model independently of
        # the real robot while live mirroring is active.
        self.stop_publisher.publish(Twist())
        self._read_packets()
        pose_is_stale = time.monotonic() - self.latest_at > self.timeout
        if self.latest_pose is None or pose_is_stale:
            return
        if self.pending is not None and not self.pending.done():
            return
        if not self.client.service_is_ready():
            self.client.wait_for_service(timeout_sec=0.0)
            return

        x, y, yaw = self.latest_pose
        self._publish_twin_amcl_pose(x, y, yaw)
        request = SetEntityState.Request()
        state = EntityState()
        state.name = self.model_name
        state.reference_frame = 'world'
        state.pose.position.x = x
        state.pose.position.y = y
        state.pose.position.z = self.model_z
        qx, qy, qz, qw = quaternion_from_yaw(yaw)
        state.pose.orientation.x = qx
        state.pose.orientation.y = qy
        state.pose.orientation.z = qz
        state.pose.orientation.w = qw
        request.state = state
        self.pending = self.client.call_async(request)

    def destroy_node(self):
        self.sock.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = GazeboTwinPoseReceiver()
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
