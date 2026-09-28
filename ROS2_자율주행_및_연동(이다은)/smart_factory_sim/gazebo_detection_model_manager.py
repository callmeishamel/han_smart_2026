#!/usr/bin/env python3
"""Create/remove Gazebo 3D models from stable real-world detections."""

import hashlib
import json
import math
import re
import socket
import time

from ament_index_python.packages import get_package_share_directory
from gazebo_msgs.msg import EntityState
from gazebo_msgs.srv import DeleteEntity, SetEntityState, SpawnEntity
import rclpy
from rclpy.node import Node


FIRE_LABELS = {'fire', 'flame', 'smoke fire', '화재'}
NO_HELMET_LABELS = {
    'person with no helmet', 'person without helmet', 'no safety helmet',
    'no_helmet', 'helmet_violation', '안전모 미착용',
}
WORKER_LABELS = {
    'person', 'human', 'worker', 'person wearing helmet', '작업자',
}
MODEL_BY_CATEGORY = {
    'fire': 'fire_event',
    'no_helmet': 'helmet_violation_event',
    'worker': 'worker_with_helmet',
}


def classify_detection(label):
    """Map NanoOWL/minimap labels to a visible twin model category."""
    normalized = str(label or '').strip().lower()
    if normalized in FIRE_LABELS:
        return 'fire'
    if normalized in NO_HELMET_LABELS:
        return 'no_helmet'
    if normalized in WORKER_LABELS:
        return 'worker'
    return None


def parse_detection_snapshot(raw):
    """Return normalized tracked detections keyed by stable model name."""
    try:
        payload = json.loads(raw.decode('utf-8'))
        session = str(payload.get('session', 'unknown'))
        detections = payload['detections']
        if not isinstance(detections, list):
            return None
    except (UnicodeDecodeError, TypeError, KeyError, json.JSONDecodeError):
        return None

    session_hash = hashlib.sha1(session.encode()).hexdigest()[:8]
    normalized = {}
    for detection in detections:
        try:
            category = classify_detection(detection['label'])
            x = float(detection['x'])
            y = float(detection['y'])
            tracker_id = re.sub(r'[^a-zA-Z0-9_-]', '_',
                                str(detection['id']))[:32]
        except (TypeError, KeyError, ValueError):
            continue
        if category is None or not tracker_id:
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        name = f'live_{category}_{session_hash}_{tracker_id}'
        normalized[name] = {'category': category, 'x': x, 'y': y}
    return normalized


class GazeboDetectionModelManager(Node):
    def __init__(self):
        super().__init__('gazebo_detection_model_manager')
        self.declare_parameter('udp_host', '127.0.0.1')
        self.declare_parameter('udp_port', 9994)
        self.declare_parameter('snapshot_timeout_seconds', 40.0)
        host = self.get_parameter('udp_host').value
        port = int(self.get_parameter('udp_port').value)
        self.snapshot_timeout = float(
            self.get_parameter('snapshot_timeout_seconds').value)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.desired = {}
        self.active = {}
        self.pending_spawns = set()
        self.received_snapshot = False
        self.last_snapshot_at = 0.0

        package = get_package_share_directory('smart_factory_sim')
        self.model_xml = {}
        for category, model_name in MODEL_BY_CATEGORY.items():
            path = f'{package}/models/{model_name}/model.sdf'
            with open(path, 'r', encoding='utf-8') as stream:
                self.model_xml[category] = stream.read()

        self.spawn_client = self.create_client(SpawnEntity, '/spawn_entity')
        self.delete_client = self.create_client(DeleteEntity, '/delete_entity')
        self.set_client = self.create_client(
            SetEntityState, '/gazebo/set_entity_state')
        self.create_timer(0.2, self._tick)
        self.get_logger().info(
            f'실물 화재/작업자 3D 모델 수신 대기: UDP {host}:{port}')

    def _read_snapshots(self):
        while True:
            try:
                raw, _address = self.sock.recvfrom(65535)
            except BlockingIOError:
                return
            snapshot = parse_detection_snapshot(raw)
            if snapshot is not None:
                self.desired = snapshot
                self.received_snapshot = True
                self.last_snapshot_at = time.monotonic()

    @staticmethod
    def _state(name, item):
        state = EntityState()
        state.name = name
        state.reference_frame = 'world'
        state.pose.position.x = item['x']
        state.pose.position.y = item['y']
        state.pose.position.z = 0.0
        state.pose.orientation.w = 1.0
        return state

    def _spawn(self, name, item):
        if not self.spawn_client.service_is_ready():
            return
        request = SpawnEntity.Request()
        request.name = name
        request.xml = self.model_xml[item['category']]
        request.robot_namespace = ''
        request.reference_frame = 'world'
        request.initial_pose = self._state(name, item).pose
        self.pending_spawns.add(name)
        future = self.spawn_client.call_async(request)

        def finished(result_future):
            self.pending_spawns.discard(name)
            try:
                response = result_future.result()
            except Exception as exc:
                self.get_logger().error(f'{name} 생성 호출 실패: {exc}')
                return
            if response.success:
                self.active[name] = dict(item)
                self.get_logger().info(
                    f"3D 생성: {item['category']} ({item['x']:.2f}, "
                    f"{item['y']:.2f})")
            else:
                self.get_logger().warning(
                    f'{name} 생성 거부: {response.status_message}')

        future.add_done_callback(finished)

    def _delete(self, name):
        if not self.delete_client.service_is_ready():
            return
        request = DeleteEntity.Request()
        request.name = name
        self.delete_client.call_async(request)
        self.active.pop(name, None)
        self.pending_spawns.discard(name)
        self.get_logger().info(f'3D 제거: {name}')

    def _move(self, name, item):
        if not self.set_client.service_is_ready():
            return
        request = SetEntityState.Request()
        request.state = self._state(name, item)
        self.set_client.call_async(request)
        self.active[name] = dict(item)

    def _tick(self):
        self._read_snapshots()
        if not self.received_snapshot:
            return
        if time.monotonic() - self.last_snapshot_at > self.snapshot_timeout:
            self.desired = {}

        for name in set(self.active) - set(self.desired):
            self._delete(name)
        for name, item in self.desired.items():
            if name in self.active:
                old = self.active[name]
                if math.hypot(old['x'] - item['x'],
                              old['y'] - item['y']) > 0.02:
                    self._move(name, item)
            elif name not in self.pending_spawns:
                self._spawn(name, item)

    def destroy_node(self):
        self.sock.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = GazeboDetectionModelManager()
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
