r"""
self_test/test_map_cleaner.py

이다은님 폴더 `maps/clean_map.py` — 저장된 SLAM 지도에서 "치우면 없어질 물건"
(의자 다리 등)을 지우는 도구를 검증합니다.

왜 필요한가
-----------
2D LiDAR 는 바닥에서 약 0.18m 한 높이만 봅니다. 하필 그 높이에 의자·책상 다리가
걸려서, 매핑 중에 서 있던 가구가 지도에 점유 셀로 박힙니다. 나중에 치워도 지도에는
그대로 남고, 그 지도를 **Nav2 · AMCL · patrol_planner 가 함께 믿습니다.**
특히 Nav2 의 static layer 는 레이트레이싱으로 지워지지 않아서, 로봇이 그 자리를
지나가며 빈 공간을 봐도 전역 코스트맵의 유령은 남습니다.

무엇을 보는가
-------------
이 도구는 **지우면 안 되는 것을 지우지 않는 것**이 훨씬 중요합니다. 벽을 지우면
로봇이 벽으로 돌진합니다. 그래서 "지운다" 보다 "안 지운다" 쪽 검증이 더 많습니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_map_cleaner.py
"""

import os
import sys
import tempfile

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_TARGET = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)",
                       "maps", "clean_map.py")

RES = 0.05


def check(desc, condition, detail=""):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def load_cleaner():
    if not os.path.exists(_TARGET):
        print(f"[중단] 검증 대상을 찾지 못했습니다:\n       {_TARGET}")
        raise SystemExit(1)
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("[건너뜀] opencv-python 이 없어 이 테스트는 돌릴 수 없습니다.")
        raise SystemExit(0)

    import importlib.util
    spec = importlib.util.spec_from_file_location("clean_map_test", _TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_map(cm):
    """검증용 지도 한 장.

    200x160 셀(10m x 8m). 바깥은 미탐사, 방 안은 자유공간, 테두리는 벽.
    여기에 여러 종류의 점유 덩어리를 심어 두고 무엇이 지워지는지 봅니다.
    """
    rows, cols = 160, 200
    image = np.full((rows, cols), cm.UNKNOWN_PIXEL, np.uint8)
    image[10:150, 10:190] = cm.FREE_PIXEL          # 방 안
    # 테두리 벽 (두께 4셀 = 0.2m)
    image[10:14, 10:190] = cm.OCC_PIXEL
    image[146:150, 10:190] = cm.OCC_PIXEL
    image[10:150, 10:14] = cm.OCC_PIXEL
    image[10:150, 186:190] = cm.OCC_PIXEL

    marks = {}

    # (1) 의자 다리 — 1셀. 사방이 자유공간
    image[40, 40] = cm.OCC_PIXEL
    marks["의자다리_1셀"] = (40, 40)

    # (2) 책상 다리 — 2x2 셀 (0.1m)
    image[60:62, 70:72] = cm.OCC_PIXEL
    marks["책상다리_2x2"] = (60, 70)

    # (3) 기둥 — 8x8 셀 (0.4m). 진짜 구조물이라 남아야 한다
    image[90:98, 100:108] = cm.OCC_PIXEL
    marks["기둥_8x8"] = (90, 100)

    # (4) 안쪽 칸막이벽 — 얇지만 길다. 남아야 한다
    image[110:114, 40:140] = cm.OCC_PIXEL
    marks["칸막이벽"] = (110, 40)

    # (5) 미탐사에 붙은 작은 점 — 뒤를 본 적이 없으므로 남아야 한다
    image[30:50, 160:180] = cm.UNKNOWN_PIXEL       # 미탐사 구멍
    image[45, 158] = cm.OCC_PIXEL
    marks["미탐사옆_점"] = (45, 158)

    # (6) 벽에 붙은 점 — 벽 성분에 합쳐지므로 남아야 한다
    image[15, 50] = cm.OCC_PIXEL                   # 위쪽 벽 바로 아래
    marks["벽에붙은_점"] = (15, 50)

    return image, marks


def test_classify(cm):
    print("=== 픽셀 값 분류 ===")
    image, _ = build_map(cm)
    occupied, free, unknown = cm.classify(image)
    check("벽은 점유", occupied[12, 100])
    check("방 안은 자유공간", free[80, 60])
    check("바깥은 미탐사", unknown[2, 2])
    check("세 마스크가 겹치지 않는다",
          not (occupied & free).any() and not (occupied & unknown).any())
    check("세 마스크가 전부를 덮는다",
          int(occupied.sum() + free.sum() + unknown.sum()) == image.size)
    print()


def test_removal_decisions(cm):
    print("=== 무엇을 지우고 무엇을 남기는가 ===")
    image, marks = build_map(cm)
    occupied, free, _unknown = cm.classify(image)
    remove, items = cm.find_removable(occupied, free, RES)

    def removed(name):
        row, col = marks[name]
        return bool(remove[row, col])

    check("의자 다리(1셀)를 지운다", removed("의자다리_1셀"))
    check("책상 다리(2x2)를 지운다", removed("책상다리_2x2"))

    check("기둥(0.4m)은 남긴다 — 진짜 구조물", not removed("기둥_8x8"))
    check("칸막이벽은 남긴다 — 얇아도 길다", not removed("칸막이벽"))
    check("미탐사 옆의 점은 남긴다 — 뒤를 본 적이 없다", not removed("미탐사옆_점"))
    check("벽에 붙은 점은 남긴다 — 벽과 한 덩어리", not removed("벽에붙은_점"))

    check("후보를 2개 찾았다", len(items) == 2, str(len(items)))
    check("  큰 것부터 정렬된다", items[0]["cells"] >= items[-1]["cells"])
    for item in items:
        check(f"  ({item['col']}, {item['row']}) 둘레가 대부분 자유공간",
              item["free_ring"] >= cm.DEFAULT_MIN_FREE_RING, str(item["free_ring"]))
    print()


def test_thresholds(cm):
    print("=== 기준을 조이고 푸는가 ===")
    image, _ = build_map(cm)
    occupied, free, _ = cm.classify(image)

    none_found, items = cm.find_removable(occupied, free, RES, max_cells=0)
    check("max_cells=0 이면 아무것도 안 지운다",
          not none_found.any() and not items)

    _mask, loose = cm.find_removable(occupied, free, RES,
                                     max_extent_m=0.50, max_cells=100)
    check("기준을 풀면 기둥까지 후보가 된다", len(loose) > 2, str(len(loose)))

    _mask, loose_ring = cm.find_removable(occupied, free, RES, min_free_ring=0.5)
    check("둘레 조건을 낮추면 미탐사에 닿은 점까지 후보가 된다",
          len(loose_ring) > 2, str(len(loose_ring)))

    _mask, strict = cm.find_removable(occupied, free, RES, min_free_ring=1.01)
    check("둘레 조건을 1.0 초과로 두면 아무것도 안 지운다", not strict,
          str(len(strict)))
    print()


def test_cli_roundtrip(cm):
    print("=== 파일 입출력 ===")
    image, marks = build_map(cm)

    with tempfile.TemporaryDirectory() as tmp:
        cm.write_pgm(os.path.join(tmp, "t.pgm"), image)
        with open(os.path.join(tmp, "t.yaml"), "w", encoding="utf-8") as f:
            f.write("image: t.pgm\nmode: trinary\nresolution: 0.05\n"
                    "origin: [-1.0, -2.0, 0.0]\nnegate: 0\n"
                    "occupied_thresh: 0.65\nfree_thresh: 0.196\n")

        loaded, yaml_text, resolution = cm.read_map(tmp, "t")
        check("쓴 그대로 읽힌다", np.array_equal(loaded, image))
        check("해상도를 읽는다", abs(resolution - 0.05) < 1e-9)

        # dry-run 은 파일을 만들지 않아야 한다
        code = cm.main(["t", "--dir", tmp, "--dry-run"])
        check("dry-run 이 정상 종료", code == 0)
        check("  파일을 만들지 않는다",
              not os.path.exists(os.path.join(tmp, "t_clean.pgm")))

        code = cm.main(["t", "--dir", tmp])
        check("정리본 생성이 정상 종료", code == 0)
        check("  pgm 이 생겼다", os.path.exists(os.path.join(tmp, "t_clean.pgm")))
        check("  yaml 이 생겼다", os.path.exists(os.path.join(tmp, "t_clean.yaml")))
        check("  검토용 그림이 생겼다",
              os.path.exists(os.path.join(tmp, "t_clean_review.png")))

        with open(os.path.join(tmp, "t_clean.yaml"), encoding="utf-8") as f:
            new_yaml = f.read()
        check("  yaml 이 새 pgm 을 가리킨다", "image: t_clean.pgm" in new_yaml)
        check("  해상도·원점은 그대로", "resolution: 0.05" in new_yaml
              and "origin: [-1.0, -2.0, 0.0]" in new_yaml)

        cleaned, _t, _r = cm.read_map(tmp, "t_clean")
        check("  크기가 같다", cleaned.shape == image.shape)

        chair_row, chair_col = marks["의자다리_1셀"]
        check("  의자 다리 자리가 자유공간이 됐다",
              cleaned[chair_row, chair_col] == cm.FREE_PIXEL)
        pillar_row, pillar_col = marks["기둥_8x8"]
        check("  기둥은 그대로 점유", cleaned[pillar_row, pillar_col] == cm.OCC_PIXEL)

        # 원본을 건드리면 되돌릴 수가 없다
        original, _t, _r = cm.read_map(tmp, "t")
        check("  **원본은 손대지 않는다**", np.array_equal(original, image))

        # 지운 셀 수만큼만 달라져야 한다
        occupied, free, _ = cm.classify(image)
        remove, _items = cm.find_removable(occupied, free, RES)
        check("  지운 셀 수만큼만 달라졌다",
              int((cleaned != image).sum()) == int(remove.sum()),
              f"{int((cleaned != image).sum())} vs {int(remove.sum())}")
    print()


def test_missing_files(cm):
    print("=== 없는 지도를 부를 때 ===")
    with tempfile.TemporaryDirectory() as tmp:
        code = cm.main(["없는지도", "--dir", tmp])
        check("예외가 아니라 오류 코드로 끝난다", code == 1)
    print()


def test_real_maps(cm):
    """저장소에 있는 실제 지도에서 무엇이 지워지는지 확인합니다.

    지금은 후보가 없는 것이 정상입니다(가구가 있는 실물 현장 지도가 아직 없음).
    여기서 보는 것은 **실제 파일을 읽다 죽지 않는가** 입니다.
    """
    print("=== 저장소의 실제 지도 ===")
    maps_dir = os.path.join(_REPO_ROOT, "ROS2_자율주행_및_연동(이다은)", "maps")
    found_any = False
    for name in ("factory_map", "smart_factory_map", "digital_twin_map"):
        if not os.path.exists(os.path.join(maps_dir, name + ".pgm")):
            continue
        found_any = True
        image, _text, resolution = cm.read_map(maps_dir, name)
        occupied, free, _unknown = cm.classify(image)
        _remove, items = cm.find_removable(occupied, free, resolution)
        check(f"{name} 을 읽고 판정한다 (후보 {len(items)}개)", True)
    check("지도를 하나 이상 읽었다", found_any)
    print()


def main():
    cm = load_cleaner()
    print("검증 대상: ROS2_자율주행_및_연동(이다은)/maps/clean_map.py\n")

    test_classify(cm)
    test_removal_decisions(cm)
    test_thresholds(cm)
    test_cli_roundtrip(cm)
    test_missing_files(cm)
    test_real_maps(cm)

    print("모든 테스트 통과!")


if __name__ == "__main__":
    main()
