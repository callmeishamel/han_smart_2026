import importlib.util
import json
from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / "smart_factory_sim"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, PACKAGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FORWARD = load_module("real_pose_udp_forwarder")
RECEIVER = load_module("gazebo_twin_pose_receiver")


class LiveTwinPoseBridgeTests(unittest.TestCase):
    def test_quaternion_yaw_round_trip(self):
        expected = -1.234
        qx, qy, qz, qw = RECEIVER.quaternion_from_yaw(expected)
        actual = FORWARD.yaw_from_quaternion(qx, qy, qz, qw)
        self.assertAlmostEqual(actual, expected)

    def test_pose_packet_requires_finite_coordinates(self):
        packet = json.dumps({'x': 1.25, 'y': -0.4, 'yaw': 0.5}).encode()
        self.assertEqual(RECEIVER.parse_pose_packet(packet), (1.25, -0.4, 0.5))
        non_finite = b'{"x":"nan","y":0,"yaw":0}'
        self.assertIsNone(RECEIVER.parse_pose_packet(non_finite))
        self.assertIsNone(RECEIVER.parse_pose_packet(b'not json'))

    def test_pose_packet_rejects_missing_coordinates(self):
        packet = json.dumps({'x': 1.0, 'y': 2.0}).encode()
        self.assertIsNone(RECEIVER.parse_pose_packet(packet))


if __name__ == "__main__":
    unittest.main()
