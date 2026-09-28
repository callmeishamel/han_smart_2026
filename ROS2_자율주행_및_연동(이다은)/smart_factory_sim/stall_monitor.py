#!/usr/bin/env python3
"""LiDAR 에 안 보이는 낮은 장애물을 "못 나가는 것"으로 알아채고 지도에 남긴다.

무슨 문제인가
-------------
2D LiDAR 는 바닥에서 약 0.18m 한 높이만 봅니다. **그보다 낮은 것은 영영 안
보입니다** — 문턱, 케이블 트레이, 파렛트 하단, 배수구 덮개, 낮은 받침대.
로봇은 앞이 비어 있다고 믿고 계속 밀어붙이고, 바퀴만 헛돕니다. 범퍼가 눌리면
auto_mapper 가 비상정지하지만, 살짝 걸린 정도로는 범퍼도 안 눌립니다.

센서가 못 보면 **행동으로 알아내야 합니다.** "전진 명령을 주고 있는데 지도상
위치가 몇 초째 그대로다" 는 곧 "앞에 뭔가 있다" 입니다.

왜 /odom 이 아니라 TF 인가
--------------------------
바퀴가 헛돌면 **오도메트리는 거짓말을 합니다.** 파렛트를 밀고 있는 동안 바퀴는
돌고 /odom 은 "잘 가고 있다" 고 보고합니다. 반면 map -> base_footprint TF 는
odometry를 보조 입력으로 쓰는 Cartographer의 라이다 스캔 매칭 결과이므로,
로봇이 실제로 안 움직였으면 그대로입니다.

그래서 두 신호를 함께 봅니다.

  전진 명령 있음 + 지도 위치 그대로            -> 걸림 (확실)
  거기에 더해 오도메트리는 움직였다고 함        -> 바퀴 헛돎 (아주 확실)

두 번째 조건은 없어도 동작하지만, 있으면 "SLAM 이 잠깐 멈춘 것" 과 "진짜 걸린
것" 을 구분해 줍니다.

이 노드가 하지 않는 것
----------------------
**`/cmd_vel` 을 쓰지 않습니다.** 순찰 중에는 Nav2 가, 매핑 중에는 auto_mapper 가
`/cmd_vel` 의 주인입니다. 여기서 또 쓰면 두 명령이 서로를 밀어내며 로봇이
갈팡질팡합니다. 이 노드는 **알아내서 알리는 일만** 하고, 피하는 것은 각자
`/blind_obstacles` 를 보고 합니다.

  auto_mapper   — 그 방향 점수를 깎아 다시 들이받지 않게 합니다
  patrol_planner — 순찰 지점을 그 자리에 두지 않습니다
  Nav2          — 아래 keepout 마스크를 물리면 경로 계획에서 피합니다
  미니맵         — 화면에 표시해 사람이 치우러 갈 수 있게 합니다

발견한 자리는 파일에 남습니다. 문턱이나 케이블 트레이는 치우지 않는 한 계속
거기 있으므로, 재시작할 때마다 다시 들이받을 이유가 없습니다.

Nav2 에 물리려면 (선택)
-----------------------
이 노드는 `/keepout_filter_mask`(OccupancyGrid)와 `/costmap_filter_info` 를 함께
발행합니다. nav2_params.yaml 의 두 코스트맵에 필터를 추가하면 Nav2 가 경로 계획
단계에서 피합니다.

    global_costmap:
      global_costmap:
        ros__parameters:
          plugins: ["static_layer", "obstacle_layer", "inflation_layer"]
          filters: ["keepout_filter"]
          keepout_filter:
            plugin: "nav2_costmap_2d::KeepoutFilter"
            enabled: True
            filter_info_topic: "/costmap_filter_info"

실행:
    ros2 run smart_factory_sim stall_monitor
"""

import json
import math
import os
import time
from collections import deque
from typing import List, Optional, Tuple

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, QoSProfile, ReliabilityPolicy,
                       qos_profile_sensor_data)
from std_msgs.msg import String
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from turtlebot3_msgs.msg import SensorState

# Nav2 keepout 필터용 메시지. 없으면 JSON 발행만 하고 조용히 넘어갑니다
# (nav2_msgs 가 없는 환경에서 이 노드 전체가 죽으면 안 됩니다).
try:
    from nav2_msgs.msg import CostmapFilterInfo
except ImportError:                                   # pragma: no cover
    CostmapFilterInfo = None


def quaternion_yaw(q) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def classify_encoder_evidence(samples, window_start_sec: float, now_sec: float,
                              min_delta: int = 2,
                              stale_sec: float = 1.0) -> str:
    """명령이 있었던 구간의 OpenCR 엔코더 상태를 분류한다.

    지도 TF가 안 움직였다는 사실 하나만으로는 낮은 장애물과 모터 미구동을
    구분할 수 없다. 양쪽 엔코더가 변했을 때만 "바퀴는 돌았지만 못 나갔다"로
    판단한다. 반환값은 테스트와 운영 로그에서 그대로 쓰는 짧은 식별자다.
    """
    if not samples:
        return 'sensor_state_missing'

    # 창 시작 바로 전 표본을 기준으로 삼으면, 창 안의 첫 센서 표본이 조금 늦어도
    # 움직임을 놓치지 않는다.
    before = [sample for sample in samples if sample[0] <= window_start_sec]
    after = [sample for sample in samples if sample[0] >= window_start_sec]
    first = before[-1] if before else (after[0] if after else None)
    last = samples[-1]
    if first is None or last[0] < now_sec - stale_sec:
        return 'sensor_state_stale'
    if not first[3] or not last[3]:
        return 'torque_disabled'

    left_delta = abs(int(last[1]) - int(first[1]))
    right_delta = abs(int(last[2]) - int(first[2]))
    if left_delta < min_delta and right_delta < min_delta:
        return 'no_encoder_motion'
    if left_delta < min_delta or right_delta < min_delta:
        return 'one_wheel_no_motion'
    return 'encoder_motion'


class StallDetector:
    """"명령은 주는데 안 나간다" 를 판정한다. ROS 없이 동작하므로 테스트할 수 있다.

    판정 조건 (모두 만족해야 함)
      1. `window_sec` 동안 표본이 모여 있다
      2. 그 동안 **거의 계속** 전진/후진 명령이 있었다 (`min_cmd_ratio`)
      3. 그런데 지도상 이동거리가 `min_move_m` 미만이다

    2번을 비율로 보는 이유: Nav2 는 목표 근처에서 속도를 잠깐 0 으로 떨어뜨리거나
    제자리 회전을 섞습니다. 한 표본이라도 0 이면 탈락시키면 실제 걸림을 놓칩니다.
    """

    def __init__(self, window_sec: float = 3.0, min_cmd_speed: float = 0.02,
                 min_move_m: float = 0.05, min_cmd_ratio: float = 0.8,
                 cooldown_sec: float = 15.0, front_offset_m: float = 0.20):
        self.window_sec = float(window_sec)
        self.min_cmd_speed = float(min_cmd_speed)
        self.min_move_m = float(min_move_m)
        self.min_cmd_ratio = float(min_cmd_ratio)
        self.cooldown_sec = float(cooldown_sec)
        self.front_offset_m = float(front_offset_m)

        self.samples = deque()      # (t, x, y, yaw, cmd_linear)
        self.last_fire_sec = None
        # 마지막으로 "못 나감"이 판정된 명령 창의 시작 시각. 노드가 같은
        # 구간의 엔코더 표본을 조회해 장애물/구동계 이상을 구분할 때 쓴다.
        self.last_window_start_sec = None

    def reset(self) -> None:
        self.samples.clear()

    def update(self, now_sec: float, pose: Optional[Tuple[float, float, float]],
               cmd_linear: float) -> Optional[Tuple[float, float]]:
        """표본 하나를 넣고, 걸림이 판정되면 의심 지점 (x, y) 를 돌려준다.

        pose 는 map 좌표계의 (x, y, yaw). None 이면(TF 없음) 판단을 미룹니다 —
        위치를 모르는 동안의 "안 움직임" 은 걸림의 증거가 될 수 없습니다.
        """
        if pose is None:
            self.reset()
            return None

        x, y, yaw = pose
        self.samples.append((now_sec, x, y, yaw, float(cmd_linear)))

        # 창 밖으로 나간 표본 버리기
        while self.samples and now_sec - self.samples[0][0] > self.window_sec:
            self.samples.popleft()

        if len(self.samples) < 2:
            return None
        if now_sec - self.samples[0][0] < self.window_sec:
            return None                        # 아직 창을 못 채웠다

        if (self.last_fire_sec is not None
                and now_sec - self.last_fire_sec < self.cooldown_sec):
            return None                        # 방금 알렸다. 같은 자리로 도배하지 않는다

        moving_commands = sum(1 for s in self.samples
                              if abs(s[4]) >= self.min_cmd_speed)
        if moving_commands < self.min_cmd_ratio * len(self.samples):
            return None                        # 애초에 갈 생각이 없었다

        # 창 안에서 **가장 멀리** 간 거리를 본다. 처음/끝만 비교하면 앞뒤로
        # 흔들리다 제자리로 돌아온 경우를 놓칩니다.
        first_x, first_y = self.samples[0][1], self.samples[0][2]
        travelled = max(math.hypot(s[1] - first_x, s[2] - first_y)
                        for s in self.samples)
        if travelled >= self.min_move_m:
            return None                        # 잘 가고 있다

        # 걸렸다. 장애물은 진행 방향 앞(후진이면 뒤)에 있다.
        direction = 1.0 if self.samples[-1][4] >= 0.0 else -1.0
        obstacle_x = x + direction * self.front_offset_m * math.cos(yaw)
        obstacle_y = y + direction * self.front_offset_m * math.sin(yaw)
        self.last_window_start_sec = self.samples[0][0]
        self.last_fire_sec = now_sec
        self.reset()
        return (obstacle_x, obstacle_y)


class RotationStallDetector:
    """제자리 회전 명령이 있는데 지도 자세가 변하지 않는지 판정한다.

    자율매핑은 목표가 뒤에 있으면 ``linear.x == 0`` 인 상태로 먼저 회전한다.
    전진 속도만 보던 기존 감시기로는 이 구간의 OpenCR/DYNAMIXEL 고장을 영원히
    발견할 수 없었다. 회전은 장애물 위치를 추정할 수 없으므로, 이 감지기는
    keepout 장애물을 만들지 않고 구동계 이상만 알리는 데 사용한다.
    """

    def __init__(self, window_sec: float = 3.0, min_cmd_speed: float = 0.10,
                 min_turn_rad: float = math.radians(8.0),
                 min_cmd_ratio: float = 0.8, cooldown_sec: float = 15.0):
        self.window_sec = float(window_sec)
        self.min_cmd_speed = float(min_cmd_speed)
        self.min_turn_rad = float(min_turn_rad)
        self.min_cmd_ratio = float(min_cmd_ratio)
        self.cooldown_sec = float(cooldown_sec)

        self.samples = deque()      # (t, yaw, cmd_angular)
        self.last_fire_sec = None
        self.last_window_start_sec = None

    def reset(self) -> None:
        self.samples.clear()

    @staticmethod
    def _angle_delta(angle: float, reference: float) -> float:
        return math.atan2(math.sin(angle - reference),
                          math.cos(angle - reference))

    def update(self, now_sec: float,
               pose: Optional[Tuple[float, float, float]],
               cmd_angular: float) -> bool:
        if pose is None:
            self.reset()
            return False

        yaw = float(pose[2])
        self.samples.append((now_sec, yaw, float(cmd_angular)))
        while self.samples and now_sec - self.samples[0][0] > self.window_sec:
            self.samples.popleft()

        if len(self.samples) < 2:
            return False
        if now_sec - self.samples[0][0] < self.window_sec:
            return False
        if (self.last_fire_sec is not None
                and now_sec - self.last_fire_sec < self.cooldown_sec):
            return False

        turning_commands = sum(1 for sample in self.samples
                               if abs(sample[2]) >= self.min_cmd_speed)
        if turning_commands < self.min_cmd_ratio * len(self.samples):
            return False

        first_yaw = self.samples[0][1]
        turned = max(abs(self._angle_delta(sample[1], first_yaw))
                     for sample in self.samples)
        if turned >= self.min_turn_rad:
            return False

        self.last_window_start_sec = self.samples[0][0]
        self.last_fire_sec = now_sec
        self.reset()
        return True


class BlindObstacle:
    """LiDAR 에 안 보이는 장애물 하나."""

    __slots__ = ("x", "y", "radius", "hits", "first_seen", "last_seen")

    def __init__(self, x, y, radius, hits=1, first_seen=None, last_seen=None):
        self.x = float(x)
        self.y = float(y)
        self.radius = float(radius)
        self.hits = int(hits)
        self.first_seen = float(first_seen if first_seen is not None else time.time())
        self.last_seen = float(last_seen if last_seen is not None else self.first_seen)

    def as_dict(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3),
                "radius": round(self.radius, 3), "hits": self.hits,
                "first_seen": self.first_seen, "last_seen": self.last_seen}

    @classmethod
    def from_dict(cls, data: dict) -> "BlindObstacle":
        return cls(data["x"], data["y"], data.get("radius", 0.20),
                   data.get("hits", 1), data.get("first_seen"), data.get("last_seen"))


class BlindObstacleMap:
    """발견한 낮은 장애물 목록. 파일로 남아 재시작해도 이어집니다.

    문턱이나 케이블 트레이는 치우지 않는 한 계속 거기 있습니다. 매번 잊어버리면
    로봇은 매번 다시 들이받습니다.
    """

    def __init__(self, merge_distance_m: float = 0.40, radius_m: float = 0.20):
        self.merge_distance_m = float(merge_distance_m)
        self.radius_m = float(radius_m)
        self.obstacles: List[BlindObstacle] = []

    def add(self, x: float, y: float, now: Optional[float] = None) -> BlindObstacle:
        """가까운 기존 항목이 있으면 합치고, 없으면 새로 만든다."""
        now = time.time() if now is None else now
        for obstacle in self.obstacles:
            if math.hypot(obstacle.x - x, obstacle.y - y) <= self.merge_distance_m:
                # 여러 번 걸린 자리일수록 위치를 신뢰합니다. 가중 평균으로 당깁니다.
                weight = 1.0 / (obstacle.hits + 1)
                obstacle.x += (x - obstacle.x) * weight
                obstacle.y += (y - obstacle.y) * weight
                obstacle.hits += 1
                obstacle.last_seen = now
                return obstacle

        obstacle = BlindObstacle(x, y, self.radius_m, 1, now, now)
        self.obstacles.append(obstacle)
        return obstacle

    def clear(self) -> int:
        count = len(self.obstacles)
        self.obstacles.clear()
        return count

    def prune(self, max_age_sec: float, now: Optional[float] = None) -> int:
        """오래 다시 안 걸린 항목을 지운다. max_age_sec <= 0 이면 영구 보관."""
        if max_age_sec <= 0:
            return 0
        now = time.time() if now is None else now
        before = len(self.obstacles)
        self.obstacles = [o for o in self.obstacles
                          if now - o.last_seen <= max_age_sec]
        return before - len(self.obstacles)

    def as_list(self) -> List[dict]:
        return [o.as_dict() for o in self.obstacles]

    def save(self, path: str) -> bool:
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"obstacles": self.as_list()}, f, ensure_ascii=False, indent=2)
            return True
        except OSError:
            return False

    def load(self, path: str) -> int:
        """읽지 못하면 0. 파일이 깨졌다고 노드가 죽으면 안 됩니다."""
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("obstacles", [])
            self.obstacles = [BlindObstacle.from_dict(item) for item in items]
            return len(self.obstacles)
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def to_mask(self, info) -> Optional[np.ndarray]:
        """OccupancyGrid 정보에 맞춰 keepout 마스크(0 또는 100)를 만든다."""
        width, height = int(info.width), int(info.height)
        resolution = float(info.resolution)
        if width <= 0 or height <= 0 or resolution <= 0:
            return None

        mask = np.zeros((height, width), dtype=np.int8)
        origin_x = float(info.origin.position.x)
        origin_y = float(info.origin.position.y)

        for obstacle in self.obstacles:
            col = int((obstacle.x - origin_x) / resolution)
            row = int((obstacle.y - origin_y) / resolution)
            pad = max(1, int(round(obstacle.radius / resolution)))
            r0, r1 = max(0, row - pad), min(height, row + pad + 1)
            c0, c1 = max(0, col - pad), min(width, col + pad + 1)
            if r0 >= r1 or c0 >= c1:
                continue
            rows = np.arange(r0, r1)[:, None]
            cols = np.arange(c0, c1)[None, :]
            inside = ((rows - row) ** 2 + (cols - col) ** 2) <= pad * pad
            mask[r0:r1, c0:c1][inside] = 100
        return mask


class StallMonitorNode(Node):
    def __init__(self):
        super().__init__('stall_monitor')

        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_footprint')
        # 이 시간 동안 전진 명령이 있었는데 안 나가면 걸린 것으로 봅니다.
        self.declare_parameter('window_sec', 3.0)
        self.declare_parameter('min_cmd_speed', 0.02)
        self.declare_parameter('min_move_m', 0.05)
        self.declare_parameter('rotation_min_cmd_speed', 0.10)
        self.declare_parameter('rotation_min_turn_deg', 8.0)
        self.declare_parameter('cooldown_sec', 15.0)
        # 로봇 중심에서 이만큼 앞에 장애물이 있다고 봅니다 (반경 0.105 + 여유).
        self.declare_parameter('front_offset_m', 0.20)
        self.declare_parameter('obstacle_radius_m', 0.20)
        self.declare_parameter('merge_distance_m', 0.40)
        # 0 이면 영구 보관. 문턱처럼 안 없어지는 것이 대부분이라 기본은 영구입니다.
        self.declare_parameter('max_age_sec', 0.0)
        self.declare_parameter('store_path',
                               os.path.expanduser('~/blind_obstacles.json'))
        # 지도상 못 움직였을 때, 실제 바퀴가 돌았는지 OpenCR 엔코더로 확인한다.
        # 엔코더도 정지했다면 낮은 장애물이 아니라 구동계 이상이다.
        self.declare_parameter('encoder_min_delta', 2)
        self.declare_parameter('sensor_state_stale_sec', 1.0)
        # 바퀴는 도는데 지도 위치는 그대로 = 헛돎. 확실한 신호라 로그로 구분합니다.
        self.declare_parameter('slip_odom_move_m', 0.15)

        gp = self.get_parameter
        self.map_frame = str(gp('map_frame').value)
        self.base_frame = str(gp('base_frame').value)
        self.store_path = str(gp('store_path').value)
        self.max_age_sec = float(gp('max_age_sec').value)
        self.slip_odom_move_m = float(gp('slip_odom_move_m').value)
        self.encoder_min_delta = max(1, int(gp('encoder_min_delta').value))
        self.sensor_state_stale_sec = max(
            0.1, float(gp('sensor_state_stale_sec').value))

        self.detector = StallDetector(
            window_sec=float(gp('window_sec').value),
            min_cmd_speed=float(gp('min_cmd_speed').value),
            min_move_m=float(gp('min_move_m').value),
            cooldown_sec=float(gp('cooldown_sec').value),
            front_offset_m=float(gp('front_offset_m').value))
        self.rotation_detector = RotationStallDetector(
            window_sec=float(gp('window_sec').value),
            min_cmd_speed=float(gp('rotation_min_cmd_speed').value),
            min_turn_rad=math.radians(float(gp('rotation_min_turn_deg').value)),
            cooldown_sec=float(gp('cooldown_sec').value))

        self.obstacles = BlindObstacleMap(
            merge_distance_m=float(gp('merge_distance_m').value),
            radius_m=float(gp('obstacle_radius_m').value))
        loaded = self.obstacles.load(self.store_path)
        if loaded:
            self.get_logger().info(
                f"이전에 찾아 둔 낮은 장애물 {loaded}개를 불러왔습니다 "
                f"({self.store_path}).")

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.cmd_linear = 0.0
        self.cmd_angular = 0.0
        self.cmd_seen_sec = 0.0
        self.odom_xy = None
        self.odom_at_window_start = None
        self.latest_map = None
        # (monotonic_sec, left_encoder, right_encoder, torque)
        self.encoder_samples = deque()

        self.create_subscription(Twist, '/cmd_vel', self._on_cmd, 10)
        self.create_subscription(Odometry, '/odom', self._on_odom, 10)
        self.create_subscription(SensorState, '/sensor_state', self._on_sensor_state,
                                 qos_profile_sensor_data)
        # map_server는 TRANSIENT_LOCAL, Cartographer 조합은 VOLATILE로
        # /map을 발행할 수 있다. 두 QoS 구독을 같이 두어 저장 지도를
        # 나중에 붙어도 받고, 자율매핑의 실시간 지도도 놓치지 않는다.
        map_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, '/map', self._on_map, 1)
        self.create_subscription(OccupancyGrid, '/map', self._on_map, map_qos)
        self.create_subscription(String, '/blind_obstacle_control',
                                 self._on_control, 10)

        # 결과는 늦게 뜬 구독자도 받아야 합니다(미니맵이 나중에 붙는 일이 흔함).
        latched = QoSProfile(depth=1,
                             reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.obstacle_pub = self.create_publisher(String, '/blind_obstacles', latched)
        self.fault_pub = self.create_publisher(String, '/actuation_fault', latched)
        self.mask_pub = self.create_publisher(OccupancyGrid, '/keepout_filter_mask',
                                              latched)
        self.info_pub = None
        if CostmapFilterInfo is not None:
            self.info_pub = self.create_publisher(
                CostmapFilterInfo, '/costmap_filter_info', latched)
        else:
            self.get_logger().warning(
                "nav2_msgs 를 찾지 못해 costmap_filter_info 를 발행하지 않습니다. "
                "JSON(/blind_obstacles) 은 그대로 나갑니다.")

        self.create_timer(0.2, self._tick)          # 5Hz 로 충분합니다
        self._publish_all()

        self.get_logger().info(
            "낮은 장애물 감시 시작 — 전진 명령이 있는데 "
            f"{self.detector.window_sec:.0f}초간 {self.detector.min_move_m*100:.0f}cm 도 "
            "못 나가면 장애물로 표시합니다. (이 노드는 /cmd_vel 을 쓰지 않습니다)")

    # ------------------------------------------------------------------
    def _on_cmd(self, msg: Twist):
        self.cmd_linear = float(msg.linear.x)
        self.cmd_angular = float(msg.angular.z)
        self.cmd_seen_sec = time.monotonic()

    def _on_odom(self, msg: Odometry):
        self.odom_xy = (float(msg.pose.pose.position.x),
                        float(msg.pose.pose.position.y))

    def _on_sensor_state(self, msg: SensorState):
        now = time.monotonic()
        self.encoder_samples.append((now, int(msg.left_encoder),
                                     int(msg.right_encoder), bool(msg.torque)))
        # 판정 창(기본 3초)보다 넉넉하게 보관하되, 장시간 매핑에서 끝없이 쌓지 않는다.
        cutoff = now - 30.0
        while self.encoder_samples and self.encoder_samples[0][0] < cutoff:
            self.encoder_samples.popleft()

    def _on_map(self, msg: OccupancyGrid):
        self.latest_map = msg
        # 영속 저장소에서 읽은 장애물도 첫 /map 수신 즉시 Nav2에 반영한다.
        # 초기화 시점에는 지도 메타데이터가 없어 마스크를 만들 수 없다.
        self._publish_mask()

    def _on_control(self, msg: String):
        """{"command": "clear"} — 치운 뒤 기록을 비웁니다."""
        try:
            command = str(json.loads(msg.data).get('command', '')).lower()
        except (TypeError, ValueError):
            command = msg.data.strip().lower()

        if command == 'clear':
            removed = self.obstacles.clear()
            self.obstacles.save(self.store_path)
            self._publish_all()
            self.get_logger().info(f"낮은 장애물 기록 {removed}개를 지웠습니다.")
        else:
            self.get_logger().warning(f"알 수 없는 명령: {msg.data!r}")

    def _encoder_evidence(self, window_start_sec: float, now_sec: float) -> str:
        return classify_encoder_evidence(
            self.encoder_samples, window_start_sec, now_sec,
            self.encoder_min_delta, self.sensor_state_stale_sec)

    def _publish_actuation_fault(self, reason: str):
        msg = String()
        msg.data = json.dumps({
            'reason': reason,
            'stamp': time.time(),
        }, ensure_ascii=False)
        self.fault_pub.publish(msg)

    # ------------------------------------------------------------------
    def _pose(self):
        try:
            tf = self.tf_buffer.lookup_transform(
                self.map_frame, self.base_frame, rclpy.time.Time())
        except TransformException:
            return None
        return (float(tf.transform.translation.x),
                float(tf.transform.translation.y),
                quaternion_yaw(tf.transform.rotation))

    def _tick(self):
        now = time.monotonic()

        # /cmd_vel 이 끊긴 지 오래면 아무도 안 몰고 있는 것입니다.
        command_is_fresh = (now - self.cmd_seen_sec) < 1.0
        commanded = self.cmd_linear if command_is_fresh else 0.0
        commanded_angular = self.cmd_angular if command_is_fresh else 0.0
        pose = self._pose()

        was_empty = not self.detector.samples
        if was_empty and self.odom_xy is not None:
            self.odom_at_window_start = self.odom_xy

        hit = self.detector.update(now, pose, commanded)

        # 곡선 주행 중 작은 조향 보정까지 회전 고장으로 오인하지 않도록, 선속도가
        # 사실상 0인 제자리 회전만 별도로 감시한다.
        pure_turn_command = (commanded_angular
                             if abs(commanded) < self.detector.min_cmd_speed
                             else 0.0)
        rotation_stalled = self.rotation_detector.update(
            now, pose, pure_turn_command)
        if rotation_stalled:
            evidence = self._encoder_evidence(
                self.rotation_detector.last_window_start_sec, now)
            reason = {
                'sensor_state_missing': '/sensor_state unavailable during rotation',
                'sensor_state_stale': '/sensor_state stale during rotation',
                'torque_disabled': 'motor torque disabled during rotation',
                'no_encoder_motion': 'both wheel encoders did not change during rotation',
                'one_wheel_no_motion': 'only one wheel encoder changed during rotation',
                'encoder_motion': (
                    'wheel encoders changed but map yaw did not change during rotation'),
            }.get(evidence, evidence)
            self.get_logger().error('Actuation fault during rotation: ' + reason)
            self._publish_actuation_fault(reason)
            return

        if hit is None:
            return

        # "명령은 있는데 지도상 못 감"은 모터가 실제로 돌았다는 증거가 있을 때만
        # 낮은 장애물이다. 이 확인 없이 저장하면 OpenCR/DYNAMIXEL 고장을 지도에
        # 가짜 장애물로 남기고, 다음 실행의 auto_mapper까지 계속 회전시킨다.
        evidence = self._encoder_evidence(
            self.detector.last_window_start_sec, now)
        if evidence != 'encoder_motion':
            reason = {
                'sensor_state_missing': '/sensor_state unavailable during drive window',
                'sensor_state_stale': '/sensor_state stale during drive window',
                'torque_disabled': 'motor torque disabled during drive window',
                'no_encoder_motion': 'both wheel encoders did not change during drive window',
                'one_wheel_no_motion': 'only one wheel encoder changed during drive window',
            }.get(evidence, evidence)
            self.get_logger().error(
                'Actuation fault, not a blind obstacle: ' + reason)
            self._publish_actuation_fault(reason)
            return

        x, y = hit
        obstacle = self.obstacles.add(x, y)

        # 바퀴는 돌았는데 지도 위치가 그대로면 헛돎입니다. 원인을 좁혀 줍니다.
        slipped = False
        if self.odom_xy is not None and self.odom_at_window_start is not None:
            odom_moved = math.hypot(self.odom_xy[0] - self.odom_at_window_start[0],
                                    self.odom_xy[1] - self.odom_at_window_start[1])
            slipped = odom_moved >= self.slip_odom_move_m

        note = " (바퀴는 돌았습니다 — 헛돎)" if slipped else ""
        self.get_logger().warning(
            f"낮은 장애물로 보입니다: ({x:.2f}, {y:.2f}) "
            f"{obstacle.hits}번째{note}. LiDAR 에 안 보이는 높이일 수 있습니다.")

        self.obstacles.prune(self.max_age_sec)
        self.obstacles.save(self.store_path)
        self._publish_all()

    # ------------------------------------------------------------------
    def _publish_all(self):
        msg = String()
        msg.data = json.dumps({"obstacles": self.obstacles.as_list()},
                              ensure_ascii=False)
        self.obstacle_pub.publish(msg)
        self._publish_mask()

    def _publish_mask(self):
        """Nav2 KeepoutFilter 용 마스크. /map 과 같은 격자를 씁니다."""
        if self.latest_map is None:
            return
        mask = self.obstacles.to_mask(self.latest_map.info)
        if mask is None:
            return

        grid = OccupancyGrid()
        grid.header.frame_id = self.map_frame
        grid.header.stamp = self.get_clock().now().to_msg()
        grid.info = self.latest_map.info
        grid.data = mask.reshape(-1).tolist()
        self.mask_pub.publish(grid)

        if self.info_pub is not None:
            info = CostmapFilterInfo()
            info.header = grid.header
            info.type = 0                       # 0 = keepout / preferred lane
            info.filter_mask_topic = '/keepout_filter_mask'
            info.base = 0.0
            info.multiplier = 1.0
            self.info_pub.publish(info)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = StallMonitorNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception:
        # launch 프로세스 그룹이 종료되면 ROS context가 먼저 내려가 Humble의
        # rclpy.spin()이 RCLError를 던질 수 있다. 정상 종료 상황만 조용히 받되,
        # 실행 중 발생한 실제 예외는 숨기지 않는다.
        if rclpy.ok():
            raise
    finally:
        if node is not None:
            node.obstacles.save(node.store_path)
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
