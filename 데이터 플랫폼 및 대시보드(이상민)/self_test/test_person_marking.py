r"""
self_test/test_person_marking.py

"젯슨이 잡은 person 이 대시보드 SLAM 미니맵에 작업자로 찍히는가"를 끝에서 끝까지
확인하는 자체 테스트. 이 경로는 세 곳에서 각각 조용히 끊길 수 있어서, 한 군데만
고쳐 놓으면 증상이 그대로 남습니다.

  1. 젯슨       ai_inference_sender.py   - 겹친 탐지 정리에서 person 이 지워지는가
  2. 다은 노트북 minimap_renderer.py      - 레이캐스팅이 사람을 못 찾고 버리는가
  3. 상민 PC    common/schema.py         - 라벨 -> "작업자" 변환이 있는가

1번은 cv2/nanoowl 이 필요하고 2번은 rclpy 가 필요해서, 둘 다 실제 소스를 읽되
무거운 의존성 없이 검증합니다 (1번은 AST 로 순수 함수만 떼어내 실행, 2번은
test_minimap_logic.py 와 같은 방식으로 스텁을 심어 import).

실행 방법 (프로젝트 루트에서):
    python self_test\test_person_marking.py
"""

import ast
import os
import sys
import time
import types

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import build_map_detection_event  # noqa: E402

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_SENDER = os.path.join(_REPO_ROOT, "smart_factory_project", "ai_inference_sender.py")
_MINIMAP = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                        "dashboard_link", "minimap_renderer.py")


def check(desc, condition, detail=""):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def _require(path, who):
    if os.path.exists(path):
        return
    print(f"[중단] {who} 소스를 찾지 못했습니다:\n       {path}")
    print("       이 테스트는 저장소를 통째로 받아야 돌아갑니다.")
    raise SystemExit(1)


# ----------------------------------------------------------------------
# 1. 젯슨 - 겹친 탐지 정리
# ----------------------------------------------------------------------
def load_sender_dedup():
    """ai_inference_sender.py 에서 순수 계산 함수/상수만 떼어내 실행합니다.

    모듈을 그대로 import 하면 cv2 · nanoowl · PIL 이 필요합니다. 그건 젯슨에만
    있으므로, AST 로 최상위 함수 정의와 상수 대입만 골라 실행합니다. 사본을 두는
    대신 **실제로 젯슨에 올라가는 파일 그 자체**를 읽습니다.
    """
    _require(_SENDER, "젯슨 ai_inference_sender.py")
    with open(_SENDER, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    wanted_fn = {
        "box_iou", "_label_name", "suppress_overlapping_detections",
        "helmet_in_head_region", "apply_helmet_compliance",
    }
    wanted_class = {"HelmetComplianceTracker"}
    wanted_const = {
        "DEDUP_IOU_THRESHOLD", "PERSON_LABEL", "HELMET_LABEL",
        "HELMET_VIOLATION_LABEL", "PPE_NO_HELMET_CONFIRM_SEC",
        "PPE_HEAD_REGION_RATIO", "PPE_TRACK_IOU_THRESHOLD",
        "PPE_TRACK_STALE_SEC",
    }

    kept = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_fn:
            kept.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in wanted_class:
            kept.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in wanted_const for t in node.targets):
            kept.append(node)

    found_fn = {n.name for n in kept if isinstance(n, ast.FunctionDef)}
    missing = wanted_fn - found_fn
    check("젯슨 소스에서 중복 정리 함수를 모두 찾음", not missing, f"없음: {missing}")

    module = ast.Module(body=kept, type_ignores=[])
    namespace = {"np": np, "os": os, "time": time}
    exec(compile(module, _SENDER, "exec"), namespace)
    return namespace


def test_sender_dedup(ns):
    print("=== 1. 젯슨 - 사람/안전모 4초 판정 ===")
    suppress = ns["suppress_overlapping_detections"]
    apply_ppe = ns["apply_helmet_compliance"]
    Tracker = ns["HelmetComplianceTracker"]
    prompts = ["person", "safety helmet", "fire", "vehicle"]

    person = np.array([100.0, 100.0, 200.0, 400.0])
    duplicate_person = np.array([102.0, 98.0, 198.0, 402.0])
    helmet = np.array([130.0, 90.0, 170.0, 150.0])

    boxes, labels = suppress(
        np.array([person, duplicate_person]), np.array([0, 0]),
        np.array([0.7, 0.5]), prompts)
    check("같은 person 박스는 하나로 줄인다", len(boxes) == 1)

    boxes, labels = suppress(
        np.array([person, helmet]), np.array([0, 1]),
        np.array([0.7, 0.6]), prompts)
    check("person과 safety helmet은 겹쳐도 둘 다 유지", len(boxes) == 2)

    tracker = Tracker(confirm_sec=4.0, stale_sec=10.0)
    no_helmet_input = np.array([person])
    person_label = np.array([0])
    boxes, names = apply_ppe(
        no_helmet_input, person_label, prompts, tracker, now=10.0)
    check("처음 4초 동안은 person으로 표시", names == ["person"])

    boxes, names = apply_ppe(
        no_helmet_input, person_label, prompts, tracker, now=13.9)
    check("3.9초에는 아직 person", names == ["person"])

    boxes, names = apply_ppe(
        no_helmet_input, person_label, prompts, tracker, now=14.0)
    check("4초 연속 안전모가 없으면 미착용으로 전환",
          names == ["person with no helmet"])

    boxes, names = apply_ppe(
        np.array([person, helmet]), np.array([0, 1]), prompts,
        tracker, now=14.1)
    check("머리 영역에 안전모가 보이면 즉시 person으로 복귀",
          names == ["person"])
    check("safety helmet 박스 자체는 출력/전송하지 않음",
          len(boxes) == 1)

    boxes, names = apply_ppe(
        no_helmet_input, person_label, prompts, tracker, now=14.2)
    check("안전모가 사라지면 4초 타이머를 새로 시작",
          names == ["person"])
    print()


# ----------------------------------------------------------------------
# 2. 다은 노트북 - 레이캐스팅
# ----------------------------------------------------------------------
def load_minimap():
    """test_minimap_logic.py 와 같은 스텁 방식으로 minimap_renderer.py 를 읽습니다."""
    _require(_MINIMAP, "미니맵 minimap_renderer.py")

    for name in ["rclpy", "rclpy.node", "rclpy.qos", "nav_msgs", "nav_msgs.msg",
                 "sensor_msgs", "sensor_msgs.msg", "std_msgs", "std_msgs.msg",
                 "geometry_msgs", "geometry_msgs.msg", "flask",
                 "tf2_ros", "tf2_ros.buffer", "tf2_ros.transform_listener"]:
        sys.modules.setdefault(name, types.ModuleType(name))

    sys.modules["rclpy.node"].Node = object
    sys.modules["nav_msgs.msg"].OccupancyGrid = object
    sys.modules["nav_msgs.msg"].Odometry = object
    sys.modules["sensor_msgs.msg"].LaserScan = object
    sys.modules["sensor_msgs.msg"].BatteryState = object
    sys.modules["std_msgs.msg"].String = object
    sys.modules["geometry_msgs.msg"].PoseWithCovarianceStamped = object
    sys.modules["geometry_msgs.msg"].PoseStamped = object
    sys.modules["rclpy.qos"].QoSProfile = lambda *a, **kw: None
    sys.modules["rclpy.qos"].ReliabilityPolicy = types.SimpleNamespace(
        RELIABLE=object(), BEST_EFFORT=object())
    sys.modules["rclpy.qos"].DurabilityPolicy = types.SimpleNamespace(
        TRANSIENT_LOCAL=object(), VOLATILE=object())

    class _FakeApp:
        def _decorator(self, *a, **kw):
            return lambda f: f
        route = before_request = after_request = errorhandler = _decorator

        def run(self, *a, **kw):
            raise AssertionError("테스트가 실제 서버를 띄우려 했습니다")

    sys.modules["flask"].Flask = lambda *a, **kw: _FakeApp()
    sys.modules["flask"].Response = object
    sys.modules["flask"].jsonify = lambda *a, **kw: (a[0] if len(a) == 1 else kw)
    sys.modules["flask"].request = types.SimpleNamespace(args={}, headers={})
    sys.modules["flask"].abort = lambda *a, **kw: None
    sys.modules["flask"].send_from_directory = lambda *a, **kw: None

    class _TransformException(Exception):
        pass

    sys.modules["tf2_ros"].TransformException = _TransformException
    sys.modules["tf2_ros.buffer"].Buffer = object
    sys.modules["tf2_ros.transform_listener"].TransformListener = object

    import importlib.util
    spec = importlib.util.spec_from_file_location("minimap_renderer_pm", _MINIMAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Grid:
    """x=3.0m 에 세로 벽 하나만 있는 10m x 10m 빈 지도."""

    def __init__(self):
        self.info = types.SimpleNamespace(
            width=100, height=100, resolution=0.1,
            origin=types.SimpleNamespace(
                position=types.SimpleNamespace(x=-5.0, y=-5.0)))
        cells = [0] * (100 * 100)
        for row in range(100):
            cells[row * 100 + 80] = 100      # x = -5.0 + 80*0.1 = 3.0m
        self.data = cells


def test_minimap_raycast(mr):
    print("=== 2. 다은 노트북 - 사람은 SLAM 지도에 없다 ===")
    grid = _Grid()
    origin = (0.0, 0.0)

    check("실시간 장애물 레이캐스팅 함수가 있다",
          hasattr(mr, "raycast_to_live_obstacle"))

    # (가) 열린 방향(90도) - 지도만 보면 아무것도 없어서 탐지가 통째로 버려졌다.
    open_dir = 1.5707963
    check("지도만 보면 열린 방향은 여전히 None (기존 동작)",
          mr.raycast_to_obstacle(grid, origin, open_dir, max_range=8.0) is None)
    hit = mr.raycast_to_live_obstacle(
        [{"x": 0.0, "y": 2.0, "cells": 5, "source": "scan"}], origin, open_dir, 8.0)
    check("열린 공간에 선 작업자도 실시간 장애물로 찾아낸다",
          hit is not None and abs(hit[1] - 2.0) < 0.05, f"실제: {hit}")

    # (나) 벽 앞에 선 작업자 - 예전에는 3.0m 벽에 마커가 찍혔다.
    wall_hit = mr.raycast_to_obstacle(grid, origin, 0.0, max_range=8.0)
    check("지도만 보면 뒤쪽 벽(3.0m)에 찍힌다 (기존 동작)",
          wall_hit is not None and abs(wall_hit[0] - 3.0) < 0.15, f"실제: {wall_hit}")
    person = [{"x": 1.5, "y": 0.0, "cells": 6, "source": "scan"}]
    hit = mr.raycast_to_live_obstacle(person, origin, 0.0, 8.0)
    check("실시간 장애물을 보면 사람 자리(1.5m)에 찍힌다",
          hit is not None and abs(hit[0] - 1.5) < 0.05, f"실제: {hit}")

    # (다) 레이에서 옆으로 많이 벗어난 물체는 집지 않는다.
    aside = [{"x": 1.5, "y": 1.2, "cells": 5, "source": "scan"}]
    check("레이에서 벗어난 물체는 무시한다",
          mr.raycast_to_live_obstacle(aside, origin, 0.0, 8.0) is None)

    # (라) 뒤에 있는 물체를 앞으로 착각하지 않는다.
    behind = [{"x": -1.5, "y": 0.0, "cells": 5, "source": "scan"}]
    check("등 뒤의 물체는 무시한다",
          mr.raycast_to_live_obstacle(behind, origin, 0.0, 8.0) is None)

    # (마) 여러 개면 가장 가까운 것.
    two = [{"x": 1.2, "y": 0.05, "cells": 5}, {"x": 2.6, "y": -0.05, "cells": 5}]
    hit = mr.raycast_to_live_obstacle(two, origin, 0.0, 8.0)
    check("겹치면 가장 가까운 것을 고른다", hit is not None and abs(hit[0] - 1.2) < 0.05)
    print()


def test_minimap_marker_style(mr):
    print("=== 3. 다은 노트북 - MJPEG 미니맵 마커 구분 ===")
    person_color, person_caption = mr.detection_marker_style("person")
    fire_color, _ = mr.detection_marker_style("fire")
    helmet_color, _ = mr.detection_marker_style("person with no helmet")

    check("person 은 작업자 표기를 받는다", person_caption == "WORKER", person_caption)
    check("작업자와 화재가 다른 색이다", person_color != fire_color)
    check("작업자와 안전모 미착용이 다른 색이다", person_color != helmet_color)
    _, unknown = mr.detection_marker_style("무언가 새로운 클래스")
    check("모르는 라벨은 원문을 그대로 보여준다", unknown == "무언가 새로운 클래스")
    print()


# ----------------------------------------------------------------------
# 4. 상민 PC - 라벨 -> 작업자
# ----------------------------------------------------------------------
def test_schema_label():
    print("=== 4. 상민 PC - 미니맵 탐지 -> DB/화면 이름 ===")
    event = build_map_detection_event(
        "A", {"id": 7, "label": "person", "x": 1.2, "y": 0.5, "hit_count": 3})
    check("미니맵의 person 이 '작업자'로 변환된다",
          event.detected_object == "작업자", event.detected_object)
    check("지도 좌표가 그대로 실린다", event.map_x == 1.2 and event.map_y == 0.5)
    print()


def main():
    namespace = load_sender_dedup()
    test_sender_dedup(namespace)

    minimap = load_minimap()
    test_minimap_raycast(minimap)
    test_minimap_marker_style(minimap)

    test_schema_label()
    print("모든 테스트 통과!")


if __name__ == "__main__":
    main()
