#!/usr/bin/env python3
"""Cartographer 초기 지도 작성을 위한 감독형 자율 매핑 주행 노드.

Nav2 없이 /scan, SLAM 지도(/map), TF를 결합해 자유공간과 미탐사
경계를 찾고, 장애물을 팽창한 A* 경로의 근거리 waypoint를 따라간다.

지도 작성 중에는 사람이 로봇 옆에서 감시해야 하며, Nav2나 키보드 teleop과
동시에 실행하면 안 된다.


왜 이 노드가 갔던 곳을 왕복했는가
---------------------------------
예전 버전은 "앞이 막히면 더 넓어 보이는 쪽으로 1.25초 회전" 하는 것이 전부였다.
지나온 자리도, 아직 안 가본 자리도 전혀 기억하지 않는다. 그래서:

1. **개활지에서 방향이 무작위였다.** 좌우 거리 차가 0.15m 미만이거나 둘 다
   사거리 밖(inf)이면 `random.choice` 로 결정했다. 넓은 공장 한가운데서는 거의
   항상 이 경우라, 사실상 제자리 랜덤워크였다.
2. **회전 방향이 매번 새로 뽑혔다.** 1.25초 돌고 FORWARD 로 돌아온 뒤 앞이 여전히
   막혀 있으면 방향을 **다시** 골랐다. 좌 → 우 → 좌 로 뒤집히면 로봇은 벽 앞에서
   좌우로 흔들리기만 했다.
3. **다 본 구역을 떠날 이유가 없었다.** 앞이 뚫려 있는 한 계속 직진하므로, 이미
   훑은 통로를 몇 번이고 다시 지나갔다.

지금은 다음을 더한다.

- **미탐사 방향 선호** — /map 을 구독해, 후보 방향으로 가상의 레이를 쏴서 미탐사
  셀(-1)이 몇 개나 걸리는지 센다. 벽에 막히면 그 뒤는 세지 않는다(갈 수 없으므로).
- **방문 기억** — 지나온 자리를 굵은 격자(기본 0.5m)에 세어 둔다. 이미 여러 번
  지난 쪽은 점수를 깎는다.
- **회전 방향 고정 + 재조정** — 한 번 정한 회전 방향은 잠시 유지해 좌우 진동을
  막고, 반대로 같은 자리를 오래 맴돌면(REDIRECT/ESCAPE) 가장 안 가본 쪽으로
  적극적으로 방향을 튼다.
- **팽창 지도 A* + waypoint** — 도달 가능한 프런티어까지의 실제
  통로 경로를 만들고, 직선이 벽을 가로지르지 않는 waypoint만 따라간다.
- **원호 조향 + TF 회전 피드백** — 70도 미만은 전진하며 방향을
  바꾸고, 큰 각도만 제자리에서 돌되 yaw로 도달을 확인한다.
- **회전 상한·복구** — 장애물 앞에서 계속 돌지 않고, 뒤가 확인된
  경우만 짧게 후진한다. 앞뒤가 막히면 안전 정지한다.

정지 거리, 범퍼, scan 타임아웃은 모든 탐색 명령보다 우선한다.


왜 Nav2/explore_lite를 쓰지 않는가
------------------------------------
`config/explore_params.yaml` 이 m-explore/explore_lite 용으로 있긴 하다. 그쪽은
Nav2 전역 경로 계획까지 갖춘 정식 프런티어 탐색이지만 **Nav2 스택이 함께 떠야**
하고(이 스크립트는 Nav2를 끄고 실행하도록 되어 있다), 별도 패키지 설치가 필요하다.

이 노드는 현재 자유공간과 연결된 프런티어를 BFS로 고르고,
팽창 자유공간 안에서 A* 경로를 만든다. 다만 Nav2 costmap·behavior
tree를 갖춘 전역 탐색기는 아니므로, 복잡한 다방 구조의 커버리지가
반드시 필요하면 explore_lite를 쓰는 편이 더 적합하다.
"""

import json
import heapq
import math
import signal
from collections import deque
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import OccupancyGrid
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, QoSProfile, ReliabilityPolicy,
                       qos_profile_sensor_data)
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from turtlebot3_msgs.msg import SensorState

# occupancy grid 판정 임계값. 이 값 이상이면 벽/장애물로 본다.
OCC_THRESHOLD = 50

# 방향 점수를 낼 때 쏘는 레이 설정.
# 한 방향당 부채꼴로 5줄을 쏘고, 각 줄을 PROBE_STEP_M 간격으로 걸어간다.
# 줄을 늘리면 점수가 안정되지만 10Hz 타이머 안에서 도는 계산이라 이 정도로 둔다.
PROBE_RAY_FRACTIONS = (-1.0, -0.5, 0.0, 0.5, 1.0)   # probe_half_angle 에 대한 비율
PROBE_STEP_M = 0.15
PROBE_MIN_RANGE_M = 0.30      # 로봇 자기 자리는 세지 않는다

# 방문 벌점을 매길 때 들여다보는 전방 거리.
VISIT_PROBE_DISTANCES_M = (0.5, 1.0, 1.5)

# 탈출/재조정 때 훑어보는 후보 방향 개수 (전방위를 균등 분할).
ESCAPE_HEADING_COUNT = 12

# 이미 한 번 걸렸던 낮은 장애물(문턱·케이블 등) 방향에 주는 감점.
# 미탐사 셀 수(최대 75 남짓)보다 크게 잡아, 그 방향이 아무리 새로워 보여도
# 다시 들이받지 않게 합니다. LiDAR 로는 영영 안 보이는 것들이라 이 기억이
# 없으면 로봇은 같은 자리에 계속 걸립니다.
BLIND_OBSTACLE_PENALTY = 120.0

# 이 거리(m) 안에 낮은 장애물이 있으면 그 방향을 피합니다.
BLIND_OBSTACLE_AVOID_M = 0.9


# 방문 기억은 "그 칸에 몇 번 **들어왔는가**"를 센다. 시간(체류)이 아니라 진입 횟수다.
#
# 시간으로 세면 주행 속도에 딸려간다. 이 노드는 0.12m/s라도 0.5m 칸 하나를
# 지나는 데 4초 이상 걸린다 — 시간 기준이면 **처음 지나가는 칸도** 곧바로
# "여러 번 왔다"로 판정되어 버린다. 진입 횟수로 세면 속도와 무관하게
# "여기 세 번째 오는 길이다" 가 그대로 나온다.
#
# 한 자리에 멈춰 선 경우는 이 기억이 아니라 stuck_radius/stuck_timeout 이 잡는다.


def normalize_angle(angle: float) -> float:
    """각도를 -pi ~ pi 로 정규화한다."""
    return math.atan2(math.sin(angle), math.cos(angle))


def turn_exit_action(min_turn_done: bool, committed: bool,
                     front_clear: bool, deadline_reached: bool,
                     rear_clear: bool, backup_enabled: bool) -> str:
    """회전 상태의 다음 동작을 결정한다.

    장애물 앞에서 deadline이 끝났는데도 다시 같은 TURN을 시작하면
    제자리 회전이 무한 반복된다. 뒤가 확실히 안전할 때만 짧게
    후진하고, 뒤도 막혔으면 정지해 감독자가 장애물을 치우게 한다.
    ROS와 무관한 순수 함수로 두어 실제 상태 전이를 회귀 테스트한다.
    """
    if min_turn_done:
        if front_clear:
            return 'FORWARD'
        if committed:
            return 'BACKUP' if rear_clear and backup_enabled else 'BLOCKED_STOP'
    if not deadline_reached:
        return 'TURN'
    if front_clear:
        return 'FORWARD'
    if rear_clear and backup_enabled:
        return 'BACKUP'
    return 'BLOCKED_STOP'


def blocked_recovery_direction(left_distance: float, right_distance: float,
                               left_observed: bool, right_observed: bool,
                               minimum_clearance: float,
                               previous_direction: float) -> Optional[float]:
    """정지 상태에서 제자리 복구 회전을 해도 되는 방향을 고른다.

    ``inf``만 보고 빈 공간으로 판정하지 않는다. LDS 드라이버가
    해당 구간을 실제로 관측했는지도 같이 확인해야 한다. 양쪽이
    같이 넓으면 기존 회전 방향을 유지해 좌우 진동을 막는다.
    """
    candidates = []
    if left_observed and left_distance >= minimum_clearance:
        candidates.append((float(left_distance), 1.0))
    if right_observed and right_distance >= minimum_clearance:
        candidates.append((float(right_distance), -1.0))
    if not candidates:
        return None
    if len(candidates) == 2:
        left, right = candidates
        if (math.isinf(left[0]) and math.isinf(right[0])) or \
                abs(left[0] - right[0]) < 0.15:
            return 1.0 if previous_direction >= 0.0 else -1.0
    return max(candidates, key=lambda item: item[0])[1]


def quaternion_yaw(q) -> float:
    """ROS2 쿼터니언에서 yaw(라디안)만 뽑는다."""
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


class MapView:
    """OccupancyGrid 한 장을 월드 좌표로 조회하기 좋게 감싼 것.

    노드 밖에서도 만들 수 있게(= 테스트할 수 있게) 배열과 원점만 받는다.
    """

    def __init__(self, cells: np.ndarray, resolution: float,
                 origin_x: float, origin_y: float):
        self.cells = cells
        self.resolution = float(resolution)
        self.origin_x = float(origin_x)
        self.origin_y = float(origin_y)
        self.height, self.width = cells.shape

    @classmethod
    def from_grid_msg(cls, msg: OccupancyGrid) -> Optional["MapView"]:
        """형식이 깨진 메시지에서는 None 을 돌려준다 (주행은 계속돼야 한다)."""
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

    def cell_at(self, x: float, y: float) -> Optional[int]:
        """월드 좌표의 셀 값. 지도 **범위 밖이면 None**.

        범위 밖과 미탐사(-1)를 구분해서 돌려준다. 둘 다 "안 가본 곳"이지만,
        호출부가 각각 다르게 다룰 수 있어야 한다.
        """
        col = int(math.floor((x - self.origin_x) / self.resolution))
        row = int(math.floor((y - self.origin_y) / self.resolution))
        if 0 <= row < self.height and 0 <= col < self.width:
            return int(self.cells[row, col])
        return None

    def cell_index_at(self, x: float, y: float) -> Optional[Tuple[int, int]]:
        """월드 좌표를 (row, col)로 바꾼다. 지도 밖이면 None."""
        col = int(math.floor((x - self.origin_x) / self.resolution))
        row = int(math.floor((y - self.origin_y) / self.resolution))
        if 0 <= row < self.height and 0 <= col < self.width:
            return row, col
        return None

    def world_at(self, row: int, col: int) -> Tuple[float, float]:
        """셀 중심의 map 좌표를 반환한다."""
        return (self.origin_x + (float(col) + 0.5) * self.resolution,
                self.origin_y + (float(row) + 0.5) * self.resolution)


class VisitMemory:
    """지나온 자리를 굵은 격자에 세어 두는 기억.

    세는 것은 **진입 횟수**다 (체류 시간이 아니다 — 위 주석 참고). 셀 하나가
    로봇보다 훨씬 크므로(기본 0.5m), 정확한 궤적이 아니라 "이 근처를 몇 번째
    지나가나"를 본다.

    상한(cap)을 두는 이유는, 한 칸의 값이 무한정 커지면 그 방향이 영원히 금지
    구역이 되어 로봇이 지도의 한쪽 구석에 갇히기 때문이다.
    """

    def __init__(self, cell_size: float = 0.5, cap: int = 10):
        self.cell_size = float(cell_size)
        self.cap = int(cap)
        self.counts = {}

    def cell_of(self, x: float, y: float) -> Tuple[int, int]:
        """이 좌표가 속한 격자 칸. 호출부가 "칸이 바뀌었나"를 볼 때 쓴다."""
        return (int(math.floor(x / self.cell_size)),
                int(math.floor(y / self.cell_size)))

    def mark(self, x: float, y: float) -> None:
        key = self.cell_of(x, y)
        self.counts[key] = min(self.cap, self.counts.get(key, 0) + 1)

    def count(self, x: float, y: float) -> int:
        return self.counts.get(self.cell_of(x, y), 0)


def count_unknown_along(map_view: MapView, origin_x: float, origin_y: float,
                        heading: float, probe_range: float,
                        half_angle: float) -> int:
    """heading 방향 부채꼴에 미탐사 셀이 몇 개나 걸리는지 센다.

    **벽을 만나면 그 레이는 거기서 끝낸다.** 벽 뒤의 미탐사 영역은 이 방향으로
    가서는 못 보므로, 세면 로봇이 벽을 향해 계속 달려드는 원인이 된다.

    지도 범위 밖은 미탐사로 센다. Cartographer 의 /map 은 지금까지 본 영역만
    담으므로, 범위 밖은 실제로 아직 안 가본 곳이다.
    """
    if map_view is None or probe_range <= PROBE_MIN_RANGE_M:
        return 0

    unknown = 0
    for fraction in PROBE_RAY_FRACTIONS:
        angle = heading + fraction * half_angle
        dx, dy = math.cos(angle), math.sin(angle)
        distance = PROBE_MIN_RANGE_M
        while distance <= probe_range:
            cell = map_view.cell_at(origin_x + distance * dx,
                                    origin_y + distance * dy)
            if cell is None:
                unknown += 1          # 지도 밖 = 아직 안 가본 곳
            elif cell >= OCC_THRESHOLD:
                break                 # 벽. 이 뒤는 이 방향으로 못 간다
            elif cell < 0:
                unknown += 1          # 미탐사
            distance += PROBE_STEP_M
    return unknown


def score_heading(map_view: MapView, visits: VisitMemory,
                  origin_x: float, origin_y: float, heading: float,
                  probe_range: float, half_angle: float,
                  visit_weight: float) -> float:
    """이 방향이 얼마나 "새로운" 곳인지 점수화한다. 클수록 좋다.

    미탐사 셀 수에서 방문 벌점을 뺀다. 방문 벌점은 그 방향 앞쪽 몇 지점의
    방문 횟수를 합한 값이라, 이미 훑은 통로를 다시 고르지 않게 한다.
    """
    unknown = count_unknown_along(map_view, origin_x, origin_y, heading,
                                  probe_range, half_angle)
    if visits is None or visit_weight <= 0.0:
        return float(unknown)

    penalty = 0
    for distance in VISIT_PROBE_DISTANCES_M:
        penalty += visits.count(origin_x + distance * math.cos(heading),
                                origin_y + distance * math.sin(heading))
    return float(unknown) - visit_weight * float(penalty)


def _adjacent_to_unknown(cells: np.ndarray) -> np.ndarray:
    """각 셀이 상하좌우의 미탐사 셀(-1)을 이웃으로 갖는지 계산한다.

    지도 밖은 프런티어로 세지 않는다. Cartographer가 지도 영역을 넓히는 동안
    바깥 테두리를 전부 목표로 고르면 실제로는 갈 수 없는 방향을 쫓게 되기 때문이다.
    """
    unknown = cells < 0
    adjacent = np.zeros_like(unknown, dtype=bool)
    adjacent[1:, :] |= unknown[:-1, :]
    adjacent[:-1, :] |= unknown[1:, :]
    adjacent[:, 1:] |= unknown[:, :-1]
    adjacent[:, :-1] |= unknown[:, 1:]
    return adjacent


def _nearest_free_cell(free: np.ndarray, row: int, col: int,
                       max_radius: int = 8) -> Optional[Tuple[int, int]]:
    """로봇 위치가 아직 unknown으로 남았을 때 가장 가까운 자유 셀을 찾는다."""
    height, width = free.shape
    if 0 <= row < height and 0 <= col < width and free[row, col]:
        return row, col
    for radius in range(1, max_radius + 1):
        top, bottom = row - radius, row + radius
        left, right = col - radius, col + radius
        for candidate_row in (top, bottom):
            if 0 <= candidate_row < height:
                for candidate_col in range(max(0, left), min(width, right + 1)):
                    if free[candidate_row, candidate_col]:
                        return candidate_row, candidate_col
        for candidate_col in (left, right):
            if 0 <= candidate_col < width:
                for candidate_row in range(max(0, top + 1), min(height, bottom)):
                    if free[candidate_row, candidate_col]:
                        return candidate_row, candidate_col
    return None


def _inflate_obstacles(cells: np.ndarray, resolution: float,
                       clearance_m: float) -> np.ndarray:
    """점유 셀을 로봇 반경만큼 팽창한 마스크를 만든다."""
    occupied = cells >= OCC_THRESHOLD
    radius = max(0, int(math.ceil(clearance_m / resolution)))
    if radius == 0:
        return occupied.copy()
    inflated = occupied.copy()
    height, width = cells.shape
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            src_r0, src_r1 = max(0, -dy), min(height, height - dy)
            src_c0, src_c1 = max(0, -dx), min(width, width - dx)
            dst_r0, dst_r1 = src_r0 + dy, src_r1 + dy
            dst_c0, dst_c1 = src_c0 + dx, src_c1 + dx
            inflated[dst_r0:dst_r1, dst_c0:dst_c1] |= \
                occupied[src_r0:src_r1, src_c0:src_c1]
    return inflated


def _navigable_cells(map_view: MapView, clearance_m: float) -> np.ndarray:
    known_free = ((map_view.cells >= 0) &
                  (map_view.cells < OCC_THRESHOLD))
    return known_free & ~_inflate_obstacles(
        map_view.cells, map_view.resolution, clearance_m)


def _nearest_connected_navigable(
        map_view: MapView, navigable: np.ndarray, row: int, col: int,
        max_steps: int = 12) -> Optional[Tuple[int, int]]:
    """벽을 건너뛰지 않고 현재 자유공간에 연결된 안전 셀을 찾는다."""
    known_free = ((map_view.cells >= 0) &
                  (map_view.cells < OCC_THRESHOLD))
    seed = _nearest_free_cell(known_free, row, col, max_radius=max_steps)
    if seed is None:
        return None
    if navigable[seed]:
        return seed
    height, width = navigable.shape
    pending = deque([(seed[0], seed[1], 0)])
    seen = {seed}
    while pending:
        current_row, current_col, steps = pending.popleft()
        if steps >= max_steps:
            continue
        for next_row, next_col in (
                (current_row - 1, current_col),
                (current_row + 1, current_col),
                (current_row, current_col - 1),
                (current_row, current_col + 1)):
            candidate = (next_row, next_col)
            if (candidate in seen or
                    not (0 <= next_row < height and 0 <= next_col < width) or
                    not known_free[next_row, next_col]):
                continue
            if navigable[next_row, next_col]:
                return candidate
            seen.add(candidate)
            pending.append((next_row, next_col, steps + 1))
    return None


def segment_is_navigable(map_view: MapView, navigable: np.ndarray,
                         start: Tuple[float, float],
                         end: Tuple[float, float]) -> bool:
    """두 월드 좌표 사이 직선이 안전 셀만 지나는지 검사한다."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(1, int(math.ceil(distance / (map_view.resolution * 0.5))))
    for index in range(1, steps + 1):
        ratio = float(index) / float(steps)
        point_x = start[0] + (end[0] - start[0]) * ratio
        point_y = start[1] + (end[1] - start[1]) * ratio
        cell = map_view.cell_index_at(point_x, point_y)
        if cell is None or not navigable[cell]:
            return False
    return True


def plan_path_to_target(
        map_view: MapView, visits: VisitMemory,
        start_x: float, start_y: float, target_x: float, target_y: float,
        clearance_m: float = 0.18,
        route_visit_weight: float = 0.35) -> Optional[Tuple[Tuple[float, float], ...]]:
    """팽창 지도에서 방문 비용을 포함한 A* 경로를 월드 좌표로 반환한다."""
    start_index = map_view.cell_index_at(start_x, start_y)
    target_index = map_view.cell_index_at(target_x, target_y)
    if start_index is None or target_index is None:
        return None
    navigable = _navigable_cells(map_view, clearance_m)
    start = _nearest_connected_navigable(
        map_view, navigable, *start_index, max_steps=12)
    goal = _nearest_connected_navigable(
        map_view, navigable, *target_index, max_steps=12)
    if start is None or goal is None:
        return None

    height, width = navigable.shape
    cost = np.full((height, width), np.inf, dtype=np.float64)
    parent_row = np.full((height, width), -1, dtype=np.int32)
    parent_col = np.full((height, width), -1, dtype=np.int32)
    cost[start] = 0.0
    queue = [(0.0, 0.0, start[0], start[1])]
    while queue:
        _estimate, current_cost, row, col = heapq.heappop(queue)
        if current_cost > cost[row, col] + 1e-9:
            continue
        if (row, col) == goal:
            break
        for next_row, next_col in ((row - 1, col), (row + 1, col),
                                   (row, col - 1), (row, col + 1)):
            if not (0 <= next_row < height and 0 <= next_col < width):
                continue
            if not navigable[next_row, next_col]:
                continue
            wx, wy = map_view.world_at(next_row, next_col)
            revisit = visits.count(wx, wy) if visits is not None else 0
            next_cost = current_cost + 1.0 + route_visit_weight * revisit
            if next_cost + 1e-9 >= cost[next_row, next_col]:
                continue
            cost[next_row, next_col] = next_cost
            parent_row[next_row, next_col] = row
            parent_col[next_row, next_col] = col
            heuristic = abs(goal[0] - next_row) + abs(goal[1] - next_col)
            heapq.heappush(queue, (
                next_cost + heuristic, next_cost, next_row, next_col))

    if not math.isfinite(float(cost[goal])):
        return None
    cells = []
    cursor = goal
    while True:
        cells.append(cursor)
        if cursor == start:
            break
        row, col = cursor
        previous = (int(parent_row[row, col]), int(parent_col[row, col]))
        if previous[0] < 0 or previous[1] < 0:
            return None
        cursor = previous
    cells.reverse()
    return tuple(map_view.world_at(row, col) for row, col in cells)


def select_frontier_target(
        map_view: MapView, visits: VisitMemory, origin_x: float, origin_y: float,
        min_distance_m: float = 0.8, search_range_m: float = 8.0,
        gain_weight: float = 0.20, distance_weight: float = 1.0,
        visit_weight: float = 2.0,
        excluded: Sequence[Tuple[float, float]] = (),
        exclusion_radius_m: float = 0.9,
        clearance_m: float = 0.18) -> Optional[Tuple[float, float, float]]:

    if map_view is None or search_range_m <= 0.0:
        return None
    index = map_view.cell_index_at(origin_x, origin_y)
    if index is None:
        return None

    free = _navigable_cells(map_view, clearance_m)
    start = _nearest_connected_navigable(map_view, free, *index)
    if start is None:
        return None

    max_steps = max(1, int(math.ceil(search_range_m / map_view.resolution)))
    distances = np.full(free.shape, -1, dtype=np.int32)
    distances[start] = 0
    pending = deque([start])
    height, width = free.shape
    while pending:
        row, col = pending.popleft()
        step = int(distances[row, col])
        if step >= max_steps:
            continue
        for next_row, next_col in ((row - 1, col), (row + 1, col),
                                   (row, col - 1), (row, col + 1)):
            if (0 <= next_row < height and 0 <= next_col < width and
                    free[next_row, next_col] and distances[next_row, next_col] < 0):
                distances[next_row, next_col] = step + 1
                pending.append((next_row, next_col))

    min_steps = max(1, int(math.ceil(min_distance_m / map_view.resolution)))
    frontier = (free & _adjacent_to_unknown(map_view.cells) &
                (distances >= min_steps) & (distances <= max_steps))
    if not np.any(frontier):
        return None

    seen = np.zeros_like(frontier, dtype=bool)
    best = None
    for start_row, start_col in np.argwhere(frontier):
        start_row, start_col = int(start_row), int(start_col)
        if seen[start_row, start_col]:
            continue
        seen[start_row, start_col] = True
        component = [(start_row, start_col)]
        pending = deque([(start_row, start_col)])
        while pending:
            row, col = pending.popleft()
            for next_row, next_col in ((row - 1, col), (row + 1, col),
                                       (row, col - 1), (row, col + 1)):
                if (0 <= next_row < height and 0 <= next_col < width and
                        frontier[next_row, next_col] and not seen[next_row, next_col]):
                    seen[next_row, next_col] = True
                    pending.append((next_row, next_col))
                    component.append((next_row, next_col))

        target_row, target_col = min(
            component, key=lambda item: int(distances[item[0], item[1]]))
        target_x, target_y = map_view.world_at(target_row, target_col)
        if any(math.hypot(target_x - blocked_x, target_y - blocked_y) <= exclusion_radius_m
               for blocked_x, blocked_y in excluded):
            continue
        path_distance = float(distances[target_row, target_col]) * map_view.resolution

        information_gain = min(len(component), 80)
        revisit_penalty = (visits.count(target_x, target_y)
                           if visits is not None else 0)
        score = (gain_weight * information_gain -
                 distance_weight * path_distance -
                 visit_weight * revisit_penalty)
        if best is None or score > best[2]:
            best = (target_x, target_y, score)
    return best


class AutoMapper(Node):
    """LiDAR 기반 저속 장애물 회피 주행기 + 미탐사 방향 선호."""

    def __init__(self) -> None:
        super().__init__('auto_mapper')

        # --- 주행/안전 (예전과 동일) ---
        self.declare_parameter('linear_speed', 0.12)
        # LDS-03은 한 프레임을 주사하는 동안도 로봇이 돌므로 제자리
        # 회전을 과도하게 높이지 않고, 기존보다 빠른 0.40rad/s로 절충한다.
        self.declare_parameter('turn_speed', 0.40)
        self.declare_parameter('safe_distance', 0.35)
        self.declare_parameter('slow_distance', 0.60)
        # 로봇 모서리에 닿을 수 있는 비스듬한 장애물도 전방으로 본다.
        self.declare_parameter('front_half_angle_deg', 35.0)
        # 드라이버가 살아 있지만 전부 0/NaN인 깨진 스캔을 개활지로 오인하지 않는다.
        self.declare_parameter('min_valid_scan_points', 5)
        self.declare_parameter('turn_duration', 0.85)
        self.declare_parameter('scan_timeout', 0.75)
        # 실물 매핑 중에는 /cmd_vel 이 보이지 않을 때 원인을 바로 구분할 수
        # 있도록 상태 로그를 켤 수 있다. 기본값은 꺼서 평소 주행 로그를
        # 과도하게 늘리지 않는다.
        self.declare_parameter('diagnostic_status', False)
        # 실행 스크립트에서 600처럼 정수 초로 전달한다.
        self.declare_parameter('max_runtime_sec', 0)

        # 앞이 계속 막혀 있으면 turn_duration 을 넘겨서도 계속 돈다. 예전에는
        # 고정 시간만 돌고 FORWARD 로 나갔다가 다시 막혀 재회전 — 이때 방향이
        # 뒤집히면서 좌우 진동이 생겼다. 다만 무한히 돌지 않도록 상한을 둔다.
        self.declare_parameter('max_turn_duration', 3.0)
        # 180도 반환은 0.40rad/s에서 약 7.9초가 필요하다. 피드백으로
        # 목표 각도에 도달하면 즉시 끝내되, 이 시간 이상은 돌지 않는다.
        self.declare_parameter('max_goal_turn_duration', 8.5)
        # 연속 제자리 회전을 끊기 위해 회전 뒤에는 짧게 전진한다. 단, 전방이
        # 막히면 이 약속보다 LiDAR 안전정지가 항상 우선한다.
        self.declare_parameter('post_turn_forward_sec', 1.2)
        # 장애물 회전이 상한까지 갔는데도 앞이 막히면 360도 계속 돌지 않고,
        # 후방 스캔이 안전할 때만 짧게 물러나 회전 공간을 만든다.
        self.declare_parameter('backup_speed', 0.06)
        self.declare_parameter('backup_duration_sec', 0.8)
        self.declare_parameter('rear_safe_distance', 0.35)
        # 회전 상한 후 전방은 막혔고 안전 후진을 확정할 수 없을
        # 때 즉시 영구 정지하지 않고, 측면이 관측된 자유공간일 때만
        # 제한된 제자리 회전을 시도한다. 평행이동은 기존 안전 조건을 그대로
        # 지킨다. 복구가 안 되면 유한 횟수 후 다시 안전 정지한다.
        self.declare_parameter('blocked_retry_delay_sec', 2.0)
        self.declare_parameter('blocked_retry_turn_deg', 110.0)
        self.declare_parameter('blocked_retry_max_count', 2)
        self.declare_parameter('blocked_side_clearance', 0.40)
        self.declare_parameter('blocked_retry_reset_sec', 12.0)

        # --- 탐색 (여기서부터가 왕복을 줄이는 부분) ---
        self.declare_parameter('map_topic', '/map')
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_footprint')
        self.declare_parameter('probe_range', 2.5)        # 방향 점수를 볼 거리(m)
        self.declare_parameter('probe_half_angle_deg', 35.0)
        self.declare_parameter('visit_cell_size', 0.5)    # 방문 기억 격자(m)
        self.declare_parameter('visit_cap', 10)           # 한 칸의 진입 횟수 상한
        # TF가 격자 경계에서 흔들리는 것을 '재진입'으로 세지 않는다.
        self.declare_parameter('visit_entry_min_travel_m', 0.20)
        self.declare_parameter('visit_weight', 3.5)       # 0 이면 방문 기억 무시
        # 회전 방향을 이 시간 동안은 바꾸지 않는다 (좌-우-좌 진동 방지).
        self.declare_parameter('turn_commit_sec', 3.0)
        # 지금 칸에 이 횟수만큼 **들어온 적이 있으면** 방향을 다시 잡는다. 0 이면 끔.
        # 1은 첫 진입이므로 최소 2 이상이어야 의미가 있다.
        self.declare_parameter('revisit_threshold', 2)
        self.declare_parameter('redirect_cooldown_sec', 3.0)
        # 재조정은 "직진보다 이만큼 더 좋은 방향"이 있을 때만 한다.
        self.declare_parameter('redirect_margin', 10.0)
        # 이 반경(m) 안에서 이 시간(초) 넘게 못 벗어나면 갇힌 것으로 본다.
        self.declare_parameter('stuck_radius', 0.5)
        self.declare_parameter('stuck_timeout', 12.0)

        # 지역 점수만 보고 반응하지 않고, 실제 지도에서 "자유공간-미탐사공간"
        # 경계를 목표로 삼는다. Nav2 없이도 긴 복도를 되짚는 현상을 줄이기 위한
        # 가벼운 frontier pursuit 이다.
        self.declare_parameter('frontier_enabled', True)
        self.declare_parameter('frontier_replan_sec', 4.0)
        self.declare_parameter('frontier_min_distance_m', 1.0)
        self.declare_parameter('frontier_search_range_m', 10.0)
        self.declare_parameter('frontier_gain_weight', 0.20)
        self.declare_parameter('frontier_distance_weight', 1.0)
        self.declare_parameter('frontier_target_reached_m', 0.65)
        self.declare_parameter('frontier_heading_tolerance_deg', 8.0)
        self.declare_parameter('frontier_pivot_angle_deg', 70.0)
        self.declare_parameter('frontier_steer_gain', 1.2)
        self.declare_parameter('frontier_max_steer', 0.30)
        self.declare_parameter('frontier_arc_min_speed_ratio', 0.55)
        self.declare_parameter('frontier_failure_cooldown_sec', 90.0)
        self.declare_parameter('frontier_failure_radius_m', 1.4)
        self.declare_parameter('frontier_clearance_m', 0.18)
        self.declare_parameter('frontier_lookahead_m', 0.45)
        self.declare_parameter('frontier_path_replan_sec', 2.0)
        self.declare_parameter('frontier_route_visit_weight', 0.35)
        # 바퀴는 굴러도 목표와의 거리가 줄지 않으면 벽을 따라 원을 그리는
        # 중일 가능성이 높다. 위치만 보는 stuck 판단과 별도로 잡는다.
        self.declare_parameter('frontier_progress_min_m', 0.20)
        self.declare_parameter('frontier_progress_timeout_sec', 12.0)

        gp = self.get_parameter
        self.linear_speed = float(gp('linear_speed').value)
        self.turn_speed = float(gp('turn_speed').value)
        self.safe_distance = float(gp('safe_distance').value)
        self.slow_distance = float(gp('slow_distance').value)
        if self.linear_speed <= 0.0:
            raise ValueError('linear_speed must be positive')
        if self.turn_speed <= 0.0:
            raise ValueError('turn_speed must be positive')
        if self.safe_distance <= 0.0:
            raise ValueError('safe_distance must be positive')
        if self.slow_distance < self.safe_distance:
            raise ValueError('slow_distance must be >= safe_distance')
        self.front_half_angle_deg = min(
            90.0, max(5.0, float(gp('front_half_angle_deg').value)))
        self.min_valid_scan_points = max(
            1, int(gp('min_valid_scan_points').value))
        self.turn_duration = float(gp('turn_duration').value)
        self.scan_timeout = float(gp('scan_timeout').value)
        self.diagnostic_status = bool(gp('diagnostic_status').value)
        self.max_runtime_sec = float(gp('max_runtime_sec').value)
        self.max_turn_duration = max(self.turn_duration,
                                     float(gp('max_turn_duration').value))
        self.max_goal_turn_duration = max(
            self.max_turn_duration,
            float(gp('max_goal_turn_duration').value))
        self.post_turn_forward_sec = max(
            0.0, float(gp('post_turn_forward_sec').value))
        self.backup_speed = max(0.0, float(gp('backup_speed').value))
        self.backup_duration_sec = max(
            0.0, float(gp('backup_duration_sec').value))
        self.rear_safe_distance = max(
            self.safe_distance, float(gp('rear_safe_distance').value))
        self.blocked_retry_delay_sec = max(
            0.5, float(gp('blocked_retry_delay_sec').value))
        self.blocked_retry_turn_rad = math.radians(min(
            170.0, max(30.0, float(gp('blocked_retry_turn_deg').value))))
        self.blocked_retry_max_count = max(
            0, int(gp('blocked_retry_max_count').value))
        self.blocked_side_clearance = max(
            self.safe_distance, float(gp('blocked_side_clearance').value))
        self.blocked_retry_reset_sec = max(
            self.blocked_retry_delay_sec,
            float(gp('blocked_retry_reset_sec').value))

        self.map_frame = str(gp('map_frame').value)
        self.base_frame = str(gp('base_frame').value)
        self.probe_range = float(gp('probe_range').value)
        self.probe_half_angle = math.radians(float(gp('probe_half_angle_deg').value))
        self.visit_weight = float(gp('visit_weight').value)
        self.visit_entry_min_travel_m = max(
            0.05, float(gp('visit_entry_min_travel_m').value))
        self.turn_commit_sec = float(gp('turn_commit_sec').value)
        self.revisit_threshold = int(gp('revisit_threshold').value)
        self.redirect_cooldown_sec = float(gp('redirect_cooldown_sec').value)
        self.redirect_margin = float(gp('redirect_margin').value)
        self.stuck_radius = float(gp('stuck_radius').value)
        self.stuck_timeout = float(gp('stuck_timeout').value)
        self.frontier_enabled = bool(gp('frontier_enabled').value)
        self.frontier_replan_sec = max(1.0, float(gp('frontier_replan_sec').value))
        self.frontier_min_distance_m = max(
            0.1, float(gp('frontier_min_distance_m').value))
        self.frontier_search_range_m = max(
            self.frontier_min_distance_m + 0.1,
            float(gp('frontier_search_range_m').value))
        self.frontier_gain_weight = max(0.0, float(gp('frontier_gain_weight').value))
        self.frontier_distance_weight = max(
            0.0, float(gp('frontier_distance_weight').value))
        self.frontier_target_reached_m = max(
            0.1, float(gp('frontier_target_reached_m').value))
        self.frontier_heading_tolerance = math.radians(min(
            90.0, max(5.0, float(gp('frontier_heading_tolerance_deg').value))))
        self.frontier_pivot_angle = math.radians(min(
            170.0, max(30.0, float(gp('frontier_pivot_angle_deg').value))))
        self.frontier_steer_gain = max(
            0.1, float(gp('frontier_steer_gain').value))
        self.frontier_max_steer = max(
            0.05, float(gp('frontier_max_steer').value))
        self.frontier_arc_min_speed_ratio = min(
            1.0, max(0.1, float(gp('frontier_arc_min_speed_ratio').value)))
        self.frontier_failure_cooldown_sec = max(
            self.frontier_replan_sec,
            float(gp('frontier_failure_cooldown_sec').value))
        self.frontier_failure_radius_m = max(
            0.1, float(gp('frontier_failure_radius_m').value))
        self.frontier_clearance_m = max(
            0.05, float(gp('frontier_clearance_m').value))
        self.frontier_lookahead_m = max(
            0.15, float(gp('frontier_lookahead_m').value))
        self.frontier_path_replan_sec = max(
            0.5, float(gp('frontier_path_replan_sec').value))
        self.frontier_route_visit_weight = max(
            0.0, float(gp('frontier_route_visit_weight').value))
        self.frontier_progress_min_m = max(
            0.05, float(gp('frontier_progress_min_m').value))
        self.frontier_progress_timeout_sec = max(
            self.redirect_cooldown_sec,
            float(gp('frontier_progress_timeout_sec').value))

        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.create_subscription(LaserScan, '/scan', self._scan_callback,
                                 qos_profile_sensor_data)
        self.create_subscription(SensorState, '/sensor_state',
                                 self._sensor_state_callback,
                                 qos_profile_sensor_data)
        self.create_subscription(OccupancyGrid, str(gp('map_topic').value),
                                 self._map_callback, 1)
        # stall_monitor 는 TRANSIENT_LOCAL 로 발행합니다(늦게 떠도 마지막 목록을
        # 받아야 하므로). 받는 쪽도 같은 durability 여야 합니다.
        blind_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(String, '/blind_obstacles',
                                 self._blind_callback, blind_qos)
        # stall_monitor가 "바퀴 자체가 돌지 않는다"고 판단하면 이를 장애물로
        # 취급하지 않는다. 그 상태에서 계속 회전/재탐색하면 지도와 원인 분석만
        # 더 망가진다. 즉시 정지해 사람이 OpenCR/DYNAMIXEL을 확인하게 한다.
        self.create_subscription(String, '/actuation_fault',
                                 self._actuation_fault_callback, blind_qos)
        self.timer = self.create_timer(0.1, self._tick)

        # SLAM 이 주는 로봇 위치. Cartographer 가 map -> base_footprint 를 채운다.
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.last_scan_time = None
        self.scan_is_valid = False
        self.invalid_scan_warned = False
        self.front_distance = math.inf
        self.left_distance = math.inf
        self.right_distance = math.inf
        self.rear_distance = math.inf
        self.left_scan_observed = False
        self.right_scan_observed = False
        self.rear_scan_observed = False
        self.state = 'WAIT_FOR_SCAN'
        self.turn_direction = 1.0
        self.turn_end_sec = None       # 이 시각까지는 최소한 돈다
        self.turn_deadline_sec = None  # 앞이 계속 막혀도 이 시각에는 그만 돈다
        self.turn_is_committed = False  # 재조정/탈출 회전은 각도를 다 채운다
        self.turn_target_heading = None
        self.forward_commit_until_sec = 0.0
        self.backup_end_sec = None
        self.blocked_since_sec = None
        self.blocked_episode_sec = None
        self.blocked_clear_since_sec = None
        self.blocked_retry_count = 0
        self.last_blocked_log_sec = -math.inf
        self.progress_pause_started_sec = None
        self.started_at = self.get_clock().now()
        self.emergency_stop = False
        self.actuation_fault = False

        self.map_view = None
        # stall_monitor 가 찾은 낮은 장애물. LiDAR 에 안 보이므로 map_view 에는
        # 없습니다. 이것만 별도로 들고 있다가 방향 점수에서 뺍니다.
        self.blind_obstacles = []
        self.visits = VisitMemory(float(gp('visit_cell_size').value),
                                  int(gp('visit_cap').value))
        self.pose = None               # (x, y, yaw) — map 좌표계
        self.pose_warned = False
        self.last_cell = None          # 마지막으로 있던 방문 격자 칸
        self.last_visit_mark_xy = None
        # (cell, visit_count). 한 번 처리한 재방문을 회전 중 매 tick 다시 처리하면
        # 목표를 계속 갈아치우며 제자리에서 도는 원인이 된다.
        self.last_handled_revisit = None
        self.last_eval_sec = 0.0       # _best_heading() 재계산 스로틀
        self.last_turn_end_sec = None
        self.last_redirect_sec = None
        self.anchor_xy = None          # 갇힘 판정용 기준점
        self.anchor_sec = 0.0
        self.frontier_target = None     # (x, y, score): 현재 추적 중인 미탐사 경계
        self.frontier_failures = []     # (x, y, 만료시각): 막혀 포기한 목표
        self.last_frontier_plan_sec = -math.inf
        self.frontier_best_distance_m = math.inf
        self.last_frontier_progress_sec = None
        self.frontier_path = ()
        self.frontier_path_index = 0
        self.last_frontier_path_plan_sec = -math.inf
        self.frontier_best_path_remaining_m = math.inf
        self.last_diagnostic_sec = 0.0

        self.get_logger().info(
            'Auto mapper ready: supervised low-speed mapping with '
            'reachable-frontier preference. Stop with Ctrl+C; '
            'do not run Nav2 or teleop at the same time.')

    # ------------------------------------------------------------------
    # 센서 콜백
    # ------------------------------------------------------------------
    @staticmethod
    def _valid_ranges(msg: LaserScan, start_deg: float,
                      end_deg: float) -> Iterable[float]:
        """Return finite sensor values inside a sector relative to robot front."""
        lower = math.radians(start_deg)
        upper = math.radians(end_deg)
        minimum = max(float(msg.range_min), 0.05)
        maximum = float(msg.range_max)

        for index, distance in enumerate(msg.ranges):
            raw_angle = msg.angle_min + index * msg.angle_increment
            # LDS-03처럼 0~2pi로 발행하는 scan도 전방 오른쪽을 음수 각도로
            # 판정할 수 있도록 항상 -pi~pi로 정규화한다.
            angle = normalize_angle(raw_angle)
            if lower <= angle <= upper and math.isfinite(distance):
                if minimum < distance < maximum:
                    yield distance

    @classmethod
    def _sector_min(cls, msg: LaserScan, start_deg: float,
                    end_deg: float) -> float:
        values = list(cls._valid_ranges(msg, start_deg, end_deg))
        return min(values) if values else math.inf

    @staticmethod
    def _sector_observed(msg: LaserScan, start_deg: float,
                         end_deg: float) -> bool:
        """해당 구간에 유효 반환값 또는 '+inf=물체 없음'이 있는지 본다."""
        lower = math.radians(start_deg)
        upper = math.radians(end_deg)
        minimum = max(float(msg.range_min), 0.05)
        maximum = float(msg.range_max)
        for index, distance in enumerate(msg.ranges):
            angle = normalize_angle(msg.angle_min + index * msg.angle_increment)
            if not (lower <= angle <= upper):
                continue
            if math.isinf(distance) and distance > 0.0:
                return True
            if math.isfinite(distance) and minimum < distance <= maximum:
                return True
        return False

    def _scan_callback(self, msg: LaserScan) -> None:
        # 전방과 측면 구간을 조금 겹치게 둔다. 예전 ±25°/35° 시작에는
        # 25~35° 사각이 있어 로봇 모서리가 비스듬한 장애물에 닿을 수 있었다.
        half = self.front_half_angle_deg
        self.front_distance = self._sector_min(msg, -half, half)
        self.left_distance = self._sector_min(msg, 30.0, 110.0)
        self.right_distance = self._sector_min(msg, -110.0, -30.0)
        self.rear_distance = min(
            self._sector_min(msg, 145.0, 180.0),
            self._sector_min(msg, -180.0, -145.0))
        self.rear_scan_observed = (
            self._sector_observed(msg, 145.0, 180.0) or
            self._sector_observed(msg, -180.0, -145.0))
        self.left_scan_observed = self._sector_observed(msg, 30.0, 110.0)
        self.right_scan_observed = self._sector_observed(msg, -110.0, -30.0)
        minimum = max(float(msg.range_min), 0.05)
        maximum = float(msg.range_max)
        valid_count = sum(
            1 for distance in msg.ranges
            if math.isfinite(distance) and minimum < distance < maximum)
        self.scan_is_valid = valid_count >= self.min_valid_scan_points
        if self.scan_is_valid:
            self.invalid_scan_warned = False
        self.last_scan_time = self.get_clock().now()

    def _map_callback(self, msg: OccupancyGrid) -> None:
        view = MapView.from_grid_msg(msg)
        if view is not None:
            self.map_view = view

    def _blind_callback(self, msg: String) -> None:
        """stall_monitor 가 알려준 낮은 장애물 목록."""
        try:
            items = json.loads(msg.data)["obstacles"]
            if not isinstance(items, list):
                raise TypeError
        except (KeyError, TypeError, ValueError):
            self.get_logger().warn('형식이 잘못된 /blind_obstacles 메시지를 건너뜁니다.')
            return
        points = []
        for item in items:
            try:
                points.append((float(item["x"]), float(item["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        self.blind_obstacles = points
        if points:
            self.get_logger().info(
                f'LiDAR 에 안 보이는 장애물 {len(points)}곳을 피하겠습니다.')

    def _actuation_fault_callback(self, msg: String) -> None:
        """OpenCR/DYNAMIXEL 미구동은 장애물 회피가 아니라 안전 정지 대상이다."""
        if self.actuation_fault:
            return
        try:
            reason = str(json.loads(msg.data).get('reason', '')).strip()
        except (TypeError, ValueError):
            reason = msg.data.strip()
        self.actuation_fault = True
        detail = f' ({reason})' if reason else ''
        self.get_logger().error(
            'Actuation fault reported; stopping mapping instead of recording a blind obstacle.'
            + detail)
        self._stop()
        if rclpy.ok():
            rclpy.shutdown()

    def _blind_penalty(self, heading: float) -> float:
        """이 방향 앞에 이미 걸렸던 자리가 있으면 감점.

        레이 위 몇 지점을 찍어 낮은 장애물과의 거리를 봅니다. 정밀할 필요는
        없습니다 — "저쪽으로 가지 마라" 만 전달하면 됩니다.
        """
        if not self.blind_obstacles or self.pose is None:
            return 0.0
        x, y, _yaw = self.pose
        penalty = 0.0
        for distance in (0.4, 0.8, 1.2, 1.6):
            probe_x = x + distance * math.cos(heading)
            probe_y = y + distance * math.sin(heading)
            for bx, by in self.blind_obstacles:
                if math.hypot(probe_x - bx, probe_y - by) <= BLIND_OBSTACLE_AVOID_M:
                    penalty += BLIND_OBSTACLE_PENALTY
                    break
        return penalty

    def _sensor_state_callback(self, msg: SensorState) -> None:
        if msg.bumper and not self.emergency_stop:
            self.emergency_stop = True
            self.get_logger().error('Bumper pressed; emergency stop.')
            self._stop()
            # 셸 실행기가 지도/최종 pose 저장 단계로 넘어가게 노드를 종료한다.
            if rclpy.ok():
                rclpy.shutdown()

    # ------------------------------------------------------------------
    # 위치 / 방문 기억
    # ------------------------------------------------------------------
    def _update_pose(self, now_sec: float) -> None:
        """TF 로 map -> base_frame 을 조회해 현재 위치를 갱신한다.

        TF 가 아직 없으면 조용히 넘어간다. 위치를 모르면 미탐사/방문 판단만
        빠지고, 예전과 같은 순수 반응형 주행으로 동작한다 — 멈추지는 않는다.
        """
        try:
            tf = self.tf_buffer.lookup_transform(
                self.map_frame, self.base_frame, rclpy.time.Time())
        except TransformException as exc:
            if not self.pose_warned:
                self.pose_warned = True
                self.get_logger().warn(
                    f'{self.map_frame} -> {self.base_frame} TF not available yet '
                    f'({exc}); driving without exploration memory.')
            return

        if self.pose_warned:
            self.pose_warned = False
            self.get_logger().info('Robot pose acquired; exploration memory active.')

        x = float(tf.transform.translation.x)
        y = float(tf.transform.translation.y)
        yaw = quaternion_yaw(tf.transform.rotation)
        self.pose = (x, y, yaw)
        self._update_frontier_progress(now_sec)

        # 칸이 바뀔 때만 센다 (체류 시간이 아니라 진입 횟수 — 위 주석 참고).
        cell = self.visits.cell_of(x, y)
        travelled_since_mark = (
            math.inf if self.last_visit_mark_xy is None else
            math.hypot(x - self.last_visit_mark_xy[0],
                       y - self.last_visit_mark_xy[1]))
        if (cell != self.last_cell and
                travelled_since_mark >= self.visit_entry_min_travel_m):
            self.last_cell = cell
            self.last_visit_mark_xy = (x, y)
            self.visits.mark(x, y)

        if self.anchor_xy is None:
            self.anchor_xy, self.anchor_sec = (x, y), now_sec
        elif math.hypot(x - self.anchor_xy[0], y - self.anchor_xy[1]) > self.stuck_radius:
            self.anchor_xy, self.anchor_sec = (x, y), now_sec

    def _score(self, heading: float) -> float:
        x, y, _yaw = self.pose
        base = score_heading(self.map_view, self.visits, x, y, heading,
                             self.probe_range, self.probe_half_angle,
                             self.visit_weight)
        # LiDAR 에 안 보이는 장애물은 map_view 에 없으므로 여기서 따로 뺍니다.
        return base - self._blind_penalty(heading)

    def _best_heading(self) -> Optional[Tuple[float, float]]:
        """전방위 후보 중 가장 점수가 높은 (heading, score). 판단 불가면 None."""
        if self.pose is None or self.map_view is None:
            return None
        _x, _y, yaw = self.pose
        best = None
        for index in range(ESCAPE_HEADING_COUNT):
            heading = normalize_angle(yaw + 2.0 * math.pi * index / ESCAPE_HEADING_COUNT)
            score = self._score(heading)
            if best is None or score > best[1]:
                best = (heading, score)
        return best

    # ------------------------------------------------------------------
    # 프런티어 목표 선택 / 추적
    # ------------------------------------------------------------------
    def _prune_frontier_failures(self, now_sec: float) -> None:
        self.frontier_failures = [
            item for item in self.frontier_failures if item[2] > now_sec]

    def _set_frontier_target(self, target: Tuple[float, float, float],
                             now_sec: float) -> None:
        """새 목표를 기록하고, 이후 거리 감소를 진행 여부로 감시한다."""
        self.frontier_target = target
        if self.pose is None:
            self.frontier_best_distance_m = math.inf
        else:
            self.frontier_best_distance_m = math.hypot(
                target[0] - self.pose[0], target[1] - self.pose[1])
        self.last_frontier_progress_sec = now_sec
        self.frontier_path = ()
        self.frontier_path_index = 0
        self.last_frontier_path_plan_sec = -math.inf
        self.frontier_best_path_remaining_m = math.inf

    def _update_frontier_progress(self, now_sec: float) -> None:
        """목표 거리가 의미 있게 줄었을 때만 진행 시간표를 갱신한다."""
        if self.frontier_target is None or self.pose is None:
            return
        # 경로가 있으면 유클리드 직선거리 대신 아래의 남은 A* 경로 길이로 본다.
        # L/U자 복도에서는 올바른 우회 중 잠시 목표와 멀어질 수 있기 때문이다.
        if self.frontier_path:
            return
        distance = math.hypot(self.frontier_target[0] - self.pose[0],
                              self.frontier_target[1] - self.pose[1])
        if distance <= self.frontier_best_distance_m - self.frontier_progress_min_m:
            self.frontier_best_distance_m = distance
            self.last_frontier_progress_sec = now_sec

    def _refresh_frontier_path(self, now_sec: float, force: bool = False) -> bool:
        if self.frontier_target is None or self.pose is None or self.map_view is None:
            return False
        if (self.frontier_path and not force and
                now_sec - self.last_frontier_path_plan_sec <
                self.frontier_path_replan_sec):
            return True
        path = plan_path_to_target(
            self.map_view, self.visits,
            self.pose[0], self.pose[1],
            self.frontier_target[0], self.frontier_target[1],
            clearance_m=self.frontier_clearance_m,
            route_visit_weight=self.frontier_route_visit_weight)
        self.last_frontier_path_plan_sec = now_sec
        if not path:
            self.frontier_path = ()
            self.frontier_path_index = 0
            return False
        self.frontier_path = path
        self.frontier_path_index = 0
        return True

    def _path_lookahead(self, now_sec: float) -> Optional[Tuple[float, float]]:
        """현재 A* 경로에서 벽을 가로지르지 않는 근거리 추종점을 고른다."""
        if not self._refresh_frontier_path(now_sec):
            return None
        x, y, _yaw = self.pose
        path = self.frontier_path
        start = min(self.frontier_path_index, len(path) - 1)
        search_end = min(len(path), start + 60)
        nearest = min(
            range(start, search_end),
            key=lambda index: math.hypot(path[index][0] - x, path[index][1] - y))
        self.frontier_path_index = nearest

        navigable = _navigable_cells(
            self.map_view, self.frontier_clearance_m)
        # 실물 로봇이 벽 가까이에서 시작하면 현재 셀이 팽창 영역일
        # 수 있다. 출발 셀만 허용하고 그 다음부터는 안전 여유를 지킨다.
        current_cell = self.map_view.cell_index_at(x, y)
        if (current_cell is not None and
                0 <= self.map_view.cells[current_cell] < OCC_THRESHOLD):
            navigable[current_cell] = True

        distance_along = 0.0
        previous = (x, y)
        lookahead = None
        for index in range(nearest, len(path)):
            point = path[index]
            distance_along += math.hypot(point[0] - previous[0],
                                         point[1] - previous[1])
            if not segment_is_navigable(
                    self.map_view, navigable, (x, y), point):
                break
            lookahead = point
            previous = point
            if distance_along >= self.frontier_lookahead_m:
                break
        if lookahead is None:
            return None

        # 목표 진행은 직선거리가 아니라 남은 경로 길이로 판단한다.
        path_remaining = math.hypot(
            path[nearest][0] - x, path[nearest][1] - y)
        previous = path[nearest]
        for point in path[nearest + 1:]:
            path_remaining += math.hypot(point[0] - previous[0],
                                         point[1] - previous[1])
            previous = point
        if (path_remaining <= self.frontier_best_path_remaining_m -
                self.frontier_progress_min_m):
            self.frontier_best_path_remaining_m = path_remaining
            self.last_frontier_progress_sec = now_sec
        return lookahead

    def _frontier_is_stalled(self, now_sec: float) -> bool:
        return bool(
            self.frontier_target is not None and
            self.last_frontier_progress_sec is not None and
            now_sec - self.last_frontier_progress_sec >=
            self.frontier_progress_timeout_sec)

    def _abandon_frontier_target(self, now_sec: float, reason: str) -> None:
        """계속 못 가는 목표를 잠시 제외해 같은 복도를 재시도하지 않게 한다."""
        if self.frontier_target is None:
            return
        x, y, _score = self.frontier_target
        self.frontier_failures.append(
            (x, y, now_sec + self.frontier_failure_cooldown_sec))
        self.get_logger().warn(
            f'Frontier target abandoned ({reason}); selecting another area.')
        self.frontier_target = None
        self.frontier_best_distance_m = math.inf
        self.last_frontier_progress_sec = None
        self.frontier_path = ()
        self.frontier_path_index = 0
        self.frontier_best_path_remaining_m = math.inf

    def _plan_frontier(self, now_sec: float, force: bool = False) -> Optional[Tuple[float, float, float]]:
        """필요할 때만 다음 프런티어를 고르고, 목표는 도달/실패 전까지 유지한다."""
        if not self.frontier_enabled or self.pose is None or self.map_view is None:
            return None
        self._prune_frontier_failures(now_sec)

        if self.frontier_target is not None:
            x, y, _yaw = self.pose
            distance = math.hypot(self.frontier_target[0] - x,
                                  self.frontier_target[1] - y)
            if distance <= self.frontier_target_reached_m:
                self.frontier_target = None
                self.frontier_best_distance_m = math.inf
                self.last_frontier_progress_sec = None
                self.frontier_path = ()
                self.frontier_path_index = 0
                self.frontier_best_path_remaining_m = math.inf
            elif not force:
                # 라이다가 멀리서 본 한 프레임만으로 경계 셀은 free/unknown에서
                # free/occupied로 자주 바뀐다. 그때마다 목표를 취소하면 실제로
                # 하단 출입구 같은 미탐사 방향까지 가지 못하고 제자리에서 다시
                # 고르는 현상이 생긴다. 목표 근처까지는 유지하고, 벽이면 전방
                # LiDAR 안전정지 → stuck/revisit 판단이 목표를 포기하게 한다.
                return self.frontier_target
            else:
                self.frontier_target = None

        if (not force and
                now_sec - self.last_frontier_plan_sec < self.frontier_replan_sec):
            return None
        self.last_frontier_plan_sec = now_sec
        x, y, _yaw = self.pose
        excluded = [(item[0], item[1]) for item in self.frontier_failures]
        target = select_frontier_target(
            self.map_view, self.visits, x, y,
            min_distance_m=self.frontier_min_distance_m,
            search_range_m=self.frontier_search_range_m,
            gain_weight=self.frontier_gain_weight,
            distance_weight=self.frontier_distance_weight,
            visit_weight=self.visit_weight,
            excluded=excluded,
            exclusion_radius_m=self.frontier_failure_radius_m,
            clearance_m=self.frontier_clearance_m)
        if target is not None:
            self._set_frontier_target(target, now_sec)
            self.get_logger().info(
                f'Frontier goal: ({target[0]:.2f}, {target[1]:.2f}), '
                f'score={target[2]:.1f}.')
        return self.frontier_target

    def _frontier_motion(self, now_sec: float) -> Optional[Tuple[float, float]]:
        """프런티어를 향한 ``(전진속도, 회전속도)``를 만든다.

        예전에는 목표 오차가 25도만 넘어도 매번 정지 후 제자리 회전을 시작했다.
        지도/TF의 작은 흔들림에도 TURN 상태가 연속되어 "빙글빙글" 보였다. 이제
        70도 미만 오차는 전진하면서 원호로 보정하고, 정말 뒤쪽 목표일 때만
        제자리 회전한다.
        """
        target = self._plan_frontier(now_sec)
        if target is None or self.pose is None:
            return None
        lookahead = self._path_lookahead(now_sec)
        if lookahead is None:
            self._abandon_frontier_target(now_sec, 'no safe path')
            target = self._plan_frontier(now_sec, force=True)
            if target is None:
                return None
            lookahead = self._path_lookahead(now_sec)
            if lookahead is None:
                self._abandon_frontier_target(now_sec, 'no safe path')
                return None
        x, y, yaw = self.pose
        heading = math.atan2(lookahead[1] - y, lookahead[0] - x)
        delta = normalize_angle(heading - yaw)
        if abs(delta) >= self.frontier_pivot_angle:
            self._begin_turn_towards(now_sec, heading, 'Frontier goal')
            return None
        if abs(delta) < self.frontier_heading_tolerance:
            return self.linear_speed, 0.0

        angular = max(-self.frontier_max_steer,
                      min(self.frontier_max_steer,
                          self.frontier_steer_gain * delta))
        error_ratio = min(1.0, abs(delta) / self.frontier_pivot_angle)
        speed_ratio = max(
            self.frontier_arc_min_speed_ratio,
            1.0 - error_ratio * (1.0 - self.frontier_arc_min_speed_ratio))
        return self.linear_speed * speed_ratio, angular

    # ------------------------------------------------------------------
    # 회전 결정
    # ------------------------------------------------------------------
    def _pause_progress_timers(self, now_sec: float) -> None:
        """제자리 회전을 '목표로 전진하지 못한 시간'으로 세지 않는다."""
        if self.progress_pause_started_sec is None:
            self.progress_pause_started_sec = now_sec

    def _resume_progress_timers(self, now_sec: float) -> None:
        if self.progress_pause_started_sec is None:
            return
        paused = max(0.0, now_sec - self.progress_pause_started_sec)
        if self.last_frontier_progress_sec is not None:
            self.last_frontier_progress_sec += paused
        if self.anchor_xy is not None:
            self.anchor_sec += paused
        self.progress_pause_started_sec = None

    def _rear_is_clear(self) -> bool:
        # 후방 스캔 자체가 없는데 inf만 나온 것을 개활지로
        # 오인하여 후진하지 않는다.
        return (self.rear_scan_observed and
                self.rear_distance >= self.rear_safe_distance)

    @staticmethod
    def _distance_text(distance: float, observed: bool = True) -> str:
        if not observed:
            return 'unobserved'
        return 'open' if math.isinf(distance) else f'{distance:.2f}m'

    def _begin_blocked_recovery_turn(self, now_sec: float,
                                     direction: float) -> None:
        """평행이동 없이 측면 자유공간으로만 한정 회전한다."""
        self.turn_direction = 1.0 if direction >= 0.0 else -1.0
        self.turn_end_sec = now_sec + 0.25
        self.turn_deadline_sec = now_sec + (
            self.blocked_retry_turn_rad / max(self.turn_speed, 1e-3))
        self.turn_is_committed = False
        self.turn_target_heading = None
        self.state = 'TURN'
        self._pause_progress_timers(now_sec)
        side = 'left' if self.turn_direction > 0.0 else 'right'
        self.get_logger().warn(
            f'Blocked recovery {self.blocked_retry_count}/'
            f'{self.blocked_retry_max_count}: rotating {side} in place; '
            'translation remains disabled until a safe direction is visible.')

    def _enter_blocked_stop(self, now_sec: float) -> None:
        self.state = 'BLOCKED_STOP'
        self.turn_target_heading = None
        self.turn_is_committed = False
        self.last_turn_end_sec = now_sec
        self.blocked_since_sec = now_sec
        self.blocked_clear_since_sec = None
        if self.blocked_episode_sec is None:
            self.blocked_episode_sec = now_sec
            self.blocked_retry_count = 0
        self.last_blocked_log_sec = now_sec
        self.get_logger().error(
            'Safe translation unavailable after turn limit; holding position '
            f'(front={self._distance_text(self.front_distance)}, '
            f'rear={self._distance_text(self.rear_distance, self.rear_scan_observed)}, '
            f'left={self._distance_text(self.left_distance, self.left_scan_observed)}, '
            f'right={self._distance_text(self.right_distance, self.right_scan_observed)}).')

    def _choose_turn_direction(self, now_sec: float) -> float:
        # 1) 방금 돌았다면 같은 방향을 유지한다. 이게 좌-우-좌 진동을 막는다.
        if (self.last_turn_end_sec is not None and
                now_sec - self.last_turn_end_sec < self.turn_commit_sec):
            return self.turn_direction

        # 2) 추적 중인 A* 경로가 있으면 다음 waypoint 쪽으로 돈다.
        if self.pose is not None and self.frontier_path:
            lookahead = self._path_lookahead(now_sec)
            if lookahead is not None:
                x, y, yaw = self.pose
                delta = normalize_angle(
                    math.atan2(lookahead[1] - y, lookahead[0] - x) - yaw)
                if abs(delta) >= math.radians(10.0):
                    return 1.0 if delta > 0.0 else -1.0

        # 3) 지도와 위치를 알면 미탐사가 많은 쪽으로 돈다.
        if self.pose is not None and self.map_view is not None:
            _x, _y, yaw = self.pose
            left = self._score(normalize_angle(yaw + math.pi / 2.0))
            right = self._score(normalize_angle(yaw - math.pi / 2.0))
            if abs(left - right) >= 1.0:
                return 1.0 if left > right else -1.0

        # 4) 지도가 아직 없거나 양쪽이 같으면 넓은 쪽으로. 완전 동률에서도
        # random으로 방향을 계속 뒤집지 않고 직전 방향을 유지한다.
        if math.isinf(self.left_distance) and math.isinf(self.right_distance):
            return self.turn_direction
        if abs(self.left_distance - self.right_distance) < 0.15:
            return self.turn_direction
        return 1.0 if self.left_distance > self.right_distance else -1.0

    def _begin_turn(self, now_sec: float) -> None:
        """장애물 회피 회전. 앞이 뚫릴 때까지(상한 안에서) 같은 방향으로 돈다."""
        self.turn_direction = self._choose_turn_direction(now_sec)
        self.turn_end_sec = now_sec + self.turn_duration
        self.turn_deadline_sec = now_sec + self.max_turn_duration
        self.turn_is_committed = False
        self.turn_target_heading = None
        self.state = 'TURN'
        self._pause_progress_timers(now_sec)
        direction = 'left' if self.turn_direction > 0.0 else 'right'
        front = 'open' if math.isinf(self.front_distance) else f'{self.front_distance:.2f}m'
        self.get_logger().info(f'Front {front}; turning {direction}.')

    def _begin_turn_towards(self, now_sec: float, heading: float, reason: str) -> None:
        """지정한 절대 방향(map 기준)을 향해 회전한다. 각도를 다 채우고 끝낸다."""
        _x, _y, yaw = self.pose
        delta = normalize_angle(heading - yaw)
        if abs(delta) < math.radians(15.0):
            return                      # 이미 그쪽을 보고 있다
        estimated_duration = abs(delta) / max(self.turn_speed, 1e-3)
        self.turn_direction = 1.0 if delta > 0.0 else -1.0
        # 0.25초는 방향 흔들림을 무시하는 최소 회전 구간이고,
        # 그 뒤부터는 TF yaw로 목표 도달을 직접 확인한다.
        self.turn_end_sec = now_sec + min(0.25, estimated_duration)
        self.turn_deadline_sec = now_sec + min(
            self.max_goal_turn_duration, estimated_duration + 0.5)
        self.turn_is_committed = True
        self.turn_target_heading = normalize_angle(heading)
        self.state = 'TURN'
        self._pause_progress_timers(now_sec)
        self.get_logger().info(
            f'{reason}: turning {math.degrees(delta):+.0f} deg toward unexplored area.')

    def _maybe_redirect(self, now_sec: float) -> bool:
        """같은 자리를 오래 맴돌면 안 가본 쪽으로 방향을 다시 잡는다.

        세 가지 계기를 함께 본다.
          - 갇힘: stuck_radius 안에서 stuck_timeout 을 넘김 (벽 사이 왕복 포함)
          - 재방문: 지금 칸에 들어온 횟수가 revisit_threshold 이상
          - 낮은 장애물: stall_monitor 가 지금 진행 방향의 장애물을 알려줌

        되돌아온 값이 True 면 회전을 시작했다는 뜻이다.
        """
        if self.pose is None or self.map_view is None:
            return False
        if (self.last_redirect_sec is not None and
                now_sec - self.last_redirect_sec < self.redirect_cooldown_sec):
            return False

        x, y, yaw = self.pose
        stuck = (self.anchor_xy is not None and
                 now_sec - self.anchor_sec > self.stuck_timeout)
        current_cell = self.visits.cell_of(x, y)
        current_visit_count = self.visits.count(x, y)
        revisit_event = (current_cell, current_visit_count)
        frontier_recently_progressed = (
            self.last_frontier_progress_sec is not None and
            now_sec - self.last_frontier_progress_sec <
            self.redirect_cooldown_sec)
        revisited = (
            self.revisit_threshold > 0 and
            current_visit_count >= self.revisit_threshold and
            revisit_event != self.last_handled_revisit and
            not frontier_recently_progressed)
        blind_ahead = self._blind_penalty(yaw) > 0.0
        goal_stalled = self._frontier_is_stalled(now_sec)
        if not (stuck or revisited or blind_ahead or goal_stalled):
            return False

        # 여기서부터는 전방위 후보를 훑는 무거운 계산이다(12방향 x 5레이).
        # 조건이 계속 참인 동안 매 tick(10Hz) 돌리면 낭비이므로 1초에 한 번만 본다.
        if now_sec - self.last_eval_sec < 1.0:
            return False
        self.last_eval_sec = now_sec

        # 지금 목표 때문에 계속 같은 구역을 되밟았다면 그 목표는 잠시 제외한다.
        # 다음 프런티어를 다시 고르면 "벽 건너의 같은 목표"를 반복 시도하지 않는다.
        if stuck or revisited or blind_ahead or goal_stalled:
            self._abandon_frontier_target(
                now_sec, 'stuck' if stuck else
                'revisited' if revisited else
                'blind obstacle' if blind_ahead else 'no progress')

        frontier = self._plan_frontier(now_sec, force=True)
        if frontier is not None:
            # 목표를 직선으로 바라보면 L/U자 벽에서 회피한 뒤 다시
            # 벽 쪽으로 돌아가는 루프가 생긴다. A* 경로의 lookahead를 본다.
            lookahead = self._path_lookahead(now_sec)
            if lookahead is None:
                self._abandon_frontier_target(now_sec, 'no safe path')
                frontier = None
            else:
                heading = math.atan2(lookahead[1] - y, lookahead[0] - x)
                score = frontier[2]
        if frontier is None:
            # 지도 갱신 초기에는 프런티어가 없을 수 있다. 그때만 기존 지역 방향
            # 점수로 안전하게 물러난다.
            best = self._best_heading()
            if best is None:
                return False
            heading, score = best
        # 갇혔거나 낮은 장애물이 확인된 때는 "직진보다 나은가"를 따지지 않는다.
        # 이미 못 나가고 있으므로 가장 나은 쪽으로 일단 튼다.
        if (not stuck and not blind_ahead and not goal_stalled and not revisited and
                score < self._score(yaw) + self.redirect_margin):
            return False

        # 장애물 감점이 여러 후보에 똑같이 걸려 현재 방향이 다시 뽑힌 경우에는
        # 더 넓은 측면으로 90도 돌려 같은 문턱을 계속 밀지 않게 한다.
        if (blind_ahead and
                abs(normalize_angle(heading - yaw)) < math.radians(15.0)):
            heading = normalize_angle(
                yaw + self._choose_turn_direction(now_sec) * math.pi / 2.0)

        self.last_redirect_sec = now_sec
        if revisited:
            self.last_handled_revisit = revisit_event
        self.anchor_xy, self.anchor_sec = (x, y), now_sec
        reason = ('Known low obstacle ahead' if blind_ahead else
                  'Frontier made no progress' if goal_stalled else
                  'Stuck' if stuck else 'Area already covered')
        self._begin_turn_towards(now_sec, heading, reason)
        return self.state == 'TURN'

    def _stop(self) -> None:
        if rclpy.ok():
            self.cmd_pub.publish(Twist())

    # ------------------------------------------------------------------
    # 주기 루프
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        now = self.get_clock().now()
        now_sec = now.nanoseconds / 1e9

        if (self.diagnostic_status and
                now_sec - self.last_diagnostic_sec >= 2.0):
            scan_age = (math.inf if self.last_scan_time is None else
                        (now - self.last_scan_time).nanoseconds / 1e9)
            front = ('inf' if math.isinf(self.front_distance)
                     else f'{self.front_distance:.2f}')
            self.get_logger().info(
                f'[diagnostic] state={self.state}, scan_age={scan_age:.2f}s, '
                f'front={front}m, scan_valid={self.scan_is_valid}, '
                f'emergency_stop={self.emergency_stop}')
            self.last_diagnostic_sec = now_sec

        if self.emergency_stop or self.actuation_fault:
            self._stop()
            return

        if self.max_runtime_sec > 0.0:
            elapsed = (now - self.started_at).nanoseconds / 1e9
            if elapsed >= self.max_runtime_sec:
                self.get_logger().info('Configured mapping time reached; stopping.')
                self._stop()
                rclpy.shutdown()
                return

        if self.last_scan_time is None:
            self._stop()
            return

        scan_age = (now - self.last_scan_time).nanoseconds / 1e9
        if scan_age > self.scan_timeout:
            self.get_logger().warn('LiDAR scan is stale; stopping for safety.')
            self._stop()
            self.state = 'WAIT_FOR_SCAN'
            return

        if not self.scan_is_valid:
            if not self.invalid_scan_warned:
                self.get_logger().error(
                    'LiDAR scan has too few valid points; stopping for safety.')
                self.invalid_scan_warned = True
            self._stop()
            self.state = 'WAIT_FOR_SCAN'
            return

        if self.state == 'WAIT_FOR_SCAN':
            self._resume_progress_timers(now_sec)
        self._update_pose(now_sec)

        command = Twist()

        if self.state == 'BLOCKED_STOP':
            if self.front_distance >= self.safe_distance:
                self.state = 'FORWARD'
                self.forward_commit_until_sec = now_sec + self.post_turn_forward_sec
                self.blocked_clear_since_sec = now_sec
                self._resume_progress_timers(now_sec)
            elif self._rear_is_clear() and self.backup_speed > 0.0:
                self.state = 'BACKUP'
                self.backup_end_sec = now_sec + self.backup_duration_sec
                self._resume_progress_timers(now_sec)
            elif (self.blocked_since_sec is not None and
                  now_sec - self.blocked_since_sec >= self.blocked_retry_delay_sec and
                  self.blocked_retry_count < self.blocked_retry_max_count):
                direction = blocked_recovery_direction(
                    self.left_distance, self.right_distance,
                    self.left_scan_observed, self.right_scan_observed,
                    self.blocked_side_clearance, self.turn_direction)
                if direction is not None:
                    self.blocked_retry_count += 1
                    self._begin_blocked_recovery_turn(now_sec, direction)
            if (self.state == 'BLOCKED_STOP' and
                    now_sec - self.last_blocked_log_sec >= 5.0):
                self.last_blocked_log_sec = now_sec
                self.get_logger().warn(
                    'Safety hold continues '
                    f'(front={self._distance_text(self.front_distance)}, '
                    f'rear={self._distance_text(self.rear_distance, self.rear_scan_observed)}, '
                    f'retries={self.blocked_retry_count}/'
                    f'{self.blocked_retry_max_count}). Clear the obstacle or move '
                    'the robot by hand; motion resumes automatically when safe.')

        elif self.state == 'BACKUP':
            if (self.backup_end_sec is None or
                    now_sec >= self.backup_end_sec or
                    not self._rear_is_clear()):
                self.state = 'FORWARD'
                self.backup_end_sec = None
                self.forward_commit_until_sec = now_sec + self.post_turn_forward_sec
                self.last_turn_end_sec = now_sec
            else:
                command.linear.x = -self.backup_speed

        elif self.state in ('WAIT_FOR_SCAN', 'FORWARD'):
            if self.front_distance < self.safe_distance:
                self.blocked_clear_since_sec = None
                self._begin_turn(now_sec)
            else:
                if self.state == 'WAIT_FOR_SCAN':
                    self.state = 'FORWARD'
                if self.blocked_episode_sec is not None:
                    if self.blocked_clear_since_sec is None:
                        self.blocked_clear_since_sec = now_sec
                    elif (now_sec - self.blocked_clear_since_sec >=
                          self.blocked_retry_reset_sec):
                        self.blocked_episode_sec = None
                        self.blocked_clear_since_sec = None
                        self.blocked_retry_count = 0
                        self.blocked_since_sec = None

            if (self.state == 'FORWARD' and
                    now_sec < self.forward_commit_until_sec):
                self.state = 'FORWARD'
                command.linear.x = (self.linear_speed * 0.55
                                    if self.front_distance < self.slow_distance
                                    else self.linear_speed)
            elif self.state == 'FORWARD' and self._maybe_redirect(now_sec):
                pass                  
            elif self.state == 'FORWARD':
                motion = self._frontier_motion(now_sec)
                if self.state != 'TURN':
                    self.state = 'FORWARD'
                    if motion is None:
                        linear, angular = self.linear_speed, 0.0
                    else:
                        linear, angular = motion
                    if self.front_distance < self.slow_distance:
                        linear *= 0.55
                    command.linear.x = linear
                    command.angular.z = angular

        if self.state == 'TURN':
            min_duration_done = now_sec >= self.turn_end_sec
            target_aligned = False
            if (self.turn_is_committed and
                    self.turn_target_heading is not None and
                    self.pose is not None):
                target_delta = normalize_angle(
                    self.turn_target_heading - self.pose[2])
                target_aligned = abs(target_delta) <= self.frontier_heading_tolerance
                if not target_aligned:
                    self.turn_direction = 1.0 if target_delta > 0.0 else -1.0
            turn_ready = (min_duration_done and
                          (target_aligned if self.turn_is_committed else True))
            front_clear = self.front_distance >= self.safe_distance
            action = turn_exit_action(
                min_turn_done=turn_ready,
                committed=self.turn_is_committed,
                front_clear=front_clear,
                deadline_reached=now_sec >= self.turn_deadline_sec,
                rear_clear=self._rear_is_clear(),
                backup_enabled=(self.backup_speed > 0.0 and
                                self.backup_duration_sec > 0.0))
            if action == 'FORWARD':
                self.state = action
                self.turn_target_heading = None
                self.turn_is_committed = False
                self.last_turn_end_sec = now_sec
                self.forward_commit_until_sec = now_sec + self.post_turn_forward_sec
                self._resume_progress_timers(now_sec)
            elif action == 'BACKUP':
                self.state = action
                self.turn_target_heading = None
                self.turn_is_committed = False
                self.backup_end_sec = now_sec + self.backup_duration_sec
                self.last_turn_end_sec = now_sec
                self._resume_progress_timers(now_sec)
                self.get_logger().warn(
                    'Turn limit reached with front blocked; backing up briefly.')
            elif action == 'BLOCKED_STOP':
                self._enter_blocked_stop(now_sec)
            else:
                if (self.turn_is_committed and
                        self.turn_target_heading is not None and
                        self.pose is not None):
                    remaining = abs(normalize_angle(
                        self.turn_target_heading - self.pose[2]))
                    angular_speed = min(
                        self.turn_speed, max(0.16, 1.2 * remaining))
                else:
                    angular_speed = self.turn_speed
                command.angular.z = angular_speed * self.turn_direction

        self.cmd_pub.publish(command)


def main(args=None) -> int:
    rclpy.init(args=args)
    node = AutoMapper()

    def request_shutdown(_signum, _frame) -> None:
        # 백그라운드 프로세스가 SIGTERM으로 종료돼도 마지막 정지 명령을 보낸다.
        node._stop()
        if rclpy.ok():
            rclpy.shutdown()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        actuation_fault = node.actuation_fault
        node._stop()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    # launch script가 "시간 만료/사용자 종료"와 실제 구동계 이상을 구분할 수 있게
    # 한다. console_scripts wrapper는 이 반환값을 프로세스 종료 코드로 사용한다.
    return 2 if actuation_fault else 0


if __name__ == '__main__':
    main()
