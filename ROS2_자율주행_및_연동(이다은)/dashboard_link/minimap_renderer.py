"""
minimap_renderer.py

실행 위치: 다은님 노트북 (ROS2 + SLAM이 돌아가는 곳)

역할:
1. ROS2 '/map' 토픽(nav_msgs/OccupancyGrid, SLAM이 만드는 지도)을 구독
2. 로봇 현재 위치+방향 토픽을 구독해서 지도 위에 로봇 위치를 표시
3. 젯슨에서 UDP로 받은 "탐지 방향각(bearing)"을 로봇 위치/방향과 결합해,
   지도(occupancy grid) 위에서 레이캐스팅으로 실제 위치를 추정해 마커로 표시
   (depth 값 대신 화각(각도)만 사용 -> depth 노이즈 문제를 우회)
4. 완성된 미니맵을 MJPEG로 스트리밍

이전 depth+TF2 방식 대비 바뀐 점:
- 젯슨은 더 이상 depth 값이 필요 없음. 박스 중심의 픽셀 위치로부터
  "카메라 정면 기준 몇 도 방향에 물체가 있는지"만 계산해서 UDP로 보냄.
- 다은님 노트북은 이미 갖고 있는 occupancy grid + 로봇 위치/방향을 이용해,
  그 각도 방향으로 가상의 레이(ray)를 쏴서 처음 부딪히는 장애물 위치를 거리로 씀.
  -> depth 카메라 없이도 동작하고, 있어도 노이즈에 훨씬 강함.

실행 전 확인 (다은님과 같이 확인 필요):
- TODO 표시된 토픽 이름이 실제 환경과 맞는지
- CAMERA_YAW_OFFSET: 카메라가 로봇 정면과 다른 방향을 보고 있다면 그 오차(라디안)
- ROS2 환경을 source한 뒤: pip install flask
- numpy/OpenCV/rclpy는 package.xml의 rosdep 의존성으로 설치

실행 방법:
    python minimap_renderer.py
"""

import os
import time
import math
import json
import base64
import hmac
import hashlib
import socket
import threading
import uuid
import numpy as np
import cv2
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
try:
    from rclpy.executors import ExternalShutdownException
except ImportError:  # ROS 없는 순수 로직 테스트 호환
    ExternalShutdownException = RuntimeError
try:
    from rclpy._rclpy_pybind11 import RCLError
except ImportError:  # ROS 배포판별 호환
    RCLError = RuntimeError
from nav_msgs.msg import OccupancyGrid, Odometry
from sensor_msgs.msg import BatteryState, LaserScan
from geometry_msgs.msg import PoseWithCovarianceStamped
from std_msgs.msg import String
from flask import Flask, Response, jsonify, request, abort, send_from_directory

from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

# 순찰 경로 플래너. smart_factory_sim 패키지 안에 있지만, 이 파일은 colcon 으로
# 설치되지 않고 `python3 minimap_renderer.py` 로 직접 실행됩니다. 패키지가 안
# 잡히는 환경이 흔해서 상대 경로로도 찾아봅니다 (저장소에서는 항상 존재).
#
# 둘 다 실패해도 **지도 화면은 살아 있어야 합니다.** 순찰 계획은 부가 기능이고,
# 여기서 예외를 올리면 로봇 위치와 탐지 마커까지 통째로 못 보게 됩니다.
patrol_planner = None
_PATROL_IMPORT_ERROR = ""
try:
    from smart_factory_sim import patrol_planner
except ImportError:
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "smart_factory_sim"))
        import patrol_planner
    except ImportError as _exc:
        _PATROL_IMPORT_ERROR = (
            f"순찰 경로 플래너를 불러오지 못했습니다 ({_exc}). "
            f"smart_factory_sim/patrol_planner.py 가 있는지 확인하세요.")
        print("⚠ " + _PATROL_IMPORT_ERROR)

STREAM_PORT = int(os.environ.get("MINIMAP_PORT", "8091"))
DETECTION_UDP_PORT = int(os.environ.get("DETECTION_UDP_PORT", "9091"))

# 이 프로세스가 뜰 때마다 새로 만들어지는 식별자.
# SpatialObjectTracker.next_id 는 재시작하면 1부터 다시 시작하므로, tracker id
# 하나만으로는 "예전에 본 1번"과 "방금 새로 잡힌 1번"을 구분할 수 없습니다.
# /detections 응답에 함께 실어서 event_logger 가 (session, id) 로 판단하게 합니다.
SESSION_ID = hashlib.sha1(f"{time.time()}:{os.getpid()}".encode()).hexdigest()[:12]
# 카메라가 로봇 정면과 정확히 같은 방향을 보고 있지 않다면 여기에 오프셋(라디안)을 넣음.
# 로봇 뒤쪽을 보는 장착은 pi(3.141592653589793) 또는 -pi를 쓴다.
CAMERA_YAW_OFFSET = float(os.environ.get("CAMERA_YAW_OFFSET", "0.0"))
DETECTION_MAX_RANGE = float(os.environ.get("DETECTION_MAX_RANGE", "8.0"))  # 레이캐스팅 최대 탐색 거리(m)

# 탐지 레이 주변 이 폭(m) 안에 있는 실시간 LiDAR 장애물은 "그 방향에 있는 것"으로 본다.
# 사람 한 명의 폭(어깨~0.5m)과 카메라 각도 오차를 함께 감안한 값이다. 너무 좁히면
# 각도가 조금만 틀어져도 사람을 지나쳐 뒤쪽 벽을 잡고, 너무 넓히면 옆에 서 있던
# 다른 물체를 사람으로 잘못 집는다.
DETECTION_RAY_CORRIDOR_M = float(os.environ.get("DETECTION_RAY_CORRIDOR_M", "0.45"))

# 공간 클러스터링(중복 마커 병합) 파라미터
DETECTION_DIST_THRESHOLD = float(os.environ.get("DETECTION_DIST_THRESHOLD", "0.35"))  # 이 거리(m) 이내면 같은 물체로 병합
DETECTION_MIN_HITS = int(os.environ.get("DETECTION_MIN_HITS", "3"))       # 최소 이 횟수만큼 검출돼야 화면에 표시
DETECTION_EMA_ALPHA = float(os.environ.get("DETECTION_EMA_ALPHA", "0.25"))  # 위치 보정 강도 (낮을수록 부드럽게 고정)
DETECTION_STALE_SEC = float(os.environ.get("DETECTION_STALE_SEC", "30.0"))  # 이 시간 재검출 없으면 삭제

ROBOT_MARKER_RADIUS = 6
DETECTION_MARKER_RADIUS = 5

# MJPEG 미니맵(/minimap_feed)의 탐지 마커 색과 표기.
#
# 예전에는 모든 탐지가 똑같은 빨간 점 + 영문 원문("person #3")이었다. 그러면
# 화면만 봐서는 작업자인지 화재인지 구분할 수 없어, 젯슨이 person 을 제대로
# 보내도 "작업자로 마킹됐다" 고 말할 수 없었다. minimap_web.html 의 범례와
# 같은 분류·같은 색을 쓴다.
#
# 색은 (B, G, R) — OpenCV 순서이고, minimap_web.html 의 CSS 토큰과 같은 값이다.
# 이름을 한글로 쓰지 않는 이유: cv2.putText 는 한글 글꼴이 없어 "???" 로 찍힌다.
# 한글 이름은 브라우저로 여는 미니맵 페이지(minimap_web.html)가 보여준다.
DETECTION_MARKER_STYLE = {
    "fire":                  ((18, 58, 210),  "FIRE"),        # --fire   #D23A12
    "person with no helmet": ((0, 108, 169),  "NO-HELMET"),   # --helmet #A96C00
    "person without helmet": ((0, 108, 169),  "NO-HELMET"),
    "no safety helmet":      ((0, 108, 169),  "NO-HELMET"),
    "no_helmet":             ((0, 108, 169),  "NO-HELMET"),
    "helmet_violation":      ((0, 108, 169),  "NO-HELMET"),
    "person":                ((217, 111, 30), "WORKER"),      # --person  #1E6FD9
    "human":                 ((217, 111, 30), "WORKER"),
    "vehicle":               ((168, 78, 107), "VEHICLE"),     # --vehicle #6B4EA8
}
DETECTION_MARKER_FALLBACK = ((136, 119, 106), None)           # --other  #6A7788


def detection_marker_style(label):
    """탐지 라벨 -> (색, 표기). 모르는 라벨은 회색 + 원문 그대로."""
    key = str(label or "").strip()
    style = DETECTION_MARKER_STYLE.get(key) or DETECTION_MARKER_STYLE.get(key.lower())
    if style is None:
        return DETECTION_MARKER_FALLBACK[0], (key or "unknown")
    return style[0], style[1]

MJPEG_FPS = float(os.environ.get("MINIMAP_MJPEG_FPS", "5"))  # 미니맵은 영상만큼 빠를 필요가 없다

# ------------------------------------------------------------------
# 접근 제어
#
# 이 서버가 내보내는 것: 공장 도면, 로봇 실시간 위치, 화재 이벤트,
# 그리고 작업자 위치와 헬멧 미착용 이력(개인정보 성격).
#
#   MINIMAP_BIND   바인딩할 인터페이스. 기본값은 기존 동작을 유지하기 위해
#                  0.0.0.0 이지만, 실제 운영에서는 내부망 IP 하나만 지정하세요.
#                    예) export MINIMAP_BIND=203.0.113.20
#   MINIMAP_TOKEN  설정하면 모든 요청에 ?t=<토큰> 또는 X-Minimap-Token 헤더가
#                  필요합니다. 미니맵 페이지는 ?t=<토큰> 으로 열면 됩니다.
#
# CORS 헤더는 의도적으로 넣지 않습니다. Access-Control-Allow-Origin: * 를 켜면
# 관제 PC 사용자가 방문한 임의의 웹사이트가 공장 지도를 읽어갈 수 있습니다.
# ------------------------------------------------------------------
MINIMAP_BIND = os.environ.get("MINIMAP_BIND", "0.0.0.0")
MINIMAP_TOKEN = os.environ.get("MINIMAP_TOKEN", "").strip()

# 이 페이지를 iframe 으로 감쌀 수 있는 출처.
# 대시보드(이상민 PC)가 다른 호스트/포트에서 돌기 때문에 기본값은 허용(*)입니다.
# 이 엔드포인트는 쿠키나 세션을 쓰지 않아 클릭재킹으로 훔칠 권한이 없고,
# 실질적인 차단은 MINIMAP_BIND(네트워크)와 MINIMAP_TOKEN(인증)이 담당합니다.
# 그래도 조이고 싶다면 대시보드 출처를 정확히 지정하세요:
#   export MINIMAP_FRAME_ANCESTORS="'self' http://203.0.113.21:8501"
MINIMAP_FRAME_ANCESTORS = os.environ.get("MINIMAP_FRAME_ANCESTORS", "*").strip()

# 지도를 눌러 로봇을 보내는 기능(디지털 트윈). 이건 화면 표시가 아니라 **실물 로봇을
# 움직이는 명령**이라, 토큰 없이는 아예 열지 않는다. MINIMAP_TOKEN 이 비어 있으면
# 기능 자체가 꺼진다 — 같은 와이파이의 누구나 공장 로봇을 조종할 수 있게 되는 것을
# 막기 위해서다. (지도를 보여주기만 할 때와 위험의 성격이 다르다.)
MINIMAP_GOTO_ENABLED = os.environ.get("MINIMAP_ALLOW_GOTO", "1").strip() not in ("0", "false", "False")
# 목표점이 벽/미탐사 영역이면 로봇이 영영 도달하지 못하고 Nav2 가 실패를 반복한다.
# 클릭 지점 주변 이 반경(m) 안에 점유 셀이 없어야 명령을 받는다.
GOTO_CLEARANCE_M = float(os.environ.get("MINIMAP_GOTO_CLEARANCE_M", "0.25"))

# 순찰 구역 자동 분할.
#
# 구역 좌표를 손으로 지정해 두지 않았으므로, SLAM 지도가 들어오면 자유 공간을
# 면적 기준으로 자동으로 나눈다. k-means 를 좌표에 돌리면 벽 모양을 따라 덩어리가
# 지고 면적도 대체로 고르게 갈린다(격자로 자르면 한 구역이 두 방에 걸친다).
#
# 주의: 이건 **화면 표시용**이다. DB(patrol_logs.zone)에 적히는 구역은 파이프라인의
# --zone 인자로 정해지며, 이 자동 분할과는 별개다. 둘을 맞추려면 사람이 확인하고
# --zone 을 그에 맞게 주어야 한다.
ZONE_COUNT = int(os.environ.get("MINIMAP_ZONE_COUNT", "3"))
ZONE_NAMES = [n.strip() for n in
              os.environ.get("MINIMAP_ZONE_NAMES", "A,B,C,D,E,F").split(",") if n.strip()]
# 로봇 상태(배터리/속도/순찰)를 "지금 값"으로 믿어도 되는 시간.
# 이걸 넘기면 화면에 "연결 끊김"으로 표시한다. 마지막 값을 그대로 두면
# 배터리가 멈춰 있는 건지 원래 그 값인지 구분할 수 없다.
ROBOT_STATUS_STALE_SEC = float(os.environ.get("MINIMAP_STATUS_STALE_SEC", "10.0"))

# TF가 이 시간 이상 끊기면 지도에 남아 있던 마지막 위치를 지운다. 낡은
# 좌표로 내비게이션을 시작하는 것은 위치를 모르는 것보다 더 위험하다.
POSE_STALE_SEC = max(
    0.1, float(os.environ.get("MINIMAP_POSE_STALE_SEC", "1.0")))

# HTTP 요청 스레드는 이 시간까지 /navigation_status의 접수/거부 확인을
# 기다린다. 확인이 안 오면 성공으로 가장하지 않고 HTTP 202 pending을 돌려준다.
_NAV_ACK_TIMEOUT_VALUE = os.environ.get(
    "MINIMAP_NAVIGATION_ACK_TIMEOUT_SEC",
    os.environ.get(
        "MINIMAP_COMMAND_ACK_TIMEOUT_SEC",
        os.environ.get("MINIMAP_COMMAND_TIMEOUT_SEC", "1.5"),
    ),
)
NAVIGATION_ACK_TIMEOUT_SEC = max(0.0, float(_NAV_ACK_TIMEOUT_VALUE))
NAVIGATION_REQUEST_TTL_SEC = max(
    30.0,
    float(os.environ.get("MINIMAP_NAVIGATION_REQUEST_TTL_SEC", "300.0")),
)
NAVIGATION_RESULT_CACHE_SIZE = 128

# 셀이 수십만 개면 k-means 가 느리다. 이 간격으로 솎아서 중심만 구하고,
# 라벨은 전체 셀에 최근접으로 다시 붙인다.
ZONE_SAMPLE_STRIDE = int(os.environ.get("MINIMAP_ZONE_SAMPLE_STRIDE", "3"))

# 순찰 경로 계획 (patrol_planner.py 가 쓰는 값들).
# 이 면적마다 순찰 지점 하나. 줄이면 촘촘하게 돌지만 한 바퀴가 길어집니다.
PATROL_AREA_PER_POINT_M2 = float(
    os.environ.get("PATROL_AREA_PER_POINT_M2", "9.0"))
PATROL_MAX_POINTS_PER_ZONE = int(
    os.environ.get("PATROL_MAX_POINTS_PER_ZONE", "3"))
# 순찰 지점이 벽에서 떨어져 있어야 할 거리. 지도 클릭 이동의 기준과 맞춥니다.
PATROL_MIN_CLEARANCE_M = float(
    os.environ.get("PATROL_MIN_CLEARANCE_M", str(GOTO_CLEARANCE_M)))
PATROL_MIN_SPACING_M = float(os.environ.get("PATROL_MIN_SPACING_M", "1.5"))

HERE = os.path.dirname(os.path.abspath(__file__))

# occupancy grid 판정 임계값 — raycast_to_obstacle() 과 반드시 같은 값을 쓴다
OCC_THRESHOLD = 50

# 지도에 없던 물체(사람/지게차 등)를 현재 /scan 에서 잡아낸다. 지도에 이미 있는 벽
# 가까이의 스캔 끝점은 제외하고, 남은 점을 덩어리로 묶어 하나의 물체로 본다.
#
# 이 결과는 **화면에 그리지 않는다.** 젯슨 탐지 각도를 지도 좌표로 옮길 때
# raycast_to_live_obstacle() 이 쓰는 내부 계산용이다 (사람은 SLAM 지도에 없어서
# 이것 없이는 작업자 마커를 찍을 자리를 알 수 없다).
LIVE_OBSTACLE_MAP_MATCH_RADIUS_M = float(
    os.environ.get("LIVE_OBSTACLE_MAP_MATCH_RADIUS_M", "0.20"))
LIVE_OBSTACLE_CLUSTER_M = float(os.environ.get("LIVE_OBSTACLE_CLUSTER_M", "0.20"))
LIVE_OBSTACLE_MIN_RETURNS = int(os.environ.get("LIVE_OBSTACLE_MIN_RETURNS", "3"))
LIVE_OBSTACLE_MAX_SPAN_M = float(os.environ.get("LIVE_OBSTACLE_MAX_SPAN_M", "1.80"))
LIVE_OBSTACLE_SCAN_STRIDE = int(os.environ.get("LIVE_OBSTACLE_SCAN_STRIDE", "4"))
LIVE_OBSTACLE_STALE_SEC = float(os.environ.get("LIVE_OBSTACLE_STALE_SEC", "1.5"))


def compute_zones(grid_msg: OccupancyGrid, n_zones: int):
    """자유 공간을 면적 기준으로 n_zones 개로 나눈다.

    반환: (라벨 래스터 uint8, 구역 정보 리스트)
      라벨 래스터: 0 = 구역 없음(벽/미탐사), 1..n = 구역 번호
      구역 정보: [{"name","x","y","area_m2","cells"}, ...]

    구역 이름은 중심의 x 좌표 순으로 붙인다. k-means 가 돌 때마다 라벨 번호를
    다르게 주기 때문에, 정렬하지 않으면 지도를 다시 받을 때마다 A 와 C 가
    뒤바뀐다.
    """
    info = grid_msg.info
    width, height = int(info.width), int(info.height)
    resolution = float(info.resolution)
    if width <= 0 or height <= 0 or resolution <= 0 or n_zones < 1:
        return None, []

    data = np.asarray(grid_msg.data, dtype=np.int16)
    if data.size != width * height:
        return None, []
    data = data.reshape((height, width))

    # map_saver_cli 기본 free_thresh(0.25)로 저장한 지도를 다시 읽으면 미탐사
    # 회색(205)이 자유 셀로 바뀌어 캔버스 전체가 사각형 구역처럼 보일 수 있다.
    # 그 상태에서는 화면 구역도 만들지 않는다. 실제 순찰 플래너도 같은 검사를
    # 수행해 잘못된 웨이포인트 전송을 거부한다.
    if patrol_planner is not None:
        grid = patrol_planner.GridInfo(
            data, resolution, float(info.origin.position.x), float(info.origin.position.y))
        if patrol_planner.has_suspicious_free_border(grid):
            return None, []

    # 알려진 자유 공간만 대상 (미탐사 -1 과 점유 셀은 제외)
    free = (data >= 0) & (data < OCC_THRESHOLD)
    free_rc = np.argwhere(free)
    if len(free_rc) < n_zones:
        return None, []

    stride = max(1, ZONE_SAMPLE_STRIDE)
    sample = free_rc[::stride].astype(np.float32)
    if len(sample) < n_zones:
        sample = free_rc.astype(np.float32)

    cv2.setRNGSeed(0)   # 실행마다 구역이 달라지지 않도록 고정
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1.0)
    _compactness, _labels, centers = cv2.kmeans(
        sample, n_zones, None, criteria, 5, cv2.KMEANS_PP_CENTERS)

    # 이름은 왼쪽 열부터, 같은 열 안에서는 위에서 아래로 붙인다.
    # x 만으로 정렬하면 세로로 나란한 두 구역의 중심 x 가 몇 cm 차이로 갈려서
    # 지도를 다시 받을 때마다 이름이 뒤바뀐다. 열 폭으로 묶어서 그걸 막는다.
    col_bucket = max(1.0, width / max(1, n_zones)) / 2.0
    order = sorted(range(len(centers)),
                   key=lambda i: (round(centers[i][1] / col_bucket), centers[i][0]))
    centers = centers[order]

    # 전체 자유 셀을 가장 가까운 중심에 배정 (솎아낸 표본이 아니라 전부)
    diff = free_rc[:, None, :].astype(np.float32) - centers[None, :, :]
    assign = np.argmin((diff ** 2).sum(axis=2), axis=1)

    raster = np.zeros((height, width), dtype=np.uint8)
    raster[free_rc[:, 0], free_rc[:, 1]] = (assign + 1).astype(np.uint8)

    cell_area = resolution * resolution
    zones = []
    for idx in range(n_zones):
        members = free_rc[assign == idx]
        if len(members) == 0:
            continue
        mean_row, mean_col = members.mean(axis=0)
        zones.append({
            "name": ZONE_NAMES[idx] if idx < len(ZONE_NAMES) else f"Z{idx + 1}",
            "index": idx + 1,
            "x": float(info.origin.position.x + mean_col * resolution),
            "y": float(info.origin.position.y + mean_row * resolution),
            "area_m2": round(float(len(members) * cell_area), 1),
            "cells": int(len(members)),
        })
    return raster, zones


def extract_live_scan_obstacles(grid_msg: OccupancyGrid, scan: LaserScan,
                                scan_transform, data=None):
    """저장 지도에 없는 /scan 반환점을 벽 이외의 실시간 장애물 중심으로 묶는다.

    data 는 미리 만들어 둔 지도 배열(grid_to_array). 이 함수는 /scan 마다
    불리므로(5~10Hz) 매번 다시 만들면 낭비입니다.
    """
    if grid_msg is None or not scan.ranges:
        return []
    info = grid_msg.info
    width, height = int(info.width), int(info.height)
    resolution = float(info.resolution)
    if width <= 0 or height <= 0 or resolution <= 0:
        return []
    if data is None:
        data = grid_to_array(grid_msg)
    if data is None or data.shape != (height, width):
        return []

    tx = float(scan_transform.transform.translation.x)
    ty = float(scan_transform.transform.translation.y)
    scan_yaw = quaternion_to_yaw(scan_transform.transform.rotation)
    match_cells = max(1, int(math.ceil(LIVE_OBSTACLE_MAP_MATCH_RADIUS_M / resolution)))
    stride = max(1, LIVE_OBSTACLE_SCAN_STRIDE)
    bucket_m = max(resolution, LIVE_OBSTACLE_CLUSTER_M)
    buckets = {}

    for index in range(0, len(scan.ranges), stride):
        distance = float(scan.ranges[index])
        if not math.isfinite(distance) or distance < scan.range_min or distance > scan.range_max:
            continue
        angle = scan_yaw + scan.angle_min + index * scan.angle_increment
        x = tx + distance * math.cos(angle)
        y = ty + distance * math.sin(angle)
        col = int((x - info.origin.position.x) / resolution)
        row = int((y - info.origin.position.y) / resolution)
        if not (0 <= row < height and 0 <= col < width):
            continue
        # 이 지도의 점유 셀(벽/기존 설비)과 가까우면 새 장애물이 아니다.
        r0, r1 = max(0, row - match_cells), min(height, row + match_cells + 1)
        c0, c1 = max(0, col - match_cells), min(width, col + match_cells + 1)
        if np.any(data[r0:r1, c0:c1] >= OCC_THRESHOLD):
            continue
        key = (math.floor(x / bucket_m), math.floor(y / bucket_m))
        buckets.setdefault(key, []).append((x, y))

    # 인접한 버킷을 하나의 물체로 합친다. 사람/상자 표면에서 나온 여러 beam이
    # 여러 빨간 점으로 흩어지는 것을 막는다.
    obstacles, remaining = [], set(buckets)
    while remaining:
        start = remaining.pop()
        stack, keys = [start], [start]
        while stack:
            bx, by = stack.pop()
            for nx in range(bx - 1, bx + 2):
                for ny in range(by - 1, by + 2):
                    neighbor = (nx, ny)
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        stack.append(neighbor)
                        keys.append(neighbor)
        points = [point for key in keys for point in buckets[key]]
        if len(points) < LIVE_OBSTACLE_MIN_RETURNS:
            continue
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        if max(xs) - min(xs) > LIVE_OBSTACLE_MAX_SPAN_M or max(ys) - min(ys) > LIVE_OBSTACLE_MAX_SPAN_M:
            continue
        obstacles.append({
            "x": float(sum(xs) / len(xs)),
            "y": float(sum(ys) / len(ys)),
            "cells": len(points),
            "source": "scan",
        })
    return obstacles


class TrackedObject:

    def __init__(self, obj_id: int, label: str, x: float, y: float):
        self.id = obj_id
        self.label = label
        self.x = x
        self.y = y
        self.count = 1                
        self.last_seen = time.time()  

    def update(self, new_x: float, new_y: float, alpha: float = DETECTION_EMA_ALPHA):
        self.x = (1 - alpha) * self.x + alpha * new_x
        self.y = (1 - alpha) * self.y + alpha * new_y
        self.count += 1
        self.last_seen = time.time()


class SpatialObjectTracker:

    def __init__(self, dist_threshold=DETECTION_DIST_THRESHOLD,
                 max_stale_sec=DETECTION_STALE_SEC, min_hits=DETECTION_MIN_HITS):
        self.dist_threshold = dist_threshold
        self.max_stale_sec = max_stale_sec
        self.min_hits = min_hits
        self.tracked_objects = []
        self.next_id = 1

    def update_or_add(self, label: str, x: float, y: float):
        best_match, min_dist = None, float('inf')


        for obj in self.tracked_objects:
            if obj.label == label:
                dist = math.hypot(obj.x - x, obj.y - y)
                if dist < min_dist:
                    min_dist, best_match = dist, obj

        if best_match and min_dist <= self.dist_threshold:
            best_match.update(x, y) 
        else:
            new_obj = TrackedObject(self.next_id, label, x, y) 
            self.next_id += 1
            self.tracked_objects.append(new_obj)

    def prune_stale_objects(self):
        now = time.time()
        self.tracked_objects = [
            obj for obj in self.tracked_objects if (now - obj.last_seen) < self.max_stale_sec
        ]

    def get_valid_objects(self):
        return [obj for obj in self.tracked_objects if obj.count >= self.min_hits]


def grid_to_array(grid_msg):
    """OccupancyGrid.data(파이썬 **리스트**) -> (height, width) int16 배열.

    이 변환이 생각보다 비쌉니다. rclpy 는 data 를 파이썬 리스트로 주는데,
    429x428 지도면 원소가 18만 개라 배열로 바꾸는 데만 이 PC 기준 약 3.7ms 가
    걸립니다. 젯슨/노트북에서는 더 걸립니다.

    예전에는 raycast_to_obstacle() 이 **탐지 UDP 패킷마다** 이 변환을 새로 했습니다.
    정작 읽는 것은 레이 위의 셀 몇 개뿐인데도요. 젯슨이 초당 20~30개의 각도 패킷을
    보내면 그것만으로 100ms/초 가까이 날아갑니다.

    지도는 자주 바뀌지 않으므로 _on_map 에서 한 번 만들어 두고 돌려씁니다.
    형식이 깨졌으면 None.
    """
    if grid_msg is None:
        return None
    info = grid_msg.info
    width, height = int(info.width), int(info.height)
    if width <= 0 or height <= 0:
        return None
    data = np.asarray(grid_msg.data, dtype=np.int16)
    if data.size != width * height:
        return None
    return data.reshape((height, width))


def quaternion_to_yaw(q) -> float:
    """ROS2 쿼터니언(orientation)에서 로봇이 바라보는 방향(yaw, 라디안)만 추출"""
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y**2 + q.z**2))


def raycast_to_obstacle(grid_msg, origin_xy, angle, max_range=8.0, data=None):
    """
    origin_xy(로봇 위치)에서 angle(map 좌표계 기준 절대각) 방향으로
    가상의 레이를 쏴서, occupancy grid 상 처음 만나는 장애물 좌표를 찾음.
    depth 센서 없이 "각도 + 이미 알고 있는 지도"만으로 거리를 추정하는 방식.

    data 에 미리 만든 배열을 주면 그것을 씁니다. **이 함수는 탐지 패킷마다
    불리므로**, 매번 리스트를 배열로 바꾸면 그것만으로 CPU 를 먹습니다
    (grid_to_array 주석 참고). 안 주면 예전처럼 직접 만듭니다.
    """
    resolution = grid_msg.info.resolution
    width, height = grid_msg.info.width, grid_msg.info.height
    origin_x = grid_msg.info.origin.position.x
    origin_y = grid_msg.info.origin.position.y
    if data is None:
        data = grid_to_array(grid_msg)
        if data is None:
            return None

    ox, oy = origin_xy
    dist = resolution  # 로봇 바로 앞부터 시작 (0에서 시작하면 자기 자신 위치가 걸릴 수 있음)
    while dist < max_range:
        x = ox + dist * math.cos(angle)
        y = oy + dist * math.sin(angle)
        col = int((x - origin_x) / resolution)
        row = int((y - origin_y) / resolution)
        if not (0 <= row < height and 0 <= col < width):
            break  # 지도 범위를 벗어남
        if data[row, col] >= 50:  # 50 이상이면 장애물로 판단
            return (x, y)
        dist += resolution
    return None  # 탐색 범위 내에 장애물을 못 찾음 (열린 공간에 있는 사람 등)


def raycast_to_live_obstacle(obstacles, origin_xy, angle,
                             max_range=DETECTION_MAX_RANGE,
                             corridor=DETECTION_RAY_CORRIDOR_M):
    """실시간 /scan 장애물(= 저장 지도에 없는 물체) 중 레이 방향에 놓인 가장 가까운
    것을 찾는다. 없으면 None.

    **사람은 SLAM 지도에 없다.** occupancy grid 는 벽·설비 같은 고정 구조물만
    담고, 사람은 지나가면 사라지므로 매핑되지 않는다. 그래서 raycast_to_obstacle()
    하나만 쓰면 젯슨이 "person" 을 아무리 정확히 잡아도

      - 열린 통로에 서 있는 작업자  -> 레이가 아무것도 못 맞혀 None -> **통째로 버려짐**
      - 벽 앞에 서 있는 작업자      -> 레이가 사람을 통과해 뒤쪽 벽에 찍힘 (몇 m 오차)

    가 되어, 미니맵에 작업자 마커가 아예 생기지 않거나 엉뚱한 벽에 붙었다.

    한편 이 노드는 이미 extract_live_scan_obstacles() 로 "지금 LiDAR 에는 보이는데
    저장 지도에는 없는 물체" 를 뽑고 있다 — 그게 바로 그 작업자다. 지금까지는 그
    결과를 빨간 점으로 그리기만 하고 탐지 위치 추정에는 쓰지 않았다. 여기서 쓴다.

    판정은 레이와의 수직 거리(corridor)로 한다. 각 장애물을 레이 방향으로 정사영해
    앞쪽(along > 0)에 있고 옆으로 corridor 이내인 것만 후보로 두고, 그중 가장 가까운
    것을 고른다. 카메라 각도와 LiDAR 는 오차가 있으므로 정확히 일치할 수 없다.
    """
    ox, oy = origin_xy
    dx, dy = math.cos(angle), math.sin(angle)

    best_hit, best_along = None, float('inf')
    for obstacle in obstacles:
        rx = float(obstacle["x"]) - ox
        ry = float(obstacle["y"]) - oy
        along = rx * dx + ry * dy            # 레이 방향으로 얼마나 앞에 있는가
        if along <= 0.0 or along > max_range:
            continue                          # 뒤쪽이거나 너무 멀다
        lateral = abs(-rx * dy + ry * dx)     # 레이에서 옆으로 얼마나 벗어났는가
        if lateral > corridor:
            continue
        if along < best_along:
            best_along = along
            best_hit = (float(obstacle["x"]), float(obstacle["y"]))
    return best_hit


class _PlanUnavailable:
    """플래너 자체를 못 불러왔을 때 PatrolPlan 자리에 놓는 값.

    호출부가 .ok / .reason / .as_dict() 만 보므로 이 셋만 흉내 냅니다.
    """

    ok = False
    waypoints = ()
    total_distance_m = 0.0

    def __init__(self, reason: str):
        self.reason = reason or "순찰 경로 플래너를 사용할 수 없습니다."

    def as_dict(self):
        return {"ok": False, "reason": self.reason, "waypoints": [],
                "zones": [], "total_distance_m": 0.0, "warnings": []}


class NavigationPublishError(Exception):
    """ROS 제어 토픽 발행 자체가 실패했음을 HTTP 계층까지 구분해 전달한다."""


class MinimapNode(Node):
    """ROS2 토픽을 구독해서 최신 맵/로봇위치/방향을 계속 최신 상태로 들고 있는 노드"""

    def __init__(self):
        super().__init__('minimap_renderer')
        self.latest_map = None     # 가장 최근에 받은 OccupancyGrid 메시지
        # 위 메시지의 numpy 사본. 리스트->배열 변환이 비싸서(18만 원소, ~3.7ms)
        # 지도가 바뀔 때 한 번만 만들고 돌려씁니다. grid_to_array() 주석 참고.
        self.map_array = None
        self.live_scan_obstacles = [] # 저장 지도에 없는 현재 /scan 장애물 (화면 표시 아님)
        # stall_monitor 가 찾은 "LiDAR 에 안 보이는 낮은 장애물"(문턱·케이블 등).
        # 센서로는 영영 안 보이므로 지도에도 없고, 사람이 치우러 가야 합니다.
        self.blind_obstacles = []
        self.live_scan_seen = 0.0
        self.zone_raster = None    # 자유 공간을 자동 분할한 구역 라벨 (0=없음, 1..n)
        self.zones = []            # 구역 이름/중심/면적
        self.patrol_route = []     # patrol_node 가 알려준 실제 순찰 경로
        self.patrol_loop = True

        # 로봇 상태. 아직 한 번도 안 왔으면 None 으로 두고, 화면에서는
        # "값 없음"과 "0%"를 구분해서 보여준다.
        self.battery = None        # {"percent","voltage","charging","present"}
        self.battery_seen = 0.0
        self.speed = None          # {"linear","angular"}
        self.speed_seen = 0.0
        self.patrol_status = None  # {"state","index","total"}
        self.patrol_seen = 0.0
        self.robot_pose = None     # (x, y) - 맵과 같은 좌표계(미터 단위)
        self.robot_yaw = None      # 로봇이 바라보는 방향 (라디안)
        self.pose_seen = 0.0       # 마지막으로 성공한 map -> base_footprint TF

        # 단일 내비게이션 제어기(patrol_node)가 보내는 명령 접수·실행
        # 상태. Flask 요청은 request_id별 Condition에서 짧게 대기한다.
        self.navigation_status = None
        self.navigation_seen = 0.0
        self.navigation_results = {}
        self.navigation_result_seen = {}
        self.navigation_pending = {}
        self.navigation_request_sequence = 0
        self.tracker = SpatialObjectTracker()  # 화각 기반 추정 위치를 중복 제거/평활화하며 추적
        self.lock = threading.Lock()  # ROS2 콜백/UDP 스레드/Flask 스레드가 동시에 접근
        self.navigation_condition = threading.Condition(self.lock)

        # TODO: SLAM 노드가 실제로 발행하는 맵 토픽 이름으로 변경 (보통 '/map')
        # Cartographer는 volatile /map을 계속 발행하고, Nav2 map_server는
        # transient-local /map을 기동 때 한 번만 발행한다. 둘 다 받기 위해 QoS가
        # 다른 구독을 함께 둔다. 이중 수신은 같은 최신 지도로 덮어쓰므로 안전하다.
        self.create_subscription(OccupancyGrid, '/map', self._on_map, 10)
        saved_map_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(OccupancyGrid, '/map', self._on_map, saved_map_qos)
        scan_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.create_subscription(LaserScan, '/scan', self._on_scan, scan_qos)
        # Gazebo mock/실물 카메라 이벤트가 이미 map 좌표로 투영된 경우에는
        # 젯슨 각도 UDP 레이캐스팅을 거치지 않고 정확한 좌표를 바로 표시한다.
        # 이 경로의 /detections 결과도 event_logger.py가 DB에 기록한다.
        self.create_subscription(String, '/detected_events', self._on_detected_event, 10)
        # 도메인 30에서 확정된 지도 좌표 탐지를 디지털 트윈 브리지가 읽는다.
        # 원시 프레임이 아니라 min_hits/공간 병합을 통과한 추적 목록 전체를
        # 발행하므로 Gazebo에 같은 사람 모델이 프레임마다 늘어나지 않는다.
        self.tracked_detections_pub = self.create_publisher(
            String, '/tracked_detections', 10)
        self.create_timer(1.0, self._publish_tracked_detections)

        # stall_monitor 는 TRANSIENT_LOCAL 로 발행합니다. 미니맵이 나중에 떠도
        # 마지막 목록을 받으려면 같은 durability 여야 합니다.
        blind_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(String, '/blind_obstacles',
                                 self._on_blind_obstacles, blind_qos)

        # 순찰 경로. patrol_node 가 TRANSIENT_LOCAL 로 한 번만 발행하므로,
        # 받는 쪽도 같은 durability 여야 늦게 떠도 받을 수 있다.
        route_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(String, '/patrol_route', self._on_patrol_route, route_qos)

        # 로봇 상태. 배터리는 터틀봇이 실제로 발행할 때만 들어온다 —
        # 시뮬레이션이나 배터리 미장착 상태에서는 아무것도 오지 않으므로,
        # 여기서 기본값을 만들어 두지 않는다(없는 값을 있는 것처럼 보이면 안 된다).
        self.create_subscription(BatteryState, '/battery_state', self._on_battery, 10)
        self.create_subscription(Odometry, '/odom', self._on_odom, 10)
        self.create_subscription(String, '/patrol_status', self._on_patrol_status, 10)
        self.create_subscription(String, '/navigation_status',
                                 self._on_navigation_status, 10)

        # 지도 이동과 순찰은 모두 하나의 제어기가 직렬화한다. 미니맵이
        # Nav2 목표를 직접 발행하면 순찰 목표와 경쟁하므로 /patrol_control만 쓴다.
        self.patrol_control_pub = self.create_publisher(String, '/patrol_control', 10)

        # /amcl_pose 대신 tf2를 사용하여 map -> base_footprint 위치를 자동으로 가져옵니다.
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.timer = self.create_timer(0.1, self._on_timer)

    def _on_timer(self):
        now = time.time()
        try:
            # map 좌표계 기준 로봇(base_footprint)의 절대 위치를 초당 10번씩 자동으로 찾습니다.
            t = self.tf_buffer.lookup_transform('map', 'base_footprint', rclpy.time.Time())
            with self.lock:
                self.robot_pose = (t.transform.translation.x, t.transform.translation.y)
                self.robot_yaw = quaternion_to_yaw(t.transform.rotation)
                self.pose_seen = now
        except TransformException:
            # TF가 끊긴 뒤에도 마지막 좌표를 실시간처럼 남겨 두지 않는다.
            with self.lock:
                self._expire_pose_locked(now)

    def _on_map(self, msg: OccupancyGrid):
        # 배열 변환과 구역 분할은 지도가 바뀔 때만 하면 된다 (/map 은 자주 오지 않는다).
        array = grid_to_array(msg)
        zone_raster, zones = compute_zones(msg, ZONE_COUNT)
        with self.lock:
            self.latest_map = msg
            self.map_array = array
            self.zone_raster = zone_raster
            self.zones = zones

    def _on_scan(self, msg: LaserScan):
        """현재 LiDAR에서 지도에 없던 물체만 추려 보관한다.

        화면에는 그리지 않는다. 젯슨 탐지 각도를 지도 좌표로 옮길 때
        raycast_to_live_obstacle() 이 참고하는 값이다.
        """
        try:
            transform = self.tf_buffer.lookup_transform(
                'map', msg.header.frame_id, rclpy.time.Time())
        except TransformException:
            return
        with self.lock:
            grid_msg = self.latest_map
            map_array = self.map_array
        obstacles = extract_live_scan_obstacles(grid_msg, msg, transform, map_array)
        with self.lock:
            self.live_scan_obstacles = obstacles
            self.live_scan_seen = time.time()

    def _on_pose(self, msg: PoseWithCovarianceStamped):
        pass  # 더 이상 사용하지 않음 (tf2가 대신함)

    def _on_battery(self, msg: BatteryState):
        percent = float(msg.percentage) if msg.percentage is not None else float('nan')
        # 드라이버마다 0~1 로 주기도 하고 0~100 으로 주기도 한다.
        if math.isfinite(percent) and percent <= 1.0:
            percent *= 100.0
        # POWER_SUPPLY_STATUS_CHARGING = 1
        charging = int(getattr(msg, 'power_supply_status', 0)) == 1
        with self.lock:
            self.battery = {
                "percent": round(percent, 1) if math.isfinite(percent) else None,
                "voltage": round(float(msg.voltage), 2) if math.isfinite(float(msg.voltage)) else None,
                "charging": charging,
                "present": bool(getattr(msg, 'present', True)),
            }
            self.battery_seen = time.time()

    def _on_odom(self, msg: Odometry):
        twist = msg.twist.twist
        with self.lock:
            self.speed = {
                "linear": round(float(twist.linear.x), 3),
                "angular": round(float(twist.angular.z), 3),
            }
            self.speed_seen = time.time()

    def _on_patrol_status(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        with self.lock:
            self.patrol_status = {
                "state": str(payload.get("state", "unknown")),
                "index": int(payload.get("index", 0)),
                "total": int(payload.get("total", 0)),
            }
            self.patrol_seen = time.time()

    def _on_navigation_status(self, msg: String):
        """patrol_node의 명령 접수·Nav2 실행 상태를 request_id별로 보관한다."""
        try:
            payload = json.loads(msg.data)
            if not isinstance(payload, dict):
                raise TypeError
        except (TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning(
                '형식이 잘못된 /navigation_status 메시지를 건너뜁니다.')
            return

        phase = str(payload.get("phase", payload.get("status", "unknown"))).lower()
        request_value = payload.get("request_id")
        request_id = None if request_value in (None, "") else str(request_value)
        distance = payload.get("distance_remaining")
        try:
            distance = float(distance) if distance is not None else None
        except (TypeError, ValueError):
            distance = None
        if distance is not None and not math.isfinite(distance):
            distance = None

        goal = payload.get("goal")
        if not isinstance(goal, dict):
            goal = None
        elif goal is not None:
            try:
                gx, gy = float(goal["x"]), float(goal["y"])
                gyaw = float(goal.get("yaw", 0.0))
                if not all(math.isfinite(v) for v in (gx, gy, gyaw)):
                    raise ValueError
                goal = {"x": gx, "y": gy, "yaw": gyaw}
            except (KeyError, TypeError, ValueError):
                goal = None

        status = {
            "request_id": request_id,
            "command": str(payload.get("command", "")),
            "goal_type": str(payload.get("goal_type", "")),
            "phase": phase,
            # 이전 클라이언트는 status 필드를 읽었으므로 동일한 값을 유지한다.
            "status": phase,
            "state": str(payload.get("state", "unknown")),
            "goal": goal,
            "message": str(payload.get("message", "")),
            "error": (None if payload.get("error") in (None, "")
                      else str(payload.get("error"))),
            "distance_remaining": distance,
            "generation": payload.get("generation"),
            "complete": (payload.get("complete")
                         if isinstance(payload.get("complete"), bool) else None),
        }
        now = time.time()
        with self.navigation_condition:
            self.navigation_status = status
            self.navigation_seen = now
            if request_id is not None:
                # 기존 키를 먼저 빼고 다시 넣어 dict 삽입 순서를 "마지막 갱신"
                # 순서로 쓴다. 진행 중 순찰 ID가 오래됐다는 이유로 먼저 잘리지 않는다.
                self.navigation_results.pop(request_id, None)
                self.navigation_results[request_id] = status
                self.navigation_result_seen.pop(request_id, None)
                self.navigation_result_seen[request_id] = now

                # 제어기는 한 번에 하나의 command generation만 실행한다. 새 요청의
                # received는 이전 요청이 대체됐다는 뜻이다. 다만 이미 HTTP에서 발행돼
                # ROS 큐에 기다리는 더 *새로운* 요청의 예약까지 지우면 안 된다.
                if phase == "received":
                    current = self.navigation_pending.get(request_id)
                    current_seq = current[1] if isinstance(current, tuple) else None
                    for other_id, other in tuple(self.navigation_pending.items()):
                        other_seq = other[1] if isinstance(other, tuple) else None
                        if (other_id != request_id and current_seq is not None
                                and other_seq is not None and other_seq < current_seq):
                            self.navigation_pending.pop(other_id, None)

                if self._navigation_request_is_final(status):
                    self.navigation_pending.pop(request_id, None)

                # 장기 운영해도 완료된 HTTP 요청이 무한정 남지 않게 한다.
                while len(self.navigation_results) > NAVIGATION_RESULT_CACHE_SIZE:
                    oldest = next(iter(self.navigation_results))
                    self.navigation_results.pop(oldest, None)
                    self.navigation_result_seen.pop(oldest, None)
            self.navigation_condition.notify_all()

    @staticmethod
    def _navigation_request_is_final(status):
        """request_id를 다시 써도 되는 최종 상태인지 판정한다."""
        phase = str(status.get("phase", "")).lower()
        command = str(status.get("command", "")).lower()
        goal_type = str(status.get("goal_type", "")).lower()
        state = str(status.get("state", "")).lower()
        explicit = status.get("complete")
        if explicit is not None:
            return bool(explicit)
        if (goal_type == "patrol" and state == "patrolling"
                and command in ("start", "resume")
                and phase in ("rejected", "canceled", "succeeded", "failed")):
            # 개별 웨이포인트 결과이며 patrol_node가 재시도/다음 지점으로 이어 간다.
            return False
        if phase in ("rejected", "canceled"):
            return True
        if phase == "accepted" and command in ("stop", "pause"):
            return True
        if phase in ("succeeded", "failed"):
            # 한 웨이포인트 결과로 루프 순찰 request_id를 풀지 않는다. 다음 command의
            # received가 올 때 이전 예약을 정리한다.
            return not (goal_type == "patrol" and state == "patrolling")
        return False

    def get_navigation_status(self):
        """가장 최근 내비게이션 상태와 수신 경과 시간을 반환한다."""
        now = time.time()
        with self.lock:
            if self.navigation_status is None:
                return None
            result = dict(self.navigation_status)
            result["age_sec"] = round(max(0.0, now - self.navigation_seen), 2)
            return result

    def wait_for_navigation_status(self, request_id: str,
                                   timeout_sec=NAVIGATION_ACK_TIMEOUT_SEC):
        """request_id의 접수 또는 거부를 대기한다. 시간 초과는 None이다."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        # accepted/rejected/failed가 HTTP 응답의 주 계약이다. 매우 짧은
        # 이동이 accepted 직후 succeeded로 덮어써지는 경우도 접수 성공으로 본다.
        conclusive = {"accepted", "rejected", "failed", "succeeded", "canceled"}
        with self.navigation_condition:
            while True:
                status = self.navigation_results.get(str(request_id))
                if status is not None and status.get("phase") in conclusive:
                    return dict(status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.navigation_condition.wait(remaining)

    def patrol_controller_available(self):
        """ROS graph에 /patrol_control 구독자가 있는지 확인한다."""
        try:
            return int(self.patrol_control_pub.get_subscription_count()) > 0
        except (AttributeError, TypeError, ValueError):
            return False

    def prepare_navigation_request(self, request_id: str):
        """새 요청을 예약한다. 같은 ID의 동시 요청은 ACK 교차를 막기 위해 거부한다."""
        with self.navigation_condition:
            request_id = str(request_id)
            self._prune_navigation_pending_locked(time.time())
            if request_id in self.navigation_pending:
                return False
            self.navigation_request_sequence = int(
                getattr(self, "navigation_request_sequence", 0)) + 1
            self.navigation_pending[request_id] = (
                time.time(), self.navigation_request_sequence)
            self.navigation_results.pop(request_id, None)
            self.navigation_result_seen.pop(request_id, None)
            return True

    def release_navigation_request(self, request_id: str):
        with self.navigation_condition:
            self.navigation_pending.pop(str(request_id), None)

    def _prune_navigation_pending_locked(self, now):
        for request_id, entry in tuple(self.navigation_pending.items()):
            started = entry[0] if isinstance(entry, tuple) else float(entry)
            if now - started > NAVIGATION_REQUEST_TTL_SEC:
                self.navigation_pending.pop(request_id, None)

    def get_navigation_result(self, request_id: str):
        """request_id별 cached status, age, in-flight 여부를 원자적으로 반환한다."""
        now = time.time()
        with self.navigation_condition:
            self._prune_navigation_pending_locked(now)
            status = self.navigation_results.get(str(request_id))
            seen = self.navigation_result_seen.get(str(request_id))
            result = None if status is None else dict(status)
            age = None if seen is None else round(max(0.0, now - seen), 2)
            pending = str(request_id) in self.navigation_pending
            return result, age, pending

    def _expire_pose_locked(self, now=None):
        """lock 보유 중 호출. 낡은 TF 좌표를 지우고 age를 반환한다."""
        seen = float(getattr(self, "pose_seen", 0.0) or 0.0)
        age = None if seen <= 0 else max(0.0, (time.time() if now is None else now) - seen)
        if age is None or age > POSE_STALE_SEC:
            self.robot_pose = None
            self.robot_yaw = None
        return age

    def get_pose_state(self):
        """신선한 위치/방향과 마지막 TF의 경과 시간을 한번에 반환한다."""
        now = time.time()
        with self.lock:
            age = self._expire_pose_locked(now)
            pose = self.robot_pose
            yaw = self.robot_yaw
            return pose, yaw, (None if age is None else round(age, 2))

    def get_robot_status(self):
        """배터리/속도/순찰 상태를 신선도와 함께 돌려준다.

        각 항목마다 age_sec 을 같이 보내는 이유: 값만 보내면 받는 쪽이 "지금
        값"인지 "멈춘 값"인지 알 수 없다. 배터리처럼 천천히 변하는 값은
        멈춰 있어도 그럴듯해 보여서 특히 위험하다.
        """
        now = time.time()

        def fresh(value, seen):
            if value is None or seen <= 0:
                return None, None
            age = round(now - seen, 1)
            return (value if age <= ROBOT_STATUS_STALE_SEC else None), age

        with self.lock:
            battery, battery_age = fresh(self.battery, self.battery_seen)
            speed, speed_age = fresh(self.speed, self.speed_seen)
            patrol, patrol_age = fresh(self.patrol_status, self.patrol_seen)

        return {
            "battery": battery,
            "battery_age_sec": battery_age,
            "speed": speed,
            "speed_age_sec": speed_age,
            "patrol": patrol,
            "patrol_age_sec": patrol_age,
            "stale_after_sec": ROBOT_STATUS_STALE_SEC,
        }

    def _on_patrol_route(self, msg: String):
        """patrol_node 가 알려준 순찰 경로를 보관한다."""
        try:
            payload = json.loads(msg.data)
            points = [(float(p['x']), float(p['y'])) for p in payload['waypoints']]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning('형식이 잘못된 /patrol_route 메시지를 건너뜁니다.')
            return
        with self.lock:
            self.patrol_route = points
            self.patrol_loop = bool(payload.get('loop', True))
        self.get_logger().info(f'순찰 경로 {len(points)}개 지점을 받았습니다.')

    def _on_detected_event(self, msg: String):
        """event_detector의 지도 좌표 이벤트를 미니맵 추적기에 추가한다."""
        try:
            event = json.loads(msg.data)
            label = str(event['label'])
            x = float(event['x'])
            y = float(event['y'])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning('형식이 잘못된 /detected_events 메시지를 건너뜁니다.')
            return
        self.add_detection(label, x, y)

    def _publish_tracked_detections(self):
        """확정된 실제 탐지의 전체 스냅샷을 디지털 트윈에 제공한다."""
        objects = self.get_valid_detections()
        payload = {
            "session": SESSION_ID,
            "detections": [
                {
                    "id": int(obj.id),
                    "label": str(obj.label),
                    "x": float(obj.x),
                    "y": float(obj.y),
                    "hit_count": int(obj.count),
                    "last_seen": float(obj.last_seen),
                }
                for obj in objects
            ],
        }
        message = String()
        message.data = json.dumps(payload, ensure_ascii=False)
        self.tracked_detections_pub.publish(message)

    def _on_blind_obstacles(self, msg: String):
        """stall_monitor 가 알려준 낮은 장애물 목록을 보관한다."""
        try:
            payload = json.loads(msg.data)
            items = payload["obstacles"]
            if not isinstance(items, list):
                raise TypeError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning('형식이 잘못된 /blind_obstacles 메시지를 건너뜁니다.')
            return
        with self.lock:
            self.blind_obstacles = items
        self.get_logger().info(f'낮은 장애물 {len(items)}곳을 받았습니다.')

    def get_blind_obstacles(self):
        with self.lock:
            return [dict(item) for item in self.blind_obstacles]

    def add_detection(self, label: str, x: float, y: float):
        with self.lock:
            self.tracker.update_or_add(label, x, y)

    def cell_is_clear(self, x: float, y: float, clearance_m: float):
        """목표가 알려진 안전 영역이고 현재 로봇에서 연결되는지 본다."""
        with self.lock:
            grid_msg = self.latest_map
            data = self.map_array
            blind_obstacles = list(getattr(self, "blind_obstacles", ()))
            self._expire_pose_locked(time.time())
            pose = self.robot_pose
        if grid_msg is None:
            return False, "지도가 아직 준비되지 않았습니다."
        if pose is None:
            return False, "로봇 위치(TF)가 없거나 낡았습니다. 위치 추정을 확인하세요."
        info = grid_msg.info
        width, height = int(info.width), int(info.height)
        resolution = float(info.resolution)
        if width <= 0 or height <= 0 or resolution <= 0:
            return False, "지도 정보가 올바르지 않습니다."
        if data is None or data.shape != (height, width):
            raw = np.asarray(grid_msg.data, dtype=np.int16)
            if raw.size != width * height:
                return False, "지도 크기가 맞지 않습니다."
            data = raw.reshape((height, width))

        # int(-0.1) == 0 이므로 원점 바깥의 음수 좌표가 0번 셀로 잘려
        # 들어가던 버그를 막기 위해 반드시 floor를 쓴다.
        col = math.floor((x - info.origin.position.x) / resolution)
        row = math.floor((y - info.origin.position.y) / resolution)
        if not (0 <= row < height and 0 <= col < width):
            return False, "지도 바깥입니다."

        target_value = int(data[row, col])
        if target_value < 0:
            return False, "목표 셀이 아직 탐사되지 않은 구역입니다."
        if target_value >= OCC_THRESHOLD:
            return False, "목표 셀이 벽이나 장애물 위입니다."

        # stall_monitor가 기록한 문턱·케이블은 occupancy grid에는 free로 보인다.
        # Nav2 keepout filter에서 뒤늦게 거부되기 전에 클릭 단계에서 같은 영역을 막는다.
        for item in blind_obstacles:
            try:
                obstacle_x = float(item["x"])
                obstacle_y = float(item["y"])
                radius = max(0.0, float(item.get("radius", 0.20)))
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in (obstacle_x, obstacle_y, radius)):
                continue
            if math.hypot(x - obstacle_x, y - obstacle_y) <= (
                    radius + max(0.0, clearance_m)):
                return False, "기록된 낮은 장애물(문턱·케이블)의 안전 영역 안입니다."

        pad = max(0, int(math.ceil(max(0.0, clearance_m) / resolution)))
        r0, r1 = row - pad, row + pad + 1
        c0, c1 = col - pad, col + pad + 1
        if r0 < 0 or c0 < 0 or r1 > height or c1 > width:
            return False, "지도 경계에서 안전 거리를 확보할 수 없습니다."
        window = data[r0:r1, c0:c1]
        if np.any(window >= OCC_THRESHOLD):
            return False, "목표 주변에 벽이나 장애물이 있습니다."
        if np.any(window < 0):
            return False, "목표 주변에 아직 탐사되지 않은 영역이 있습니다."

        known_free = (data >= 0) & (data < OCC_THRESHOLD)
        if pad:
            kernel = np.ones((pad * 2 + 1, pad * 2 + 1), dtype=np.uint8)
            safe = cv2.erode(
                known_free.astype(np.uint8), kernel, iterations=1,
                borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
        else:
            safe = known_free

        robot_col = math.floor((pose[0] - info.origin.position.x) / resolution)
        robot_row = math.floor((pose[1] - info.origin.position.y) / resolution)
        if not (0 <= robot_row < height and 0 <= robot_col < width):
            return False, "현재 로봇 위치가 지도 바깥이어서 도달 가능성을 확인할 수 없습니다."

        # 먼저 알려진 자유 공간 자체가 연결되는지 본다. 닫힌 문 너머의
        # 빈 방을 목표로 받지 않게 하는 검사다.
        _free_count, free_labels = cv2.connectedComponents(
            known_free.astype(np.uint8), connectivity=8)
        robot_free_label = int(free_labels[robot_row, robot_col])
        target_free_label = int(free_labels[row, col])
        if robot_free_label <= 0 or robot_free_label != target_free_label:
            return False, "현재 로봇 위치에서 목표까지 연결된 자유 공간이 없습니다."

        # 통로가 로봇 여유 거리보다 좁으면 known_free는 연결되어도 safe는 둘로
        # 나뉜다. 현재 위치가 벽 근처면 같은 자유 성분의 가장 가까운 안전 셀을
        # 시작점으로 삼는다.
        _safe_count, safe_labels = cv2.connectedComponents(
            safe.astype(np.uint8), connectivity=8)
        target_safe_label = int(safe_labels[row, col])
        if target_safe_label <= 0:
            return False, "목표에서 안전 거리를 확보할 수 없습니다."

        start_safe_label = int(safe_labels[robot_row, robot_col])
        if start_safe_label <= 0:
            candidates = np.argwhere(safe & (free_labels == robot_free_label))
            if candidates.size == 0:
                return False, "로봇 주변에 연결된 안전 영역이 없습니다."
            delta = candidates - np.asarray((robot_row, robot_col))
            distance_sq = (delta * delta).sum(axis=1)
            best_index = int(np.argmin(distance_sq))
            # 로봇이 안전 마스크에서 너무 멀면 엉뚱한 연결 성분으로 순간
            # 이동하여 판정하지 않는다.
            max_start_cells = max(pad + 1, int(math.ceil(0.75 / resolution)))
            if int(distance_sq[best_index]) > max_start_cells * max_start_cells:
                return False, "로봇 주변에 연결된 안전 영역이 없습니다."
            nearest = candidates[best_index]
            start_safe_label = int(safe_labels[int(nearest[0]), int(nearest[1])])

        if start_safe_label != target_safe_label:
            return False, "목표까지 로봇의 안전 거리를 유지하는 경로가 없습니다."
        return True, ""

    def build_patrol_plan(self):
        """지금 지도로 순찰 경로를 만든다.

        구역(zones/zone_raster)은 **이 노드가 이미 나눠 둔 것을 그대로 넘깁니다.**
        플래너가 따로 나누면 화면의 "A 구역" 과 순찰의 "A 구역" 이 달라질 수
        있습니다 — 관제하는 사람이 둘을 대조할 수 없게 됩니다.
        """
        if patrol_planner is None:
            return _PlanUnavailable(_PATROL_IMPORT_ERROR)

        grid_msg, pose, _yaw = self.get_snapshot()
        if grid_msg is None:
            return patrol_planner.PatrolPlan.failed("지도가 아직 준비되지 않았습니다.")

        raster, zones = self.get_zones()
        grid = patrol_planner.GridInfo.from_grid_msg(grid_msg)
        if grid is None:
            return patrol_planner.PatrolPlan.failed("지도 크기 정보가 올바르지 않습니다.")

        return patrol_planner.plan_patrol_route(
            grid,
            robot_xy=pose,
            zones=zones or None,
            zone_raster=raster,
            zone_count=ZONE_COUNT,
            zone_names=ZONE_NAMES,
            min_clearance_m=PATROL_MIN_CLEARANCE_M,
            area_per_point_m2=PATROL_AREA_PER_POINT_M2,
            max_points_per_zone=PATROL_MAX_POINTS_PER_ZONE,
            min_spacing_m=PATROL_MIN_SPACING_M,
            # 이미 한 번 걸렸던 자리에 순찰 지점을 두면 갈 때마다 또 걸립니다.
            blind_obstacles=self.get_blind_obstacles(),
        )

    def send_patrol_command(self, command: str, plan=None, request_id=None):
        """/patrol_control 로 순찰 제어 명령을 보내고 request_id를 반환한다."""
        request_id = str(request_id or uuid.uuid4().hex)
        payload = {
            "command": command,
            "request_id": request_id,
            "reason": "minimap",
        }
        if plan is not None:
            payload["waypoints"] = [w.as_dict() for w in plan.waypoints]
            payload["loop"] = True
            payload["source"] = "dashboard"
        msg = String()
        msg.data = json.dumps(payload)
        if not self.prepare_navigation_request(request_id):
            raise ValueError("같은 request_id의 명령이 이미 처리 중입니다.")
        try:
            self.patrol_control_pub.publish(msg)
        except Exception as exc:
            self.release_navigation_request(request_id)
            raise NavigationPublishError(
                f"/patrol_control 발행 실패: {exc}") from exc
        return request_id

    def send_goto(self, x: float, y: float, stop_patrol=True, request_id=None):
        """지도 좌표 이동도 /patrol_control 하나로 보내 목표 경쟁을 없앤다.

        stop_patrol 인자는 예전 파이썬 호출부와의 호환을 위해 남겨 두지만, goto는
        언제나 현재 순찰을 취소하고 이동한다. 실제 직렬화 payload에는 넣지 않는다.
        """
        del stop_patrol
        request_id = str(request_id or uuid.uuid4().hex)
        pose, _current_yaw, _age = self.get_pose_state()
        if pose is None:
            raise RuntimeError("로봇 위치(TF)가 없거나 낡았습니다.")
        yaw = math.atan2(y - pose[1], x - pose[0])
        payload = {
            "command": "goto",
            "x": float(x),
            "y": float(y),
            "yaw": float(yaw),
            "request_id": request_id,
            "reason": "minimap_goto",
        }
        msg = String()
        msg.data = json.dumps(payload)
        if not self.prepare_navigation_request(request_id):
            raise ValueError("같은 request_id의 명령이 이미 처리 중입니다.")
        try:
            self.patrol_control_pub.publish(msg)
        except Exception as exc:
            self.release_navigation_request(request_id)
            raise NavigationPublishError(
                f"/patrol_control 발행 실패: {exc}") from exc
        self.get_logger().info(
            f"미니맵 이동 명령: ({x:.2f}, {y:.2f}) yaw={yaw:.2f}"
            f" request_id={request_id}")
        return request_id

    def get_snapshot(self):
        """레이캐스팅에 필요한 최신 맵/위치/방향을 락 안에서 한 번에 복사해서 반환
        (탐지 목록은 트래커가 내부적으로 관리하므로 여기선 넘기지 않음)"""
        with self.lock:
            self._expire_pose_locked(time.time())
            return self.latest_map, self.robot_pose, self.robot_yaw

    def get_snapshot_with_array(self):
        """get_snapshot() + 미리 만들어 둔 지도 배열.

        탐지 UDP 수신 스레드처럼 **자주 부르는 곳**은 이걸 씁니다. 배열을 매번
        새로 만들면 리스트 변환만으로 CPU 를 먹습니다(grid_to_array 주석 참고).
        """
        with self.lock:
            self._expire_pose_locked(time.time())
            return (self.latest_map, self.robot_pose, self.robot_yaw,
                    self.map_array)

    def get_valid_detections(self):
        """min_hits를 채운, 화면에 그려도 될 만큼 신뢰할 수 있는 탐지 목록"""
        with self.lock:
            self.tracker.prune_stale_objects()
            return self.tracker.get_valid_objects()

    def current_zone(self):
        """로봇이 지금 서 있는 구역 이름. 모르면 None.

        구역 라스터는 자유 공간에만 값이 있다. 로봇이 벽에 바짝 붙어 있거나
        위치 추정이 살짝 튀면 0(구역 없음) 셀에 떨어질 수 있으므로, 주변을
        조금 넓혀 가장 많이 나온 구역을 고른다.
        """
        with self.lock:
            raster = self.zone_raster
            pose = self.robot_pose
            grid_msg = self.latest_map
            zones = list(self.zones)
        if raster is None or pose is None or grid_msg is None:
            return None

        info = grid_msg.info
        resolution = float(info.resolution)
        if resolution <= 0:
            return None
        height, width = raster.shape
        col = int((pose[0] - info.origin.position.x) / resolution)
        row = int((pose[1] - info.origin.position.y) / resolution)
        if not (0 <= row < height and 0 <= col < width):
            return None

        pad = max(1, int(round(0.30 / resolution)))
        r0, r1 = max(0, row - pad), min(height, row + pad + 1)
        c0, c1 = max(0, col - pad), min(width, col + pad + 1)
        window = raster[r0:r1, c0:c1]
        window = window[window > 0]
        if window.size == 0:
            return None
        index = int(np.bincount(window).argmax())
        for zone in zones:
            if zone["index"] == index:
                return zone["name"]
        return None

    def get_zones(self):
        with self.lock:
            return self.zone_raster, list(self.zones)

    def get_live_obstacles(self):
        """저장 지도에 없는 **현재** LiDAR 장애물만 돌려준다 (사람/지게차 등).

        화면 표시용이 아니라 raycast_to_live_obstacle() 전용이다. 벽까지 후보에
        넣으면 사람 앞을 지나 벽을 먼저 집는 일이 생기므로, "지도에 없는 것"만 본다.
        """
        with self.lock:
            if time.time() - self.live_scan_seen > LIVE_OBSTACLE_STALE_SEC:
                return []   # 스캔이 끊긴 동안의 낡은 좌표를 현재 위치로 쓰면 안 된다
            return [dict(obstacle) for obstacle in self.live_scan_obstacles]

    def render_frame(self):
        """현재 맵 + 로봇 위치 + 탐지 마커를 하나의 이미지로 그려서 반환"""
        grid_msg, pose, yaw = self.get_snapshot()
        detections = self.get_valid_detections()

        if grid_msg is None:
            blank = np.full((400, 400, 3), 30, dtype=np.uint8)
            cv2.putText(blank, "Waiting for /map...", (30, 200),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)
            return blank

        width, height = grid_msg.info.width, grid_msg.info.height
        resolution = grid_msg.info.resolution
        origin_x = grid_msg.info.origin.position.x
        origin_y = grid_msg.info.origin.position.y

        data = np.array(grid_msg.data, dtype=np.int16).reshape((height, width))
        # -1 미탐사(회색) / 0~49 자유(흰색) / 50 이상 장애물(검정).
        # 예전에는 `data == 100` 만 장애물로 봐서, SLAM이 내놓는 중간 확률값
        # (65, 80 등)이 미탐사 회색으로 잘못 그려졌다.
        img = np.full((height, width), 127, dtype=np.uint8)
        img[data >= 0] = 255
        img[data >= OCC_THRESHOLD] = 0

        # 맵 해상도 4배 확대 (화질 깨짐 방지)
        SCALE = 4
        img = cv2.resize(img, (width * SCALE, height * SCALE), interpolation=cv2.INTER_NEAREST)

        img_bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        img_bgr = cv2.flip(img_bgr, 0)

        def to_pixel(x, y):
            # 확대된 배율(SCALE)만큼 좌표도 같이 곱해줍니다.
            px = int(((x - origin_x) / resolution) * SCALE)
            py = int((height * SCALE) - (((y - origin_y) / resolution) * SCALE))
            return px, py

        if pose is not None:
            px, py = to_pixel(*pose)
            # 진행 방향 표시 — 이 표시가 없으면 로봇이 어디를 보고 있는지 알 수 없다.
            if yaw is not None:
                reach = ROBOT_MARKER_RADIUS * 4
                hx = px + int(reach * math.cos(yaw))
                hy = py - int(reach * math.sin(yaw))  # flip 했으므로 y는 부호 반전
                cv2.line(img_bgr, (px, py), (hx, hy), (0, 200, 0), 3, cv2.LINE_AA)
            cv2.circle(img_bgr, (px, py), ROBOT_MARKER_RADIUS * 2, (0, 255, 0), -1)

        for obj in detections:
            px, py = to_pixel(obj.x, obj.y)
            color, caption = detection_marker_style(obj.label)
            cv2.circle(img_bgr, (px, py), DETECTION_MARKER_RADIUS * 2, color, -1)
            # 지도 배경(흰색/회색)에서 색만으로는 묻히므로 흰 테두리를 두른다.
            cv2.circle(img_bgr, (px, py), DETECTION_MARKER_RADIUS * 2 + 2, (255, 255, 255), 1)
            cv2.putText(img_bgr, f"{caption} #{obj.id}", (px + 12, py),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        return img_bgr


app = Flask(__name__)
minimap_node: MinimapNode = None  # main()에서 초기화됨


@app.before_request
def _require_token():
    """MINIMAP_TOKEN이 설정된 경우에만 동작. 비어 있으면 아무것도 하지 않는다."""
    if not MINIMAP_TOKEN:
        return None
    given = request.headers.get("X-Minimap-Token") or request.args.get("t", "")
    if not hmac.compare_digest(str(given), MINIMAP_TOKEN):
        abort(403)
    return None


@app.after_request
def _harden(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    # frame-ancestors 는 <meta> 로는 적용되지 않으므로 반드시 헤더로 보낸다.
    resp.headers["Content-Security-Policy"] = "frame-ancestors " + MINIMAP_FRAME_ANCESTORS
    # CORS 는 켜지 않는다 (위 주석 참고). 혹시 켜져 있으면 제거한다.
    resp.headers.pop("Access-Control-Allow-Origin", None)
    return resp


@app.route('/')
def index():
    """브라우저용 미니맵 페이지. 지도는 한 번만 받고, 이후엔 좌표만 갱신한다."""
    return send_from_directory(HERE, 'minimap_web.html')


def generate_minimap_frames():
    """기존 대시보드(<img src=".../minimap_feed">)를 위한 MJPEG 스트림.
    프레임 간 대기가 없으면 JPEG 인코딩이 코어 하나를 100% 점유한다."""
    interval = 1.0 / MJPEG_FPS if MJPEG_FPS > 0 else 0.2
    while True:
        frame = minimap_node.render_frame()
        ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n'
            )
        time.sleep(interval)


@app.route('/minimap_feed')
def minimap_feed():
    return Response(
        generate_minimap_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/detections')
def detections_endpoint():
    """event_logger.py(이상민님 PC)가 주기적으로 폴링하는 JSON 엔드포인트.

    session 은 이 프로세스가 뜰 때마다 새로 만들어지는 값입니다.
    tracker id(SpatialObjectTracker.next_id)는 프로세스가 재시작하면 1부터
    다시 시작하므로, id 만으로는 예전 물체와 새 물체를 구분할 수 없습니다.
    event_logger 는 (session, id) 를 짝으로 봐야 중복 적재도 막고, 재시작 뒤
    새로 잡힌 물체를 "이미 본 id"로 오인해 버리는 일도 없습니다.
    """
    objs = minimap_node.get_valid_detections()
    return jsonify({
        "session": SESSION_ID,
        "detections": [
            {"id": o.id, "label": o.label, "x": o.x, "y": o.y,
             "hit_count": o.count, "last_seen": o.last_seen}
            for o in objs
        ]
    })


@app.route('/map_data')
def map_data():
    """occupancy grid 를 브라우저가 한 번만 받아가도록 압축해서 내보낸다.

    cells: base64(bytes), 셀 하나당 1바이트 — 0=자유 1=장애물 2=미탐사.
    version 은 셀 내용의 해시라, 지도가 실제로 바뀌었을 때만 값이 달라진다.
    클라이언트가 ?v=<현재버전> 을 보내고 값이 같으면 cells 를 생략한다.
    """
    grid_msg, _pose, _yaw = minimap_node.get_snapshot()
    if grid_msg is None:
        return jsonify({"ready": False})

    info = grid_msg.info
    w, h = int(info.width), int(info.height)
    arr = np.asarray(grid_msg.data, dtype=np.int8)
    if w <= 0 or h <= 0 or arr.size != w * h:
        return jsonify({"ready": False, "error": "grid size mismatch"})

    cells = np.where(arr < 0, 2, np.where(arr >= OCC_THRESHOLD, 1, 0)).astype(np.uint8)
    raw = cells.tobytes()
    version = "%s-%dx%d" % (hashlib.sha1(raw).hexdigest()[:16], w, h)

    payload = {
        "ready": True,
        "version": version,
        "w": w,
        "h": h,
        "res": float(info.resolution),
        "ox": float(info.origin.position.x),
        "oy": float(info.origin.position.y),
    }
    if request.args.get("v") != version:
        payload["cells"] = base64.b64encode(raw).decode("ascii")
    return jsonify(payload)


@app.route('/zones')
def zones_endpoint():
    """SLAM 지도에서 자동으로 나눈 순찰 구역.

    지도와 마찬가지로 셀 라스터를 한 번만 받아가고, version 이 같으면 생략한다.
    cells: base64, 셀당 1바이트 — 0 = 구역 없음, 1..n = 구역 번호.
    """
    grid_msg, _pose, _yaw = minimap_node.get_snapshot()
    if grid_msg is None:
        return jsonify({"ready": False})
    raster, zones = minimap_node.get_zones()
    if raster is None:
        return jsonify({"ready": False})

    info = grid_msg.info
    raw = raster.tobytes()
    version = hashlib.sha1(raw).hexdigest()[:16]
    payload = {
        "ready": True,
        "version": version,
        "w": int(info.width),
        "h": int(info.height),
        "res": float(info.resolution),
        "ox": float(info.origin.position.x),
        "oy": float(info.origin.position.y),
        "zones": zones,
    }
    if request.args.get("v") != version:
        payload["cells"] = base64.b64encode(raw).decode("ascii")
    return jsonify(payload)


@app.route('/state')
def state_endpoint():
    """로봇 위치·확정 탐지·지도 내부 장애물을 담은 가벼운 응답 (약 5Hz 폴링).

    stamp 와 last_seen 은 둘 다 이 서버의 시계 기준이다. 브라우저는 둘의 차이만
    쓰므로, 관제 PC와 이 노트북의 시계가 어긋나도 경과 시간이 틀어지지 않는다.
    """
    grid_msg, _pose, _yaw = minimap_node.get_snapshot()
    pose, yaw, pose_age = minimap_node.get_pose_state()
    objs = minimap_node.get_valid_detections()
    with minimap_node.lock:
        route = [{"x": x, "y": y} for x, y in minimap_node.patrol_route]
        route_loop = minimap_node.patrol_loop
    return jsonify({
        "ready": grid_msg is not None,
        "stamp": time.time(),
        "robot": None if pose is None else {
            "x": float(pose[0]),
            "y": float(pose[1]),
            "yaw": float(yaw) if yaw is not None else 0.0,
        },
        "pose_age_sec": pose_age,
        "detections": [
            {"id": int(o.id), "label": str(o.label),
             "x": float(o.x), "y": float(o.y),
             "hit_count": int(o.count), "last_seen": float(o.last_seen)}
            for o in objs
        ],
        "patrol_route": route,
        "patrol_loop": route_loop,
        "robot_status": minimap_node.get_robot_status(),
        "navigation": minimap_node.get_navigation_status(),
        "current_zone": minimap_node.current_zone(),
        # 관제 버튼을 띄울지 화면이 판단할 수 있게 알려줍니다. 이게 없으면
        # 버튼을 눌러 403 을 받고 나서야 "꺼져 있구나" 를 알게 됩니다.
        "control_enabled": bool(
            MINIMAP_GOTO_ENABLED and MINIMAP_TOKEN
            and minimap_node.patrol_controller_available()),
        "navigation_available": minimap_node.patrol_controller_available(),
        # LiDAR 에 안 보이는 낮은 장애물. 사람이 치우러 가야 하는 자리라
        # 화면에 표시합니다.
        "blind_obstacles": minimap_node.get_blind_obstacles(),
    })


def _command_request_id(payload):
    """클라이언트 request_id가 있으면 보존하되 로그/메모리를 해치지 않게 제한한다."""
    value = payload.get("request_id") if isinstance(payload, dict) else None
    request_id = str(value).strip() if value not in (None, "") else ""
    if not request_id:
        request_id = uuid.uuid4().hex
    return request_id[:128]


def _navigation_result_ok(result):
    phase = str(result.get("phase", "")).lower()
    command = str(result.get("command", "")).lower()
    if phase in ("accepted", "succeeded"):
        return True
    return phase == "canceled" and command in ("stop", "pause")


def _navigation_poll_complete(result):
    """GET status 폴링을 끝낼 상태. goto accepted는 실제 목표 결과까지 추적한다."""
    phase = str(result.get("phase", "")).lower()
    goal_type = str(result.get("goal_type", "")).lower()
    if phase == "accepted" and goal_type != "goto":
        return True
    return minimap_node._navigation_request_is_final(result)


def _navigation_http_response(request_id, response_payload):
    """ROS publish 뒤 실제 제어기의 접수 결과를 HTTP 의미에 맞게 변환한다."""
    # 구형 테스트 더블/롤링 배포 객체는 request별 조회 캐시가 없다. 그 경우에만
    # 예전처럼 HTTP 대기 후 예약을 풀고, 현재 노드는 최종 상태 추적까지 유지한다.
    legacy_tracking = not hasattr(minimap_node, "get_navigation_result")
    try:
        result = minimap_node.wait_for_navigation_status(
            request_id, NAVIGATION_ACK_TIMEOUT_SEC)
    finally:
        if legacy_tracking and hasattr(minimap_node, "release_navigation_request"):
            minimap_node.release_navigation_request(request_id)
    body = dict(response_payload)
    body["request_id"] = request_id

    if result is None:
        # 수신자가 있었더라도 콜백이 늦거나 제어기가 멈출 수 있다. 이를 성공으로
        # 표시하면 관제자는 로봇이 명령을 수행한다고 오인한다.
        if legacy_tracking:
            with minimap_node.lock:
                observed = minimap_node.navigation_results.get(request_id)
                observed = None if observed is None else dict(observed)
            observed_age = None
        else:
            observed, observed_age, _pending = minimap_node.get_navigation_result(
                request_id)
        if observed is not None:
            observed["age_sec"] = observed_age
        body.update({
            "ok": False,
            "pending": True,
            "status": "pending",
            "navigation": observed,
        })
        return jsonify(body), 202

    phase = str(result.get("phase", "unknown"))
    body.update({
        "navigation": result,
        "status": phase,
        "pending": False,
    })
    if _navigation_result_ok(result):
        body["ok"] = True
        return jsonify(body), 200

    message = result.get("error") or result.get("message")
    body.update({
        "ok": False,
        "error": message or f"내비게이션 명령이 {phase} 상태로 종료되었습니다.",
    })
    return jsonify(body), 409


def _navigation_unavailable_response():
    return jsonify({
        "ok": False,
        "error": "내비게이션 제어기(/patrol_control 구독자)가 연결되지 않았습니다.",
    }), 503


def _navigation_publish_error_response(exc):
    minimap_node.get_logger().error(f"내비게이션 명령 전송 실패: {exc}")
    return jsonify({
        "ok": False,
        "error": "내비게이션 제어기로 명령을 전송하지 못했습니다.",
    }), 503


@app.route('/navigation_status/<request_id>')
def navigation_status_endpoint(request_id):
    """202 요청을 request_id로 끝까지 추적하는 인증된 읽기 endpoint.

    200 = 추적 완료(성공/실패는 body.ok), 202 = 아직 처리 중,
    404 = 캐시와 진행 목록 모두에 없는 ID.
    """
    if not MINIMAP_TOKEN:
        return jsonify({
            "ok": False,
            "error": "MINIMAP_TOKEN이 설정되지 않아 명령 상태를 조회할 수 없습니다.",
        }), 403
    _require_token()

    request_id = str(request_id).strip()
    if not request_id or len(request_id) > 128:
        return jsonify({"ok": False, "error": "request_id가 올바르지 않습니다."}), 400

    result, age, pending = minimap_node.get_navigation_result(request_id)
    if result is None:
        if pending:
            return jsonify({
                "ok": False,
                "found": True,
                "pending": True,
                "complete": False,
                "request_id": request_id,
                "navigation": None,
            }), 202
        return jsonify({
            "ok": False,
            "found": False,
            "pending": False,
            "complete": False,
            "request_id": request_id,
            "error": "요청 상태가 없거나 캐시 보존 범위를 지났습니다.",
        }), 404

    result["age_sec"] = age
    if not _navigation_poll_complete(result):
        return jsonify({
            "ok": False,
            "found": True,
            "pending": True,
            "complete": False,
            "request_id": request_id,
            "status": result.get("phase"),
            "navigation": result,
        }), 202

    ok = _navigation_result_ok(result)
    body = {
        "ok": ok,
        "found": True,
        "pending": False,
        "complete": True,
        "request_id": request_id,
        "status": result.get("phase"),
        "navigation": result,
    }
    if not ok:
        body["error"] = (
            result.get("error") or result.get("message")
            or "내비게이션 명령이 실패했습니다.")
    return jsonify(body), 200


@app.route('/goto', methods=['POST'])
def goto_endpoint():
    """지도에서 찍은 좌표로 로봇을 보낸다 (디지털 트윈).

    이 엔드포인트만 규칙이 다르다. 나머지는 읽기 전용이라 MINIMAP_TOKEN 을 안 걸면
    그냥 열려 있지만, 여기는 **실물 로봇을 움직인다.** 토큰이 없으면 같은 망의
    누구나 공장 로봇을 조종할 수 있게 되므로, 토큰 미설정 시 기능 자체를 끈다.
    """
    if not MINIMAP_GOTO_ENABLED:
        return jsonify({"ok": False,
                        "error": "이동 기능이 꺼져 있습니다 (MINIMAP_ALLOW_GOTO=0)."}), 403
    if not MINIMAP_TOKEN:
        return jsonify({
            "ok": False,
            "error": "MINIMAP_TOKEN 이 설정되지 않아 이동 명령을 받지 않습니다. "
                     "인증 없이 열어두면 같은 망의 누구나 로봇을 조종할 수 있습니다.",
        }), 403
    _require_token()

    payload = request.get_json(silent=True) or {}
    try:
        x = float(payload['x'])
        y = float(payload['y'])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "x, y 좌표가 필요합니다."}), 400
    if not (math.isfinite(x) and math.isfinite(y)):
        return jsonify({"ok": False, "error": "좌표 값이 올바르지 않습니다."}), 400

    if not minimap_node.patrol_controller_available():
        return _navigation_unavailable_response()

    pose, _yaw, pose_age = minimap_node.get_pose_state()
    if pose is None:
        age_text = "" if pose_age is None else f" (마지막 TF {pose_age:.1f}초 전)"
        return jsonify({
            "ok": False,
            "error": "로봇 위치(TF)가 없거나 낡아 이동할 수 없습니다." + age_text,
        }), 409

    clear, reason = minimap_node.cell_is_clear(x, y, GOTO_CLEARANCE_M)
    if not clear:
        # 여기서 막지 않으면 Nav2 가 도달 불가 목표로 계속 재시도하며,
        # 화면에는 '보냈다'고만 보여서 왜 안 가는지 알 수 없다.
        return jsonify({"ok": False, "error": reason}), 400

    # stop_patrol=false를 보내던 구형 클라이언트도 받아들이되 무시한다. goto는
    # 단일 제어기에서 현재 순찰 목표를 항상 취소한 뒤 실행한다.
    request_id = _command_request_id(payload)
    try:
        minimap_node.send_goto(
            x, y, stop_patrol=True, request_id=request_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except RuntimeError as exc:
        # 좌표 검사 뒤 큰 맵의 연결 성분을 계산하는 동안 TF가 만료될 수 있다.
        return jsonify({"ok": False, "error": str(exc)}), 409
    except NavigationPublishError as exc:
        return _navigation_publish_error_response(exc)
    except Exception as exc:
        return _navigation_publish_error_response(exc)
    return _navigation_http_response(request_id, {
        "x": x,
        "y": y,
        "patrol_paused": False,
        "patrol_stopped": True,
    })


@app.route('/patrol_plan')
def patrol_plan_endpoint():
    """지금 지도로 만든 순찰 경로를 돌려준다 (실행하지 않음, 미리보기용).

    읽기 전용이라 토큰 규칙은 나머지 조회 엔드포인트와 같습니다. 실제로 로봇을
    움직이는 것은 아래 /patrol start 이고, 그쪽은 토큰이 필수입니다.
    """
    plan = minimap_node.build_patrol_plan()
    return jsonify(plan.as_dict())


@app.route('/patrol', methods=['POST'])
def patrol_endpoint():
    """순찰 시작/정지/일시정지/재개.

    start 는 **지금 지도로 경로를 새로 만들어** 함께 보냅니다. 그래서 지도를
    다시 만든 뒤에도 patrol_node 를 재시작할 필요가 없습니다.

    이 엔드포인트는 실물 로봇을 움직입니다. 그래서 /goto 와 같은 규칙으로
    토큰이 없으면 아예 열지 않습니다.
    """
    if not MINIMAP_GOTO_ENABLED or not MINIMAP_TOKEN:
        return jsonify({"ok": False, "error": "제어 기능이 꺼져 있습니다."}), 403
    _require_token()
    payload = request.get_json(silent=True) or {}
    command = str(payload.get('command', '')).lower()

    if command not in ('start', 'stop', 'pause', 'resume'):
        return jsonify({
            "ok": False,
            "error": "command 는 start, stop, pause, resume 중 하나입니다.",
        }), 400

    if not minimap_node.patrol_controller_available():
        return _navigation_unavailable_response()

    if command == 'start':
        pose, _yaw, pose_age = minimap_node.get_pose_state()
        if pose is None:
            age_text = "" if pose_age is None else f" (마지막 TF {pose_age:.1f}초 전)"
            return jsonify({
                "ok": False,
                "error": "로봇 위치(TF)가 없거나 낡아 순찰을 시작할 수 없습니다." + age_text,
            }), 409
        plan = minimap_node.build_patrol_plan()
        if not plan.ok:
            return jsonify({"ok": False, "error": plan.reason}), 409
        request_id = _command_request_id(payload)
        try:
            minimap_node.send_patrol_command(
                'start', plan, request_id=request_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except Exception as exc:
            return _navigation_publish_error_response(exc)
        minimap_node.get_logger().info(
            f"순찰 시작: 지점 {len(plan.waypoints)}개 / "
            f"한 바퀴 {plan.total_distance_m:.1f}m")
        return _navigation_http_response(request_id, {
            "command": command,
            "plan": plan.as_dict(),
        })

    request_id = _command_request_id(payload)
    try:
        minimap_node.send_patrol_command(command, request_id=request_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return _navigation_publish_error_response(exc)
    minimap_node.get_logger().info(f"순찰 제어: {command}")
    return _navigation_http_response(request_id, {"command": command})


@app.route('/current_zone')
def current_zone_endpoint():
    """로봇이 지금 있는 구역만 돌려주는 가벼운 응답.

    데이터 파이프라인(jetson_patrol_pipeline.py)이 --zone auto 로 이걸 폴링한다.
    /state 는 지도 장애물·탐지 목록까지 실어 무거우므로 따로 둔다.
    """
    zone = minimap_node.current_zone()
    return jsonify({"zone": zone, "stamp": time.time()})


@app.route('/health')
def health():
    grid_msg, pose, yaw = minimap_node.get_snapshot()
    return jsonify({
        "map_ready": grid_msg is not None,
        "pose_ready": pose is not None,
        "camera_yaw_offset_rad": CAMERA_YAW_OFFSET,
        "camera_yaw_offset_deg": round(math.degrees(CAMERA_YAW_OFFSET), 3),
        "camera_bearing_convention": "image-right-positive_to_ros-left-positive",
    })


def parse_detection_packet(raw):
    """젯슨이 보낸 UDP 한 건 -> (label, angle_offset). 못 읽으면 None.

    소켓 없이 검증할 수 있도록 순수 함수로 떼어 놓았습니다. 수신 루프 안에
    두면 "이 패킷에서 죽는다"를 테스트로 잡을 방법이 없습니다.

    **예외를 좁게 잡으면 안 됩니다.** 예전에는 (KeyError, ValueError,
    JSONDecodeError) 만 잡았는데, dict 가 아닌 JSON(`5`, `[1,2]`)이나
    angle_offset 이 null 인 패킷은 TypeError 로 터집니다. 그 한 방에 수신
    스레드가 죽고, 지도와 로봇 위치는 다른 스레드가 계속 그리므로
    **화면은 멀쩡한데 탐지 마커만 영영 안 생겼습니다.**
    """
    try:
        msg = json.loads(raw.decode('utf-8'))
        label = str(msg["label"])
        angle_offset = float(msg["angle_offset"])
    except Exception:
        return None
    if not label or not math.isfinite(angle_offset):
        # 라벨이 비었거나 각도가 NaN/inf 면 레이캐스팅이 엉뚱한 곳을 짚습니다.
        return None
    return label, angle_offset


def camera_bearing_to_map(robot_yaw, angle_offset, camera_yaw_offset=0.0):
    """카메라 화면 방향각을 ROS map 절대각으로 바꾼다.

    영상 픽셀은 오른쪽으로 갈수록 angle_offset이 양수이다. 그런데 ROS REP-103의
    yaw는 반시계(로봇의 왼쪽)가 양수이므로 화면 각도는 **빼야** 한다.
    장착 방향 차이는 camera_yaw_offset으로 따로 보정한다.
    """
    return float(robot_yaw) + float(camera_yaw_offset) - float(angle_offset)


def detection_listener_thread():

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', DETECTION_UDP_PORT))
    print(f"탐지 각도 UDP 수신 대기: 0.0.0.0:{DETECTION_UDP_PORT}")

    dropped = 0
    last_drop_log = 0.0

    while True:
        try:
            raw, _addr = sock.recvfrom(4096)
        except OSError as exc:
            print(f"⚠ [탐지 수신] UDP 수신 실패: {exc}")
            time.sleep(0.1)
            continue

        parsed = parse_detection_packet(raw)
        if parsed is None:
            dropped += 1
            now = time.time()
            if now - last_drop_log > 10.0:
                print(f"⚠ [탐지 수신] 형식이 맞지 않는 패킷 {dropped}건 폐기. "
                      f"포트 {DETECTION_UDP_PORT} 로 다른 프로그램이 보내고 있는지 확인하세요.")
                dropped = 0
                last_drop_log = now
            continue

        label, angle_offset = parsed

        grid_msg, pose, yaw, map_array = minimap_node.get_snapshot_with_array()
        if grid_msg is None or pose is None or yaw is None:
            continue  # 아직 맵/위치 정보가 준비 안 됨

        absolute_angle = camera_bearing_to_map(
            yaw, angle_offset, CAMERA_YAW_OFFSET)

        hit = raycast_to_live_obstacle(
            minimap_node.get_live_obstacles(), pose, absolute_angle,
            DETECTION_MAX_RANGE)
        
        if hit is None:
            hit = raycast_to_obstacle(grid_msg, pose, absolute_angle,
                                      DETECTION_MAX_RANGE, data=map_array)

        if hit is not None:
            minimap_node.add_detection(label, hit[0], hit[1])


def ros_spin_thread():
    try:
        rclpy.spin(minimap_node)
    except (KeyboardInterrupt, ExternalShutdownException, RCLError):
        # Ctrl+C에서 메인 스레드와 rclpy signal handler가 동시에 종료될 수
        # 있다. 이미 내려간 context는 정상 종료이므로 traceback을 남기지 않는다.
        pass


def main():
    global minimap_node
    rclpy.init()
    minimap_node = MinimapNode()

    spin_thread = threading.Thread(target=ros_spin_thread, daemon=True)
    spin_thread.start()
    threading.Thread(target=detection_listener_thread, daemon=True).start()

    shown = MINIMAP_BIND if MINIMAP_BIND != "0.0.0.0" else "<이 PC의 IP>"
    suffix = f"?t={MINIMAP_TOKEN}" if MINIMAP_TOKEN else ""
    print(f"미니맵 페이지     : http://{shown}:{STREAM_PORT}/{suffix}")
    print(f"MJPEG(기존 대시보드): http://{shown}:{STREAM_PORT}/minimap_feed{suffix}")

    if MINIMAP_BIND == "0.0.0.0" and not MINIMAP_TOKEN:
        print()
        print("  [주의] 모든 네트워크 인터페이스에 인증 없이 열려 있습니다.")
        print("         이 서버는 공장 도면, 로봇 위치, 작업자 탐지 이력을 내보냅니다.")
        print("         운영 시에는 아래처럼 제한하세요:")
        print("           export MINIMAP_BIND=203.0.113.20")
        print("           export MINIMAP_TOKEN=$(python3 -c \"import secrets;print(secrets.token_urlsafe(16))\")")
        print()

    try:
        app.run(host=MINIMAP_BIND, port=STREAM_PORT, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        spin_thread.join(timeout=2.0)
        minimap_node.destroy_node()


if __name__ == '__main__':
    main()
