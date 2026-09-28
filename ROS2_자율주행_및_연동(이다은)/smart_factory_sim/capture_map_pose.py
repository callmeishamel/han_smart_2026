#!/usr/bin/env python3
"""현재 map 좌표계의 로봇 자세를 원자적으로 저장한다.

출력 파일 첫 줄은 대시보드 실행기가 바로 읽을 수 있는 ``x y yaw`` 세 실수다.
Cartographer가 살아 있는 동안 map -> base_footprint TF를 조회해야 한다.
"""

import argparse
import math
import os
from pathlib import Path
import time

import rclpy
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener


def quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(sin_yaw, cos_yaw)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', help='저장할 .pose 파일')
    parser.add_argument('--timeout', type=float, default=8.0,
                        help='TF를 기다릴 최대 시간(초)')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rclpy.init(args=[])
    node = Node('capture_map_pose')
    tf_buffer = Buffer()
    _listener = TransformListener(tf_buffer, node)
    deadline = time.monotonic() + args.timeout
    last_error = 'transform not received'
    stable_since = None
    previous_pose = None

    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            try:
                transform = tf_buffer.lookup_transform(
                    'map', 'base_footprint', Time())
            except TransformException as exc:
                last_error = str(exc)
                continue

            translation = transform.transform.translation
            rotation = transform.transform.rotation
            yaw = quaternion_to_yaw(
                rotation.x, rotation.y, rotation.z, rotation.w)

            values = (translation.x, translation.y, yaw)
            if not all(math.isfinite(value) for value in values):
                last_error = 'transform contained a non-finite value'
                stable_since = None
                previous_pose = None
                continue

            if previous_pose is None:
                stable_since = time.monotonic()
            else:
                dx = translation.x - previous_pose[0]
                dy = translation.y - previous_pose[1]
                dyaw = math.atan2(
                    math.sin(yaw - previous_pose[2]),
                    math.cos(yaw - previous_pose[2]))
                if math.hypot(dx, dy) > 0.02 or abs(dyaw) > math.radians(3.0):
                    stable_since = time.monotonic()
            previous_pose = values

            # 새 TF listener의 첫 샘플을 바로 쓰지 않고 0.8초간 자세가 안정적인지 본다.
            if time.monotonic() - stable_since < 0.8:
                continue

            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f'.{output.name}.{os.getpid()}.tmp')
            temporary.write_text(
                f'{translation.x:.9f} {translation.y:.9f} {yaw:.9f}\n',
                encoding='utf-8')
            os.replace(temporary, output)
            node.get_logger().info(
                f'Final map pose saved: x={translation.x:.3f}, '
                f'y={translation.y:.3f}, yaw={yaw:.3f} -> {output}')
            return 0

        node.get_logger().error(
            f'Could not capture map -> base_footprint within '
            f'{args.timeout:.1f}s: {last_error}')
        return 1
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
