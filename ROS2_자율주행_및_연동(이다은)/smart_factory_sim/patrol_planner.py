#!/usr/bin/env python3
"""SLAM 지도에서 순찰 경로를 만든다.

왜 필요한가
-----------
`patrol_node.py` 의 웨이포인트는 `digital_twin_1.world`(Gazebo 시뮬레이션)의 랙
배치에 맞춰 손으로 찍은 좌표였습니다. 실물 로봇이 만든 지도는 크기도 원점도
다르므로, 그 좌표를 그대로 쓰면 지도 밖이거나 벽 속입니다. 그러면 Nav2 가 모든
목표를 ABORTED 로 돌려주는데, patrol_node 는 재시도 후 다음 지점으로 넘어가므로
**로그만 보면 순찰이 도는 것처럼 보입니다.**

이 모듈은 지금 들고 있는 지도에서 직접 경로를 만듭니다.


어떻게 만드는가
---------------
1. **여유 공간 계산** — 자유 셀에서 벽까지의 거리(distanceTransform)를 구합니다.
   로봇 반경 + 여유(`min_clearance_m`)를 못 채우는 자리는 후보에서 뺍니다.
   벽에 붙은 점을 목표로 주면 Nav2 가 영영 도달하지 못합니다.
2. **도달 가능성** — 로봇이 있는 연결 성분만 남깁니다. 문이 닫힌 옆방이 지도에
   찍혀 있어도 그쪽 점은 후보가 되면 안 됩니다.
3. **구역 분할** — 자유 공간을 k-means 로 나눕니다. 미니맵이 이미 나눠 둔 구역이
   있으면 그걸 그대로 받아 씁니다(화면의 구역 이름과 순찰 구역이 어긋나지
   않도록).
4. **구역별 지점 선정** — 넓은 구역일수록 지점을 더 둡니다. 각 지점은 "벽에서
   멀고 구역 중심에 가까운" 셀을 고릅니다.
5. **순서 정하기** — 여기가 "효율적인 루트"의 핵심입니다. 직선거리로 순서를
   정하면 **벽을 뚫고 가는 순서**가 나옵니다. 그래서 지도 위를 실제로 걸어서
   구한 거리(측지거리)로 거리 행렬을 만들고, 최근접 이웃 + 2-opt 로 순회
   경로를 개선합니다.
6. **바라볼 방향** — 각 지점의 yaw 는 다음 지점을 향하게 잡습니다. 카메라가
   진행 방향을 보므로 이동 중에도 탐지가 됩니다.


쓰는 곳
-------
- `patrol_node.py` : `waypoints` 파라미터가 비어 있으면 관제에서 경로가 올 때까지
  기다립니다. 관제(미니맵 `/patrol` start)가 이 모듈로 만든 경로를 실어 보냅니다.
- `dashboard_link/minimap_renderer.py` : `/patrol_plan` 으로 경로를 미리 보여주고,
  `/patrol` start 로 실제 순찰을 시작합니다.

ROS2 없이도 돌아갑니다(numpy + OpenCV 만 씁니다). 그래서 자체 테스트가 가능합니다.
"""

import heapq
import math
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

# occupancy grid 판정 임계값 — minimap_renderer.py 와 반드시 같은 값
OCC_THRESHOLD = 50

# 기본 구역 이름. minimap_renderer.py 의 ZONE_NAMES 와 같은 순서를 씁니다.
DEFAULT_ZONE_NAMES = ["A", "B", "C", "D", "E", "F"]

# 순찰 지점이 벽에서 최소한 떨어져 있어야 할 거리(m).
# 터틀봇 반경 0.105m + 주행 여유. minimap 의 GOTO_CLEARANCE_M(0.25) 과 맞춥니다.
DEFAULT_MIN_CLEARANCE_M = 0.25

# 이 면적(m^2)마다 순찰 지점을 하나씩 둡니다. 작을수록 촘촘하게 돕니다.
DEFAULT_AREA_PER_POINT_M2 = 9.0

# 한 구역에서 만들 수 있는 최대 지점 수. 넓은 구역 하나가 경로를 독차지하는 것을 막습니다.
DEFAULT_MAX_POINTS_PER_ZONE = 3

# 두 순찰 지점이 이보다 가까우면 하나로 봅니다(m).
# 구역별로 따로 뽑기 때문에, 경계에 걸친 두 구역이 거의 같은 자리를 고를 수 있습니다.
# 그대로 두면 로봇이 20cm 를 이동하려고 Nav2 목표를 한 번 더 쓰고 dwell 시간도
# 한 번 더 씁니다 — 순찰 한 바퀴가 그만큼 길어집니다.
DEFAULT_MIN_SPACING_M = 1.5

# 거리 행렬을 구할 때 쓸 성긴 격자 크기(m).
# 원본 해상도(보통 0.05m)로 다익스트라를 돌리면 셀이 수십만 개라 느립니다.
# 순서를 정하는 데는 이 정도 정밀도면 충분합니다.
DEFAULT_COARSE_CELL_M = 0.20

# 지점을 고를 때 "구역 중심에 가까운 것"에 주는 가중치(m 당 감점).
# 벽에서 먼 것과 중심에 가까운 것 사이의 균형입니다.
CENTER_PULL = 0.15

# 2-opt 개선 반복 상한. 지점이 10개 남짓이라 보통 몇 번이면 수렴합니다.
MAX_TWO_OPT_PASSES = 60

# 다익스트라 정수 비용 (직선 10, 대각 14 ≈ 10*sqrt(2))
_STRAIGHT, _DIAGONAL = 10, 14
_NEIGHBORS = ((-1, 0, _STRAIGHT), (1, 0, _STRAIGHT), (0, -1, _STRAIGHT), (0, 1, _STRAIGHT),
              (-1, -1, _DIAGONAL), (-1, 1, _DIAGONAL), (1, -1, _DIAGONAL), (1, 1, _DIAGONAL))


class Waypoint:
    """순찰 지점 하나."""

    __slots__ = ("x", "y", "yaw", "zone", "clearance_m")

    def __init__(self, x: float, y: float, yaw: float = 0.0,
                 zone: str = "", clearance_m: float = 0.0):
        self.x = float(x)
        self.y = float(y)
        self.yaw = float(yaw)
        self.zone = str(zone)
        self.clearance_m = float(clearance_m)

    def as_dict(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3),
                "yaw": round(self.yaw, 4), "zone": self.zone,
                "clearance_m": round(self.clearance_m, 2)}

    def __repr__(self):
        return f"Waypoint({self.x:.2f}, {self.y:.2f}, zone={self.zone!r})"


class PatrolPlan:
    """계획 결과. 실패해도 예외를 던지지 않고 ok=False 로 돌려줍니다.

    순찰을 못 짜는 것은 흔한 상황(지도가 아직 안 왔다, 너무 좁다)이고, 그때
    관제 화면이 죽으면 안 되기 때문입니다.
    """

    def __init__(self, waypoints: List[Waypoint], zones: List[dict],
                 total_distance_m: float, warnings: List[str],
                 ok: bool = True, reason: str = ""):
        self.waypoints = waypoints
        self.zones = zones
        self.total_distance_m = float(total_distance_m)
        self.warnings = warnings
        self.ok = bool(ok)
        self.reason = reason

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "waypoints": [w.as_dict() for w in self.waypoints],
            "zones": self.zones,
            "total_distance_m": round(self.total_distance_m, 2),
            "warnings": self.warnings,
        }

    def flat_waypoints(self) -> List[float]:
        """patrol_node 의 `waypoints` 파라미터 형식(x, y, yaw 를 이어붙인 목록)."""
        flat = []
        for w in self.waypoints:
            flat.extend([w.x, w.y, w.yaw])
        return flat

    @staticmethod
    def failed(reason: str) -> "PatrolPlan":
        return PatrolPlan([], [], 0.0, [], ok=False, reason=reason)


# ==========================================================================
# 격자 유틸
# ==========================================================================
class GridInfo:
    """occupancy grid 한 장. ROS 메시지 없이도 만들 수 있게 배열만 받습니다."""

    def __init__(self, cells: np.ndarray, resolution: float,
                 origin_x: float, origin_y: float):
        self.cells = np.asarray(cells)
        self.resolution = float(resolution)
        self.origin_x = float(origin_x)
        self.origin_y = float(origin_y)
        self.height, self.width = self.cells.shape

    @classmethod
    def from_grid_msg(cls, msg) -> Optional["GridInfo"]:
        info = msg.info
        width, height = int(info.width), int(info.height)
        resolution = float(info.resolution)
        if width <= 0 or height <= 0 or resolution <= 0:
            return None
        data = np.asarray(msg.data, dtype=np.int16)
        if data.size != width * height:
            return None
        return cls(data.reshape((height, width)), resolution,
                   float(info.origin.position.x), float(info.origin.position.y))

    def cell_center(self, row: int, col: int) -> Tuple[float, float]:
        """격자 칸의 **중심** 좌표. 모서리를 쓰면 목표가 반 칸씩 벽으로 치우칩니다."""
        return (self.origin_x + (col + 0.5) * self.resolution,
                self.origin_y + (row + 0.5) * self.resolution)

    def cell_of(self, x: float, y: float) -> Tuple[int, int]:
        return (int((y - self.origin_y) / self.resolution),
                int((x - self.origin_x) / self.resolution))

    def inside(self, row: int, col: int) -> bool:
        return 0 <= row < self.height and 0 <= col < self.width


def carve_blind_obstacles(grid: GridInfo, safe: np.ndarray,
                          blind_obstacles) -> Tuple[np.ndarray, int]:
    """LiDAR 에 안 보이는 장애물(stall_monitor 가 찾은 것) 자리를 후보에서 뺀다.

    문턱·케이블 트레이처럼 **센서로는 영영 안 보이는** 것들이라 occupancy grid 에
    없습니다. 그래서 여기서 직접 지웁니다. 안 그러면 로봇이 이미 한 번 걸렸던
    자리를 순찰 지점으로 삼고, 갈 때마다 또 걸립니다.

    (남긴 안전 마스크, 지운 셀 수) 를 돌려줍니다.
    """
    if not blind_obstacles:
        return safe, 0

    rows = np.arange(grid.height)[:, None]
    cols = np.arange(grid.width)[None, :]
    blocked = np.zeros_like(safe, dtype=bool)

    for item in blind_obstacles:
        try:
            x = float(item["x"])
            y = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        radius_m = float(item.get("radius", 0.20))
        row, col = grid.cell_of(x, y)
        pad = max(1, int(round(radius_m / grid.resolution)))
        if not (-pad <= row < grid.height + pad and -pad <= col < grid.width + pad):
            continue
        blocked |= ((rows - row) ** 2 + (cols - col) ** 2) <= pad * pad

    removed = int((safe & blocked).sum())
    return safe & ~blocked, removed


def free_mask(grid: GridInfo) -> np.ndarray:
    """알려진 자유 공간(0 이상 OCC_THRESHOLD 미만)만 255."""
    cells = grid.cells
    return (((cells >= 0) & (cells < OCC_THRESHOLD)).astype(np.uint8)) * 255


def has_suspicious_free_border(grid: GridInfo, min_free_ratio: float = 0.90) -> bool:
    """미탐사 여백이 자유 공간으로 잘못 읽힌 직사각형 지도를 찾는다.

    ``map_saver_cli``의 기본 ``free_thresh: 0.25``로 저장한 PGM을 다시 읽으면
    미탐사 회색(205, 점유도 약 0.196)이 자유 셀 0으로 바뀔 수 있다. 그 정보는
    OccupancyGrid에 도달한 뒤에는 원래 미탐사였는지 복원할 수 없다. 이 상태로
    계획하면 캔버스 전체가 하나의 큰 방인 것처럼 순찰 지점이 생긴다.

    정상 SLAM 지도는 보통 외곽이 -1이고, 네 모서리와 외곽의 90% 이상이 모두
    자유 셀인 경우는 비정상으로 본다. 자동으로 잘라 내면 벽이 덜 닫힌 지도에서
    실제 공간까지 지울 수 있으므로 계획을 거부하고 저장 임계값을 고치게 한다.
    """
    if grid is None or grid.height < 3 or grid.width < 3:
        return False
    free = (grid.cells >= 0) & (grid.cells < OCC_THRESHOLD)
    corners = (free[0, 0], free[0, -1], free[-1, 0], free[-1, -1])
    if not all(bool(value) for value in corners):
        return False
    border = np.concatenate((free[0, :], free[-1, :], free[1:-1, 0], free[1:-1, -1]))
    return bool(border.size and float(border.mean()) >= min_free_ratio)


def clearance_map(grid: GridInfo, free: np.ndarray) -> np.ndarray:
    """각 자유 셀에서 가장 가까운 벽/미탐사까지의 거리(m).

    미탐사(-1)도 장애물로 칩니다. 아직 본 적 없는 곳 바로 옆을 순찰 지점으로
    삼으면, 나중에 그 자리가 벽으로 밝혀졌을 때 목표가 벽 속에 들어갑니다.
    """
    return cv2.distanceTransform(free, cv2.DIST_L2, 5) * grid.resolution


def reachable_mask(safe: np.ndarray, start_rc: Optional[Tuple[int, int]]) -> np.ndarray:
    """로봇이 실제로 갈 수 있는 성분만 남긴 마스크.

    문이 닫힌 옆방이 지도에 찍혀 있어도 그쪽에 순찰 지점을 두면 안 됩니다.
    로봇 위치를 모르면 가장 넓은 성분을 씁니다.
    """
    count, labels = cv2.connectedComponents(safe.astype(np.uint8), connectivity=8)
    if count <= 1:
        return np.zeros_like(safe, dtype=bool)

    target = None
    if start_rc is not None:
        row, col = start_rc
        if 0 <= row < labels.shape[0] and 0 <= col < labels.shape[1]:
            label = int(labels[row, col])
            if label > 0:
                target = label

    if target is None:
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0                      # 배경 제외
        target = int(np.argmax(sizes))

    return labels == target


def _nearest_true(mask: np.ndarray, start_rc: Tuple[int, int],
                  max_radius: int = 40) -> Optional[Tuple[int, int]]:
    """start_rc 에서 가장 가까운 True 셀. 로봇이 안전 영역 밖(벽 근처)에 서 있을 때 씁니다."""
    row, col = start_rc
    height, width = mask.shape
    if 0 <= row < height and 0 <= col < width and mask[row, col]:
        return (row, col)
    for radius in range(1, max_radius + 1):
        r0, r1 = max(0, row - radius), min(height, row + radius + 1)
        c0, c1 = max(0, col - radius), min(width, col + radius + 1)
        window = mask[r0:r1, c0:c1]
        if not window.any():
            continue
        found = np.argwhere(window)
        offsets = found + (r0, c0)
        distances = ((offsets - (row, col)) ** 2).sum(axis=1)
        best = offsets[int(np.argmin(distances))]
        return (int(best[0]), int(best[1]))
    return None


# ==========================================================================
# 구역 분할
# ==========================================================================
def partition_zones(grid: GridInfo, usable: np.ndarray, zone_count: int,
                    zone_names: Optional[Sequence[str]] = None):
    """자유 공간을 k-means 로 나눈다. (라스터, 구역목록) 반환.

    이름 붙이는 규칙은 minimap_renderer.compute_zones() 와 같습니다 — 왼쪽 열부터,
    같은 열 안에서는 위에서 아래로. 두 곳이 다른 규칙을 쓰면 화면의 "A 구역" 과
    순찰의 "A 구역" 이 달라집니다.
    """
    names = list(zone_names) if zone_names else DEFAULT_ZONE_NAMES
    cells_rc = np.argwhere(usable)
    if len(cells_rc) < zone_count or zone_count < 1:
        return None, []

    sample = cells_rc.astype(np.float32)
    cv2.setRNGSeed(0)                     # 실행마다 구역이 달라지지 않게 고정
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1.0)
    _compactness, _labels, centers = cv2.kmeans(
        sample, zone_count, None, criteria, 5, cv2.KMEANS_PP_CENTERS)

    col_bucket = max(1.0, grid.width / max(1, zone_count)) / 2.0
    order = sorted(range(len(centers)),
                   key=lambda i: (round(centers[i][1] / col_bucket), centers[i][0]))
    centers = centers[order]

    diff = cells_rc[:, None, :].astype(np.float32) - centers[None, :, :]
    assign = np.argmin((diff ** 2).sum(axis=2), axis=1)

    raster = np.zeros((grid.height, grid.width), dtype=np.uint8)
    raster[cells_rc[:, 0], cells_rc[:, 1]] = (assign + 1).astype(np.uint8)

    cell_area = grid.resolution * grid.resolution
    zones = []
    for idx in range(zone_count):
        members = cells_rc[assign == idx]
        if len(members) == 0:
            continue
        mean_row, mean_col = members.mean(axis=0)
        x, y = grid.cell_center(mean_row, mean_col)
        zones.append({
            "name": names[idx] if idx < len(names) else f"Z{idx + 1}",
            "index": idx + 1,
            "x": float(x), "y": float(y),
            "area_m2": round(float(len(members) * cell_area), 1),
            "cells": int(len(members)),
        })
    return raster, zones


def _points_for_zone(area_m2: float, area_per_point: float, cap: int) -> int:
    if area_per_point <= 0:
        return 1
    return int(max(1, min(cap, round(area_m2 / area_per_point))))


def pick_zone_waypoints(grid: GridInfo, usable: np.ndarray, raster: np.ndarray,
                        zone: dict, clearance: np.ndarray,
                        count: int) -> List[Waypoint]:
    """한 구역 안에서 순찰 지점 count 개를 고른다.

    구역을 다시 k-means 로 count 조각으로 나눈 뒤, 각 조각에서
    `여유거리 - CENTER_PULL * 조각중심까지거리` 가 가장 큰 셀을 고릅니다.
    벽에서 멀되 구석에 처박히지 않는 자리가 나옵니다.
    """
    zone_cells = np.argwhere(usable & (raster == zone["index"]))
    if len(zone_cells) == 0:
        return []

    count = int(max(1, min(count, len(zone_cells))))
    if count == 1:
        centers = np.array([zone_cells.mean(axis=0)], dtype=np.float32)
        assign = np.zeros(len(zone_cells), dtype=np.int32)
    else:
        cv2.setRNGSeed(0)
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        _c, labels, centers = cv2.kmeans(
            zone_cells.astype(np.float32), count, None,
            criteria, 3, cv2.KMEANS_PP_CENTERS)
        assign = labels.ravel()

    picked = []
    for slot in range(count):
        members = zone_cells[assign == slot]
        if len(members) == 0:
            continue
        center = centers[slot]
        dist_m = np.sqrt(((members - center) ** 2).sum(axis=1)) * grid.resolution
        score = clearance[members[:, 0], members[:, 1]] - CENTER_PULL * dist_m
        best = members[int(np.argmax(score))]
        x, y = grid.cell_center(int(best[0]), int(best[1]))
        picked.append(Waypoint(x, y, 0.0, zone["name"],
                               float(clearance[best[0], best[1]])))
    return picked


# ==========================================================================
# 거리 행렬 (측지거리) 와 순회 순서
# ==========================================================================
def _coarse_passable(usable: np.ndarray, factor: int) -> np.ndarray:
    """성긴 격자로 줄인 통행 가능 마스크.

    블록 안 자유 셀이 절반을 넘으면 통행 가능으로 봅니다. 거리 **순서**를
    정하는 용도라 이 정도 근사면 충분하고, 원본 해상도로 다익스트라를 돌리는
    것보다 수십 배 빠릅니다.
    """
    if factor <= 1:
        return usable.copy()
    height, width = usable.shape
    rows, cols = height // factor, width // factor
    if rows < 1 or cols < 1:
        return usable.copy()
    trimmed = usable[:rows * factor, :cols * factor].astype(np.float32)
    blocks = trimmed.reshape(rows, factor, cols, factor).mean(axis=(1, 3))
    return blocks >= 0.5


def _dijkstra(passable: np.ndarray, start: Tuple[int, int]) -> np.ndarray:
    """start 에서 각 셀까지의 비용(정수). 못 가면 -1."""
    height, width = passable.shape
    dist = np.full((height, width), -1, dtype=np.int32)
    if not passable[start]:
        return dist

    dist[start] = 0
    queue = [(0, start[0], start[1])]
    while queue:
        cost, row, col = heapq.heappop(queue)
        # 힙에는 같은 셀이 여러 번 들어갈 수 있습니다. 이미 더 싼 길을 찾았으면
        # 지금 꺼낸 항목은 낡은 것이므로 버립니다. (dist 는 push 직전에만 쓰므로
        # 여기서 꺼낸 셀의 dist 는 항상 0 이상입니다.)
        if cost > dist[row, col]:
            continue
        for d_row, d_col, step in _NEIGHBORS:
            n_row, n_col = row + d_row, col + d_col
            if not (0 <= n_row < height and 0 <= n_col < width):
                continue
            if not passable[n_row, n_col]:
                continue
            new_cost = cost + step
            if dist[n_row, n_col] < 0 or new_cost < dist[n_row, n_col]:
                dist[n_row, n_col] = new_cost
                heapq.heappush(queue, (new_cost, n_row, n_col))
    return dist


def geodesic_distance_matrix(grid: GridInfo, usable: np.ndarray,
                             waypoints: Sequence[Waypoint],
                             coarse_cell_m: float) -> Tuple[np.ndarray, List[str]]:
    """지도 위를 실제로 걸어서 잰 지점 간 거리(m) 행렬.

    **직선거리를 쓰면 안 됩니다.** 벽 하나를 사이에 둔 두 지점은 직선으로는
    2m 지만 실제로는 돌아서 15m 일 수 있고, 그 순서로 순찰하면 로봇이 공장을
    가로질러 왕복합니다.
    """
    warnings: List[str] = []
    factor = max(1, int(round(coarse_cell_m / grid.resolution)))
    passable = _coarse_passable(usable, factor)
    metres_per_unit = (grid.resolution * factor) / _STRAIGHT

    nodes = []
    for wp in waypoints:
        row, col = grid.cell_of(wp.x, wp.y)
        node = (min(max(row // factor, 0), passable.shape[0] - 1),
                min(max(col // factor, 0), passable.shape[1] - 1))
        if not passable[node]:
            # 원본 격자에서는 안전한 자리인데 성긴 격자에서 막힌 경우.
            # 좁은 통로에서 생깁니다. 그 칸만 열어 줍니다.
            near = _nearest_true(passable, node, max_radius=3)
            if near is not None:
                node = near
            else:
                passable[node] = True
        nodes.append(node)

    size = len(nodes)
    matrix = np.zeros((size, size), dtype=np.float64)
    for i, node in enumerate(nodes):
        field = _dijkstra(passable, node)
        for j, other in enumerate(nodes):
            if i == j:
                continue
            cost = int(field[other])
            if cost < 0:
                # 성긴 격자에서 끊긴 경우. 직선거리로 대신하되 사실을 남깁니다.
                straight = math.hypot(waypoints[i].x - waypoints[j].x,
                                      waypoints[i].y - waypoints[j].y)
                matrix[i, j] = straight * 1.5
                message = (f"{waypoints[i].zone}-{waypoints[j].zone} 구간의 실제 경로를 "
                           f"찾지 못해 직선거리로 대신했습니다.")
                if message not in warnings:
                    warnings.append(message)
            else:
                matrix[i, j] = cost * metres_per_unit

    # 다익스트라는 방향에 따라 미세하게 다를 수 있어 대칭으로 맞춥니다.
    matrix = np.minimum(matrix, matrix.T)
    return matrix, warnings


def _tour_length(order: Sequence[int], matrix: np.ndarray) -> float:
    return float(sum(matrix[order[i], order[(i + 1) % len(order)]]
                     for i in range(len(order))))


def order_waypoints(matrix: np.ndarray, start: int = 0) -> List[int]:
    """순회 순서를 정한다 (최근접 이웃 -> 2-opt 개선).

    순찰은 시작점으로 돌아오는 **닫힌 경로**이므로 순회 판매원 문제입니다.
    지점이 10개 남짓이라 완전탐색도 가능하지만, 지점 수를 늘려도 버티도록
    근사 알고리즘을 씁니다.
    """
    size = len(matrix)
    if size <= 2:
        return list(range(size))

    # 1) 최근접 이웃으로 초기 경로
    unvisited = set(range(size))
    unvisited.remove(start)
    order = [start]
    while unvisited:
        last = order[-1]
        nearest = min(unvisited, key=lambda j: matrix[last, j])
        order.append(nearest)
        unvisited.remove(nearest)

    # 2) 2-opt — 교차하는 구간을 뒤집어 길이를 줄인다
    for _ in range(MAX_TWO_OPT_PASSES):
        improved = False
        for i in range(1, size - 1):
            for j in range(i + 1, size):
                a, b = order[i - 1], order[i]
                c, d = order[j], order[(j + 1) % size]
                if b == d or a == c:
                    continue
                before = matrix[a, b] + matrix[c, d]
                after = matrix[a, c] + matrix[b, d]
                if after + 1e-9 < before:
                    order[i:j + 1] = reversed(order[i:j + 1])
                    improved = True
        if not improved:
            break
    return order


def enforce_spacing(waypoints: List[Waypoint],
                    min_spacing_m: float) -> Tuple[List[Waypoint], int]:
    """너무 붙어 있는 지점을 정리한다. (남긴 지점, 버린 개수)

    여유거리가 큰(= 더 안전한) 지점부터 남깁니다. 다만 **구역 하나가 통째로
    사라지는 것은 막습니다** — 순찰의 목적이 모든 구역을 도는 것이라, 간격
    규칙보다 구역 커버리지가 우선입니다.
    """
    if min_spacing_m <= 0 or len(waypoints) < 2:
        return list(waypoints), 0

    kept: List[Waypoint] = []
    for wp in sorted(waypoints, key=lambda w: -w.clearance_m):
        if all(math.hypot(wp.x - k.x, wp.y - k.y) >= min_spacing_m for k in kept):
            kept.append(wp)

    covered = {w.zone for w in kept}
    for wp in waypoints:
        if wp.zone not in covered:
            kept.append(wp)          # 간격을 어겨서라도 그 구역은 들른다
            covered.add(wp.zone)

    return kept, len(waypoints) - len(kept)


def _assign_yaw(waypoints: List[Waypoint]) -> None:
    """각 지점에서 **다음 지점을 바라보게** yaw 를 정한다.

    카메라가 진행 방향을 향하므로 이동 중에도 앞을 감시합니다. 마지막 지점은
    첫 지점을 향합니다(순찰은 순환).
    """
    size = len(waypoints)
    for i, wp in enumerate(waypoints):
        nxt = waypoints[(i + 1) % size]
        if size == 1 or (abs(nxt.x - wp.x) < 1e-6 and abs(nxt.y - wp.y) < 1e-6):
            wp.yaw = 0.0
        else:
            wp.yaw = math.atan2(nxt.y - wp.y, nxt.x - wp.x)


# ==========================================================================
# 메인
# ==========================================================================
def plan_patrol_route(grid: GridInfo, *,
                      robot_xy: Optional[Tuple[float, float]] = None,
                      zone_count: int = 3,
                      zone_names: Optional[Sequence[str]] = None,
                      zones: Optional[List[dict]] = None,
                      zone_raster: Optional[np.ndarray] = None,
                      min_clearance_m: float = DEFAULT_MIN_CLEARANCE_M,
                      area_per_point_m2: float = DEFAULT_AREA_PER_POINT_M2,
                      max_points_per_zone: int = DEFAULT_MAX_POINTS_PER_ZONE,
                      min_spacing_m: float = DEFAULT_MIN_SPACING_M,
                      blind_obstacles=None,
                      coarse_cell_m: float = DEFAULT_COARSE_CELL_M) -> PatrolPlan:
    """지도 한 장에서 순찰 경로를 만든다.

    zones/zone_raster 를 주면 그대로 씁니다(미니맵이 이미 나눠 둔 구역과 맞추기
    위해서). 주지 않으면 여기서 직접 나눕니다.
    """
    if grid is None:
        return PatrolPlan.failed("지도가 아직 준비되지 않았습니다.")

    if has_suspicious_free_border(grid):
        return PatrolPlan.failed(
            "지도 바깥 미탐사 영역이 자유 공간으로 읽혀 전체가 사각형으로 잡혔습니다. "
            "지도 YAML의 free_thresh를 0.196으로 고친 뒤 map_server/Nav2를 다시 시작하세요.")

    free = free_mask(grid)
    if not free.any():
        return PatrolPlan.failed("지도에 자유 공간이 없습니다.")

    clearance = clearance_map(grid, free)
    safe = clearance >= min_clearance_m
    if not safe.any():
        return PatrolPlan.failed(
            f"벽에서 {min_clearance_m:.2f}m 이상 떨어진 자리가 없습니다. "
            f"지도가 너무 좁거나 아직 덜 만들어졌습니다.")

    safe, carved = carve_blind_obstacles(grid, safe, blind_obstacles)
    if not safe.any():
        return PatrolPlan.failed(
            "낮은 장애물을 빼고 나니 설 자리가 없습니다. "
            "장애물을 치우고 기록을 비우세요(/blind_obstacle_control clear).")

    start_rc = None
    if robot_xy is not None:
        start_rc = _nearest_true(safe, grid.cell_of(*robot_xy))

    usable = reachable_mask(safe, start_rc)
    if not usable.any():
        return PatrolPlan.failed("로봇이 갈 수 있는 영역을 찾지 못했습니다.")

    warnings: List[str] = []
    if carved > 0:
        area = carved * grid.resolution * grid.resolution
        warnings.append(
            f"LiDAR 에 안 보이는 장애물 자리 {area:.1f}m² 를 후보에서 뺐습니다.")

    dropped = int(safe.sum() - usable.sum())
    if dropped > 0:
        area = dropped * grid.resolution * grid.resolution
        if area >= 1.0:
            warnings.append(
                f"로봇 위치에서 이어지지 않는 영역 {area:.1f}m² 를 제외했습니다 "
                f"(문이 닫혔거나 지도가 끊긴 곳).")

    # 구역: 받은 것이 있으면 그대로, 없으면 직접 나눈다
    if zones is None or zone_raster is None:
        zone_raster, zones = partition_zones(grid, usable, zone_count, zone_names)
        if not zones:
            return PatrolPlan.failed("구역을 나눌 만큼 공간이 넓지 않습니다.")

    # 지점 선정
    waypoints: List[Waypoint] = []
    for zone in zones:
        count = _points_for_zone(zone.get("area_m2", 0.0),
                                 area_per_point_m2, max_points_per_zone)
        picked = pick_zone_waypoints(grid, usable, zone_raster, zone, clearance, count)
        if not picked:
            warnings.append(f"{zone['name']} 구역에는 설 만한 자리가 없어 건너뛰었습니다.")
            continue
        waypoints.extend(picked)

    if not waypoints:
        return PatrolPlan.failed("순찰 지점을 하나도 만들지 못했습니다.")

    waypoints, merged = enforce_spacing(waypoints, min_spacing_m)
    if merged > 0:
        warnings.append(
            f"{min_spacing_m:.1f}m 안에 겹친 지점 {merged}개를 합쳤습니다.")

    if len(waypoints) == 1:
        _assign_yaw(waypoints)
        return PatrolPlan(waypoints, zones, 0.0,
                          warnings + ["순찰 지점이 하나뿐입니다 (구역이 좁습니다)."])


    matrix, distance_warnings = geodesic_distance_matrix(
        grid, usable, waypoints, coarse_cell_m)
    warnings.extend(distance_warnings)

    start_index = 0
    if robot_xy is not None:
        start_index = int(np.argmin([math.hypot(w.x - robot_xy[0], w.y - robot_xy[1])
                                     for w in waypoints]))

    order = order_waypoints(matrix, start_index)
    ordered = [waypoints[i] for i in order]
    total = _tour_length(order, matrix)
    _assign_yaw(ordered)

    return PatrolPlan(ordered, zones, total, warnings)


def plan_from_grid_msg(msg, **kwargs) -> PatrolPlan:
    """nav_msgs/OccupancyGrid 를 그대로 받는 편의 함수."""
    grid = GridInfo.from_grid_msg(msg)
    if grid is None:
        return PatrolPlan.failed("지도 메시지의 크기 정보가 올바르지 않습니다.")
    return plan_patrol_route(grid, **kwargs)
