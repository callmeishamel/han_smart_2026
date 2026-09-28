#!/usr/bin/env python3
"""Gazebo 월드(.world)에서 Nav2용 2D 점유격자 지도(PGM + YAML)를 생성한다.

왜 이렇게 바꿨나
----------------
예전 버전은 smart_factory.world 의 장애물 좌표를 파이썬 코드에 손으로 다시
적어두고 PIL 로 그렸다. 그래서 월드 파일을 고쳐도 지도는 그대로였고, 실제로
"지도와 월드가 서로 다른 공간"인 상태로 커밋되어 Nav2 순찰이 전혀 동작하지
않았다. 이제는 .world 파일을 직접 파싱해서 그리므로 둘이 어긋날 수 없다.

의존성 없음(표준 라이브러리만). PGM(P5)은 헤더 + 바이트 배열이라 직접 쓴다.

사용법
------
    python3 generate_map.py                     # 아래 WORLDS 전부 생성
    python3 generate_map.py --list              # 무엇을 생성하는지만 보기
    python3 generate_map.py --world ../worlds/digital_twin_1.world \
                            --name digital_twin_map --seed 0.5,3.5

출력값 규약 (map_server 가 읽는 값과 동일)
------------------------------------------
    254 자유공간 / 0 장애물 / 205 미탐사
    미탐사 = 로봇 시작점에서 도달할 수 없는 칸(즉 바깥 공간). 실제 SLAM 지도와
    같은 모양이 되도록 시작점에서 flood fill 로 판정한다.
"""

import argparse
import math
import os
import sys
import xml.etree.ElementTree as ET
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
WORLDS_DIR = os.path.normpath(os.path.join(HERE, "..", "worlds"))

FREE, OCC, UNKNOWN = 254, 0, 205

# 기본 생성 대상. seed 는 로봇 스폰 좌표(= launch 파일의 spawn_entity -x -y)와
# 맞춰야 한다. 이 점에서 도달 가능한 영역만 자유공간으로 표시하기 때문이다.
WORLDS = [
    {"world": "smart_factory.world",  "name": "smart_factory_map",  "seed": (0.5, 3.5)},
    {"world": "digital_twin_1.world", "name": "digital_twin_map",   "seed": (0.5, 3.5)},
]

# 이 높이의 수평면을 지나는 장애물만 지도에 넣는다.
# TurtleBot3 Burger 의 LDS 라이다는 base_footprint 기준 약 0.18 m 에 있다.
DEFAULT_LIDAR_Z = 0.18
DEFAULT_RES = 0.05
DEFAULT_MARGIN = 0.6   # 바깥 벽 너머로 남길 여백(m)

# 지도에 넣지 않을 모델 이름 조각 (이벤트 표식, 조명, 바닥, 로봇 등)
SKIP_NAMES = ("ground_plane", "sun", "turtlebot", "robot", "event_target")


# ---------------------------------------------------------------------------
# SDF 파싱
# ---------------------------------------------------------------------------
def _pose_of(elem):
    """<pose>x y z r p y</pose> 를 2D (x, y, yaw) 로. 없으면 원점."""
    node = elem.find("pose")
    if node is None or not (node.text or "").strip():
        return (0.0, 0.0, 0.0)
    v = [float(t) for t in node.text.split()]
    while len(v) < 6:
        v.append(0.0)
    if abs(v[3]) > 1e-6 or abs(v[4]) > 1e-6:
        print(f"  [주의] roll/pitch 가 0이 아닌 pose 를 2D로 눕혀서 처리합니다: {node.text.strip()}")
    return (v[0], v[1], v[5])


def _compose(parent, child):
    """부모 좌표계 위에 자식 pose 를 얹는다 (2D 강체 변환 합성)."""
    px, py, pa = parent
    cx, cy, ca = child
    return (px + cx * math.cos(pa) - cy * math.sin(pa),
            py + cx * math.sin(pa) + cy * math.cos(pa),
            pa + ca)


def _z_span(pose_z, geom):
    """장애물이 차지하는 z 구간. 라이다 평면과 겹치는지 판정하는 데 쓴다."""
    box = geom.find("box")
    if box is not None:
        sz = [float(t) for t in box.find("size").text.split()]
        return (pose_z - sz[2] / 2.0, pose_z + sz[2] / 2.0)
    cyl = geom.find("cylinder")
    if cyl is not None:
        length = float(cyl.find("length").text)
        return (pose_z - length / 2.0, pose_z + length / 2.0)
    sph = geom.find("sphere")
    if sph is not None:
        r = float(sph.find("radius").text)
        return (pose_z - r, pose_z + r)
    return None


def extract_shapes(world_path, lidar_z=DEFAULT_LIDAR_Z):
    """월드 안의 collision 도형을 (종류, 파라미터) 목록으로 뽑는다."""
    root = ET.parse(world_path).getroot()
    world = root.find("world")
    if world is None:
        raise SystemExit(f"{world_path}: <world> 요소가 없습니다.")

    shapes = []
    skipped = []

    for model in world.findall("model"):
        name = model.get("name", "")
        if any(s in name for s in SKIP_NAMES):
            skipped.append(name)
            continue

        model_pose = _pose_of(model)
        # <pose> 의 z 는 도형 높이 판정에 필요하므로 따로 읽는다
        mz_node = model.find("pose")
        model_z = float(mz_node.text.split()[2]) if mz_node is not None and mz_node.text else 0.0

        for link in model.findall("link"):
            link_pose = _compose(model_pose, _pose_of(link))
            lz_node = link.find("pose")
            link_z = model_z + (float(lz_node.text.split()[2])
                                if lz_node is not None and lz_node.text else 0.0)

            for col in link.findall("collision"):
                geom = col.find("geometry")
                if geom is None:
                    continue
                col_pose = _compose(link_pose, _pose_of(col))
                cz_node = col.find("pose")
                col_z = link_z + (float(cz_node.text.split()[2])
                                  if cz_node is not None and cz_node.text else 0.0)

                span = _z_span(col_z, geom)
                if span is None:
                    continue
                if not (span[0] <= lidar_z <= span[1]):
                    continue   # 라이다 평면을 지나지 않으므로 스캔에 안 잡힌다

                box = geom.find("box")
                if box is not None:
                    sz = [float(t) for t in box.find("size").text.split()]
                    shapes.append(("box", col_pose[0], col_pose[1], sz[0], sz[1], col_pose[2]))
                    continue
                cyl = geom.find("cylinder")
                if cyl is not None:
                    shapes.append(("cyl", col_pose[0], col_pose[1],
                                   float(cyl.find("radius").text)))

    if skipped:
        print(f"  건너뜀: {', '.join(sorted(set(skipped)))}")
    return shapes


# ---------------------------------------------------------------------------
# 래스터화
# ---------------------------------------------------------------------------
def shape_extent(s):
    """도형 하나가 실제로 차지하는 (xmin, ymin, xmax, ymax).

    회전한 박스는 외접원이 아니라 네 모서리를 직접 돌려서 잰다. 외접원을 쓰면
    20m x 0.2m 짜리 벽이 20m 짜리 정사각형으로 잡혀 캔버스가 배로 커진다."""
    if s[0] == "box":
        _, cx, cy, w, h, yaw = s
        ca, sa = math.cos(yaw), math.sin(yaw)
        xs, ys = [], []
        for ex, ey in ((-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)):
            xs.append(cx + ex * ca - ey * sa)
            ys.append(cy + ex * sa + ey * ca)
        return (min(xs), min(ys), max(xs), max(ys))
    _, cx, cy, r = s
    return (cx - r, cy - r, cx + r, cy + r)


def bounds_of(shapes, margin):
    if not shapes:
        raise SystemExit("장애물을 하나도 찾지 못했습니다. 월드 파일을 확인하세요.")
    ext = [shape_extent(s) for s in shapes]
    return (min(e[0] for e in ext) - margin, min(e[1] for e in ext) - margin,
            max(e[2] for e in ext) + margin, max(e[3] for e in ext) + margin)


def rasterize(shapes, res, margin):
    ox, oy, x1, y1 = bounds_of(shapes, margin)
    w = int(math.ceil((x1 - ox) / res))
    h = int(math.ceil((y1 - oy) / res))
    grid = bytearray([FREE]) * (w * h)       # row 0 = y 최소 (아래쪽)

    def mark(col, row):
        if 0 <= col < w and 0 <= row < h:
            grid[row * w + col] = OCC

    for s in shapes:
        xmin, ymin, xmax, ymax = shape_extent(s)
        c0 = max(0, int((xmin - ox) / res))
        c1 = min(w - 1, int((xmax - ox) / res) + 1)
        r0 = max(0, int((ymin - oy) / res))
        r1 = min(h - 1, int((ymax - oy) / res) + 1)

        if s[0] == "box":
            _, cx, cy, bw, bh, yaw = s
            ca, sa = math.cos(-yaw), math.sin(-yaw)
            for row in range(r0, r1 + 1):
                py = oy + (row + 0.5) * res
                for col in range(c0, c1 + 1):
                    px = ox + (col + 0.5) * res
                    dx, dy = px - cx, py - cy
                    lx = dx * ca - dy * sa       # 박스 로컬 좌표로 역회전
                    ly = dx * sa + dy * ca
                    if abs(lx) <= bw / 2.0 and abs(ly) <= bh / 2.0:
                        mark(col, row)
        else:
            _, cx, cy, rad = s
            for row in range(r0, r1 + 1):
                py = oy + (row + 0.5) * res
                for col in range(c0, c1 + 1):
                    px = ox + (col + 0.5) * res
                    if math.hypot(px - cx, py - cy) <= rad:
                        mark(col, row)

    return grid, w, h, ox, oy


def mark_unreachable_unknown(grid, w, h, res, ox, oy, seed):
    """로봇 시작점에서 4방향으로 퍼져나가며 닿는 칸만 자유공간으로 남기고,
    닿지 못한 자유칸(= 벽 바깥)은 미탐사로 바꾼다. 실제 SLAM 지도와 같은 모양."""
    sc = int((seed[0] - ox) / res)
    sr = int((seed[1] - oy) / res)
    if not (0 <= sc < w and 0 <= sr < h) or grid[sr * w + sc] == OCC:
        print(f"  [주의] 시작점 {seed} 이 지도 밖이거나 장애물 안입니다. "
              f"미탐사 판정을 건너뜁니다.")
        return 0

    seen = bytearray(w * h)
    q = deque([(sc, sr)])
    seen[sr * w + sc] = 1
    reached = 0
    while q:
        c, r = q.popleft()
        reached += 1
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nc, nr = c + dc, r + dr
            if 0 <= nc < w and 0 <= nr < h:
                i = nr * w + nc
                if not seen[i] and grid[i] != OCC:
                    seen[i] = 1
                    q.append((nc, nr))

    changed = 0
    for i in range(w * h):
        if grid[i] == FREE and not seen[i]:
            grid[i] = UNKNOWN
            changed += 1
    return changed


def write_pgm(path, grid, w, h):
    """PGM(P5)은 위에서 아래로 쓴다. 우리 grid 는 row 0 이 아래쪽이므로 뒤집는다."""
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (w, h))
        for row in range(h - 1, -1, -1):
            f.write(bytes(grid[row * w:(row + 1) * w]))


def write_yaml(path, pgm_name, res, ox, oy):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"image: {pgm_name}\n")
        f.write("mode: trinary\n")
        f.write(f"resolution: {res}\n")
        f.write(f"origin: [{ox:.4f}, {oy:.4f}, 0.0]\n")
        f.write("negate: 0\n")
        f.write("occupied_thresh: 0.65\n")
    # 0.196 이어야 합니다. 미탐사 칸(205)의 점유도가 (255-205)/255 = 0.19608 인데,
    # map_server 는 "점유도 < free_thresh 이면 자유공간" 으로 읽습니다. 0.25 로 두면
    # **미탐사가 통째로 자유공간이 됩니다** — 이 스크립트가 바로 위에서 205 로
    # 칠해 둔 바깥 공간을 Nav2 가 주행 가능한 곳으로 보게 됩니다.
        f.write("free_thresh: 0.196\n")


# ---------------------------------------------------------------------------
def build(world_path, name, seed, res, margin, lidar_z, out_dir):
    print(f"\n{os.path.basename(world_path)} -> {name}.pgm / {name}.yaml")
    shapes = extract_shapes(world_path, lidar_z)
    print(f"  장애물 도형 {len(shapes)}개 (라이다 높이 {lidar_z}m 를 지나는 것만)")

    grid, w, h, ox, oy = rasterize(shapes, res, margin)
    unknown = mark_unreachable_unknown(grid, w, h, res, ox, oy, seed)

    occ = sum(1 for v in grid if v == OCC)
    free = sum(1 for v in grid if v == FREE)
    total = w * h
    print(f"  {w}x{h}px = {w*res:.1f}m x {h*res:.1f}m, origin=({ox:.3f}, {oy:.3f})")
    print(f"  자유 {free} ({free/total:5.1%}) / 장애물 {occ} ({occ/total:5.1%}) "
          f"/ 미탐사 {unknown} ({unknown/total:5.1%})")

    pgm = os.path.join(out_dir, name + ".pgm")
    write_pgm(pgm, grid, w, h)
    write_yaml(os.path.join(out_dir, name + ".yaml"), name + ".pgm", res, ox, oy)
    print(f"  저장 완료: {pgm}")
    return {"name": name, "w": w, "h": h, "res": res, "ox": ox, "oy": oy}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--world", help="월드 파일 경로 (생략하면 기본 대상 전부)")
    p.add_argument("--name", help="출력 파일 이름(확장자 제외)")
    p.add_argument("--seed", help="로봇 시작 좌표 'x,y' (도달 가능 영역 판정용)")
    p.add_argument("--res", type=float, default=DEFAULT_RES, help="해상도 m/px")
    p.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="바깥 여백 m")
    p.add_argument("--lidar-z", type=float, default=DEFAULT_LIDAR_Z, help="라이다 높이 m")
    p.add_argument("--out", default=HERE, help="출력 폴더")
    p.add_argument("--list", action="store_true", help="기본 생성 대상만 출력")
    a = p.parse_args(argv)

    if a.list:
        for w in WORLDS:
            print(f"{w['world']:26s} -> {w['name']}  (seed {w['seed']})")
        return 0

    if a.world:
        if not a.name:
            p.error("--world 를 쓸 때는 --name 도 필요합니다.")
        seed = tuple(float(t) for t in a.seed.split(",")) if a.seed else (0.0, 0.0)
        build(a.world, a.name, seed, a.res, a.margin, a.lidar_z, a.out)
        return 0

    for w in WORLDS:
        path = os.path.join(WORLDS_DIR, w["world"])
        if not os.path.exists(path):
            print(f"[건너뜀] {path} 없음")
            continue
        build(path, w["name"], w["seed"], a.res, a.margin, a.lidar_z, a.out)

    print("\n주의: 커밋되어 있던 factory_map.pgm/.yaml 은 건드리지 않았습니다.")
    print("      그 파일은 실물 로봇 SLAM 산출물로 보이므로 그대로 둡니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
