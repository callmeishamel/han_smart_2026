import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "maps" / "map_to_gazebo_world.py"
)
SPEC = importlib.util.spec_from_file_location("map_to_gazebo_world", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MapToGazeboWorldTests(unittest.TestCase):
    def test_pgm_row_is_converted_to_ros_map_y(self):
        # PGM row zero is the top of the image; ROS map origin is bottom-left.
        x, y = MODULE.cell_center(
            (0, 0), width=4, height=3, resolution=0.5,
            origin_x=-1.0, origin_y=-2.0,
        )
        self.assertAlmostEqual(x, -0.75)
        self.assertAlmostEqual(y, -0.75)

    def test_component_filter_and_lossless_rectangles(self):
        occupied = {(0, 0), (0, 1), (1, 0), (4, 4)}
        filtered = MODULE.filter_components(occupied, 2)
        self.assertEqual(filtered, {(0, 0), (0, 1), (1, 0)})
        rectangles = MODULE.cells_to_rectangles(filtered)
        rebuilt = set()
        for row0, row1, col0, col1 in rectangles:
            for row in range(row0, row1 + 1):
                for col in range(col0, col1 + 1):
                    rebuilt.add((row, col))
        self.assertEqual(rebuilt, filtered)

    def test_build_writes_parseable_world_model_mesh_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pgm = root / "test_map.pgm"
            yaml = root / "test_map.yaml"
            # 9x9 known-free room, a three-cell wall, and one isolated speck.
            pixels = [254] * 81
            for row, col in ((3, 3), (3, 4), (3, 5), (7, 7)):
                pixels[row * 9 + col] = 0
            pgm.write_bytes(b"P5\n9 9\n255\n" + bytes(pixels))
            yaml.write_text(
                "image: test_map.pgm\n"
                "mode: trinary\n"
                "resolution: 0.1\n"
                "origin: [-0.2, -0.3, 0.0]\n"
                "negate: 0\n"
                "occupied_thresh: 0.65\n"
                "free_thresh: 0.196\n",
                encoding="utf-8",
            )

            model_dir = root / "models" / "factory_map_twin"
            world = root / "worlds" / "factory_digital_twin.world"
            metadata = root / "config" / "factory_digital_twin.json"
            result = MODULE.build(yaml, model_dir, world, metadata)

            self.assertEqual(result["occupied_cells_source"], 4)
            self.assertEqual(result["occupied_cells_used"], 3)
            self.assertEqual(result["filtered_noise_cells"], 1)
            self.assertTrue((model_dir / "meshes" / "walls.obj").is_file())
            mesh_text = (
                model_dir / "meshes" / "walls.obj"
            ).read_text(encoding="utf-8")
            self.assertIn("v ", mesh_text)
            self.assertEqual(ET.parse(world).getroot().tag, "sdf")
            world_text = world.read_text(encoding="utf-8")
            self.assertNotIn("fire_event_target", world_text)
            self.assertNotIn("helmet_violation_event_target", world_text)
            model_root = ET.parse(model_dir / "model.sdf").getroot()
            self.assertEqual(model_root.tag, "sdf")

            stored = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(stored["source_map"], "test_map.yaml")
            self.assertEqual(stored["map_yaml"], str(yaml.resolve()))
            self.assertIn("spawn", stored)
            self.assertIn("waypoints", stored)

    def test_checked_in_factory_map_classification(self):
        package_dir = Path(__file__).resolve().parents[1]
        config = MODULE.load_map_yaml(
            package_dir / "maps" / "factory_map.yaml")
        width, height, pixels = MODULE.read_pgm(
            package_dir / "maps" / str(config["image"]))
        occupied, free, unknown = MODULE.classify_cells(
            pixels, width, height,
            negate=int(config["negate"]),
            occupied_thresh=float(config["occupied_thresh"]),
            free_thresh=float(config["free_thresh"]),
        )
        self.assertEqual((width, height), (100, 106))
        self.assertEqual(len(occupied), 90)
        self.assertEqual(len(free), 1711)
        self.assertEqual(len(unknown), 8799)


if __name__ == "__main__":
    unittest.main()
