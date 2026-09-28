import importlib.util
import json
from pathlib import Path
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "smart_factory_sim" / "gazebo_detection_model_manager.py"
)
SPEC = importlib.util.spec_from_file_location(
    "gazebo_detection_model_manager", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GazeboDetectionModelTests(unittest.TestCase):
    def test_distinguishes_fire_worker_and_no_helmet(self):
        self.assertEqual(MODULE.classify_detection("fire"), "fire")
        self.assertEqual(MODULE.classify_detection("person"), "worker")
        self.assertEqual(
            MODULE.classify_detection("person with no helmet"), "no_helmet")
        self.assertIsNone(MODULE.classify_detection("vehicle"))

    def test_snapshot_supports_multiple_tracked_people(self):
        payload = {
            "session": "real-session",
            "detections": [
                {"id": 1, "label": "fire", "x": 1.0, "y": 2.0},
                {"id": 2, "label": "person", "x": 2.0, "y": 3.0},
                {"id": 3, "label": "person with no helmet",
                 "x": 3.0, "y": 4.0},
            ],
        }
        result = MODULE.parse_detection_snapshot(json.dumps(payload).encode())
        self.assertEqual(len(result), 3)
        self.assertEqual(
            {item["category"] for item in result.values()},
            {"fire", "worker", "no_helmet"},
        )

    def test_snapshot_rejects_invalid_coordinates(self):
        payload = {
            "session": "s",
            "detections": [
                {"id": 1, "label": "fire", "x": "nan", "y": 2.0},
            ],
        }
        result = MODULE.parse_detection_snapshot(json.dumps(payload).encode())
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
