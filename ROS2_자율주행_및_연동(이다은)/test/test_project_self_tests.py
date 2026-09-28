"""Register the ROS/navigation offline checks with ``colcon test``.

The canonical self-test scripts live in the data-platform folder because they
exercise contracts across team boundaries.  Keeping a second copy here would
let the copies drift, so these pytest cases execute the canonical scripts.
"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


def _find_project_root():
    """Find the checkout whether pytest runs from source or a colcon build dir."""
    for parent in Path(__file__).resolve().parents:
        self_test_dir = parent / "데이터 플랫폼 및 대시보드(이상민)" / "self_test"
        if self_test_dir.is_dir():
            return parent, self_test_dir
    raise RuntimeError("저장소 루트와 공용 self_test 폴더를 찾지 못했습니다.")


PROJECT_ROOT, SELF_TEST_DIR = _find_project_root()


class ProjectSelfTests(unittest.TestCase):
    """Expose each canonical script as one ``colcon test`` test case."""

    def _run_script(self, script_name):
        script = SELF_TEST_DIR / script_name
        self.assertTrue(script.is_file(), f"검증 스크립트가 없습니다: {script}")

        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"{script_name} failed with exit code {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}",
        )

    def test_auto_mapper(self):
        self._run_script("test_auto_mapper.py")

    def test_integration_wiring(self):
        self._run_script("test_integration_wiring.py")

    def test_map_cleaner(self):
        self._run_script("test_map_cleaner.py")

    def test_minimap_logic(self):
        self._run_script("test_minimap_logic.py")

    def test_patrol_planner(self):
        self._run_script("test_patrol_planner.py")

    def test_person_marking(self):
        self._run_script("test_person_marking.py")

    def test_real_navigation_safety(self):
        self._run_script("test_real_navigation_safety.py")

    def test_scan_timing(self):
        self._run_script("test_scan_timing.py")

    def test_stall_monitor(self):
        self._run_script("test_stall_monitor.py")
