#!/usr/bin/env python3
"""저장된 SLAM 지도에서 "치우면 없어질 물건"을 지운다.

무슨 문제인가
-------------
2D LiDAR 는 한 높이(터틀봇 LDS-03 은 바닥에서 약 0.18m)만 봅니다. 하필 그 높이에
**의자 다리 · 책상 다리 · 파렛트 모서리 · 사람 발목** 이 걸립니다. 매핑 중에 이런
것들이 서 있으면 지도에 점유 셀로 박히고, 나중에 치워도 지도에는 그대로 남습니다.

이게 왜 골치아프냐면, 저장된 지도를 **세 곳이 함께** 믿기 때문입니다.

  - Nav2 전역 코스트맵(static layer) — 유령 장애물 주변이 부풀려져 경로가 막힙니다
  - AMCL — 없는 물체를 기준으로 위치를 맞추려 해서 정합이 나빠집니다
  - patrol_planner — 여유 공간이 줄어 순찰 지점이 밀리거나 구역이 통째로 빠집니다

특히 Nav2 는 **static layer 를 레이트레이싱으로 지우지 않습니다.** 로봇이 그 자리를
지나가며 "비어 있다"를 봐도 전역 코스트맵의 유령은 그대로입니다.

왜 SLAM 중에 저절로 안 지워지는가
---------------------------------
Cartographer 의 서브맵은 `num_range_data`(기본 90 스캔)를 채우면 **동결됩니다.**
의자가 서브맵 N 에 박히면, 나중에 그 자리를 다시 지나가며 빈 공간을 관측해도 그건
새 서브맵에 들어갈 뿐 옛 서브맵은 바뀌지 않습니다. 확률 파라미터를 조정하면
서브맵 하나 안에서 생겼다 사라지는 잡음은 줄지만, 이미 굳은 것은 못 지웁니다.

그래서 이 스크립트가 필요합니다.

어떻게 판단하는가
-----------------
점유 셀 덩어리를 연결 성분으로 묶고, 아래 **세 가지를 모두** 만족할 때만 지웁니다.

  1. 작다      — 셀 수가 `--max-cells` 이하
  2. 짧다      — 가로/세로 길이가 `--max-extent` (m) 이하
  3. 떠 있다   — 덩어리 둘레가 **전부 자유공간** (기본값 기준)

3번이 중요합니다. 둘레에 미탐사가 조금이라도 닿아 있으면 그 물체 뒤를 본 적이
없다는 뜻이고, 그러면 가구인지 벽의 일부인지 판단할 근거가 없습니다. 그런 건
건드리지 않습니다. **모르는 것은 지우지 않는다** 가 이 스크립트의 원칙입니다.

이 기준이 실제로 원하는 대로 갈라집니다.

  통로 한가운데 의자 -> 로봇이 주위를 돌았으므로 둘레가 전부 자유공간 -> 지움
  벽에 붙은 의자     -> 둘레에 벽이 섞임 -> 남김 (옆이 벽이라 지워도 이득이 없음)

원본은 절대 덮어쓰지 않습니다. `<이름>_clean.pgm/.yaml` 을 새로 만들고, 무엇을
지웠는지 빨갛게 칠한 검토용 그림(`<이름>_clean_review.png`)을 함께 냅니다.
**그림을 눈으로 확인한 뒤에** 쓰세요.

사용법
------
    # 무엇이 지워질지 먼저 본다 (파일은 안 씁니다)
    python3 clean_map.py factory_map --dry-run

    # 실제로 정리본을 만든다
    python3 clean_map.py factory_map

    # 기준을 조절한다 (기본은 0.20m 이하, 12셀 이하)
    python3 clean_map.py factory_map --max-extent 0.30 --max-cells 20

정리본을 쓰려면 Nav2 실행 시 지도를 바꿔 주면 됩니다.

    ros2 launch smart_factory_sim real_navigation.launch.py \\
        map:=$HOME/factory_map_clean.yaml
"""

import argparse
import os
import re
import sys

import cv2
import numpy as np

# Windows 콘솔(cp949)에서 이모지를 출력하다 죽는 것을 막습니다.
# (이 폴더는 ROS2 패키지라 상민님 폴더의 common/console.py 를 가져올 수 없어
#  같은 일을 직접 합니다. 리눅스는 원래 UTF-8 이라 아무 일도 일어나지 않습니다.)
try:
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

# map_server 규약의 픽셀 값 (generate_map.py 와 같은 값)
FREE_PIXEL, OCC_PIXEL, UNKNOWN_PIXEL = 254, 0, 205

# 지울 후보의 크기 상한.
# 의자/책상 다리는 지름 2~5cm 라 0.05m 해상도에서 보통 1~4셀입니다. 스캔이 흔들려
# 번지는 것까지 감안해 넉넉히 잡되, 파렛트나 기둥(0.3m 이상)은 남기는 값입니다.
DEFAULT_MAX_EXTENT_M = 0.20
DEFAULT_MAX_CELLS = 12

# 덩어리 둘레에서 자유공간이 차지해야 할 최소 비율. 기본값은 **1.0(전부)** 입니다.
#
# 비율을 느슨하게 두면(예: 0.7) 미탐사가 둘레의 일부만 차지할 때 그냥 통과합니다.
# 그런데 미탐사가 조금이라도 닿아 있다는 건 **그 물체 뒤를 본 적이 없다**는 뜻이고,
# 그러면 그게 가구인지 벽의 일부인지 판단할 근거가 없습니다. 이 도구는 Nav2 가
# 믿을 지도를 고치는 것이라, 애매하면 남기는 쪽이 맞습니다.
#
# 실제로도 이 기준이 원하는 대로 갈라집니다.
#   통로 한가운데 의자  -> 로봇이 돌아다녔으므로 둘레가 전부 자유공간 -> 지움
#   벽에 붙은 의자      -> 둘레에 벽이 섞임 -> 남김 (어차피 옆이 벽이라 이득도 없음)
DEFAULT_MIN_FREE_RING = 1.0

# 둘레를 볼 때 덩어리에서 얼마나 떨어진 띠를 볼지(셀).
RING_WIDTH_CELLS = 2


def read_map(directory: str, name: str):
    """pgm + yaml 을 읽어 (이미지, yaml 텍스트, 해상도) 를 돌려준다.

    cv2.imread 는 Windows 에서 한글 경로를 못 읽습니다. 이 저장소의 폴더 이름이
    한글이라 항상 걸리므로 바이트로 읽어 디코드합니다.
    """
    pgm_path = os.path.join(directory, name + ".pgm")
    yaml_path = os.path.join(directory, name + ".yaml")
    for path in (pgm_path, yaml_path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    raw = np.fromfile(pgm_path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"{pgm_path} 를 이미지로 읽지 못했습니다.")

    with open(yaml_path, encoding="utf-8") as f:
        text = f.read()
    match = re.search(r"^resolution:\s*([0-9.]+)", text, re.M)
    resolution = float(match.group(1)) if match else 0.05
    return image, text, resolution


def classify(image: np.ndarray, occupied_thresh_pixel: int = 128):
    """(점유, 자유, 미탐사) 불리언 마스크.

    map_server 는 점유도(= (255-값)/255)와 임계값으로 판정하지만, 여기서는
    generate_map.py 가 쓰는 세 값(0 / 205 / 254)을 그대로 다룹니다. 중간값이
    섞인 지도에서도 동작하도록 임계로 나눕니다.
    """
    occupied = image < occupied_thresh_pixel
    free = image >= 250
    unknown = ~occupied & ~free
    return occupied, free, unknown


def find_removable(occupied, free, resolution,
                   max_extent_m=DEFAULT_MAX_EXTENT_M,
                   max_cells=DEFAULT_MAX_CELLS,
                   min_free_ring=DEFAULT_MIN_FREE_RING):
    """지워도 될 점유 덩어리를 찾는다.

    돌려주는 것: (지울 셀 마스크, 항목 목록). 항목은 사람이 검토할 수 있도록
    위치·크기·둘레 비율을 담습니다.
    """
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        occupied.astype(np.uint8), connectivity=8)

    remove = np.zeros_like(occupied, dtype=bool)
    items = []
    height, width = occupied.shape

    for label in range(1, count):
        left, top, comp_w, comp_h, area = (int(v) for v in stats[label])
        extent_m = max(comp_w, comp_h) * resolution

        if area > max_cells or extent_m > max_extent_m:
            continue                      # 크거나 길다 = 벽/설비

        # 덩어리 둘레를 본다. bbox 를 여유 있게 잘라서 그 안에서만 계산한다.
        pad = RING_WIDTH_CELLS + 1
        r0, r1 = max(0, top - pad), min(height, top + comp_h + pad)
        c0, c1 = max(0, left - pad), min(width, left + comp_w + pad)
        window = labels[r0:r1, c0:c1]
        blob = (window == label).astype(np.uint8)

        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * RING_WIDTH_CELLS + 1, 2 * RING_WIDTH_CELLS + 1))
        ring = cv2.dilate(blob, kernel).astype(bool) & ~blob.astype(bool)
        ring_total = int(ring.sum())
        if ring_total == 0:
            continue

        free_ratio = float(free[r0:r1, c0:c1][ring].sum()) / ring_total
        if free_ratio < min_free_ring:
            # 둘레에 미탐사나 다른 벽이 섞여 있다 = 이게 뭔지 확신할 수 없다.
            # (기본값 1.0 이면 "티끌 하나라도 섞이면 남긴다" 가 됩니다.)
            continue

        remove |= (labels == label)
        items.append({
            "cells": area,
            "extent_m": round(extent_m, 3),
            "free_ring": round(free_ratio, 2),
            "row": int(round(centroids[label][1])),
            "col": int(round(centroids[label][0])),
        })

    items.sort(key=lambda item: -item["cells"])
    return remove, items


def make_review_image(image: np.ndarray, remove: np.ndarray) -> np.ndarray:
    """지운 자리를 빨갛게 칠한 검토용 그림. 눈으로 확인하라고 만드는 것."""
    canvas = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    # 작은 점이라 그냥 칠하면 안 보입니다. 원래 위치는 빨강, 주변은 옅은 빨강.
    halo = cv2.dilate(remove.astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))).astype(bool)
    canvas[halo] = (200, 200, 255)
    canvas[remove] = (0, 0, 230)
    return canvas


def write_pgm(path: str, image: np.ndarray) -> None:
    """generate_map.py 와 같은 P5 바이너리 형식으로 쓴다."""
    height, width = image.shape
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (width, height))
        f.write(image.astype(np.uint8).tobytes())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="SLAM 지도에서 치우면 없어질 작은 물건(의자 다리 등)을 지웁니다.")
    parser.add_argument("name", help="지도 이름 (확장자 없이). 예: factory_map")
    parser.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="지도 폴더 (기본: 이 스크립트가 있는 곳)")
    parser.add_argument("--max-extent", type=float, default=DEFAULT_MAX_EXTENT_M,
                        help="이 길이(m) 이하인 덩어리만 지웁니다")
    parser.add_argument("--max-cells", type=int, default=DEFAULT_MAX_CELLS,
                        help="이 셀 수 이하인 덩어리만 지웁니다")
    parser.add_argument("--min-free-ring", type=float, default=DEFAULT_MIN_FREE_RING,
                        help="둘레가 이 비율 이상 자유공간이어야 지웁니다 "
                             "(기본 1.0 = 전부. 낮추면 벽/미탐사에 닿은 것도 지웁니다)")
    parser.add_argument("--dry-run", action="store_true",
                        help="무엇이 지워질지만 보고 파일은 쓰지 않습니다")
    parser.add_argument("--suffix", default="_clean", help="정리본 이름 뒤에 붙일 말")
    args = parser.parse_args(argv)

    try:
        image, yaml_text, resolution = read_map(args.dir, args.name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ 지도를 읽지 못했습니다: {exc}")
        return 1

    occupied, free, unknown = classify(image)
    total_occupied = int(occupied.sum())
    print(f"{args.name}: {image.shape[1]}x{image.shape[0]} @ {resolution}m")
    print(f"  점유 {total_occupied}칸 · 자유 {int(free.sum())}칸 · 미탐사 {int(unknown.sum())}칸")
    print(f"  기준: {args.max_extent}m 이하 · {args.max_cells}칸 이하 · "
          f"둘레 자유공간 {args.min_free_ring:.0%} 이상")
    print()

    remove, items = find_removable(
        occupied, free, resolution,
        max_extent_m=args.max_extent, max_cells=args.max_cells,
        min_free_ring=args.min_free_ring)

    if not items:
        print("지울 만한 덩어리를 찾지 못했습니다.")
        print("  물건이 정말 없거나, 벽에 붙어 있어(둘레 조건 미달) 남겨 둔 것입니다.")
        print("  --max-extent 를 키우거나 --min-free-ring 을 낮춰 보세요.")
        return 0

    removed_cells = int(remove.sum())
    print(f"지울 후보 {len(items)}개 / {removed_cells}칸 "
          f"(전체 점유의 {removed_cells / max(1, total_occupied):.1%})")
    for index, item in enumerate(items[:20], 1):
        print(f"  {index:2d}. {item['cells']:3d}칸 · {item['extent_m']:.2f}m · "
              f"둘레 자유 {item['free_ring']:.0%} · 픽셀({item['col']}, {item['row']})")
    if len(items) > 20:
        print(f"  ... 외 {len(items) - 20}개")
    print()

    if args.dry_run:
        print("--dry-run 이라 파일을 쓰지 않았습니다.")
        return 0

    cleaned = image.copy()
    cleaned[remove] = FREE_PIXEL

    out_name = args.name + args.suffix
    pgm_path = os.path.join(args.dir, out_name + ".pgm")
    yaml_path = os.path.join(args.dir, out_name + ".yaml")
    review_path = os.path.join(args.dir, out_name + "_review.png")

    write_pgm(pgm_path, cleaned)
    # yaml 은 image 줄만 바꿔 그대로 씁니다. 해상도·원점·임계값은 같아야 합니다.
    new_yaml = re.sub(r"^image:.*$", f"image: {out_name}.pgm", yaml_text, count=1,
                      flags=re.M)
    with open(yaml_path, "w", encoding="utf-8", newline="") as f:
        f.write(new_yaml if new_yaml.endswith("\n") else new_yaml + "\n")

    ok, buffer = cv2.imencode(".png", make_review_image(image, remove))
    if ok:
        buffer.tofile(review_path)

    print(f"✅ 정리본: {pgm_path}")
    print(f"          {yaml_path}")
    if ok:
        print(f"   검토용: {review_path}  ← 빨간 점이 지운 자리입니다. 꼭 확인하세요.")
    print()
    print("   원본은 그대로 두었습니다. 정리본이 이상하면 그냥 지우면 됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
