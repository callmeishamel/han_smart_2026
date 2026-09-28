-- 실물 TurtleBot3 LDS-03 전용 Cartographer 설정.
--
-- 일부 OpenCR/TurtleBot3 조합의 기동 직후 stamp=0 /odom은
-- odom_filter_relay.py가 제거한다. 정상 바퀴 odometry는 LiDAR scan matching을
-- 보조해 회전 중 위치 추정이 튀는 현상을 줄인다. odom TF는 로봇 브링업이
-- 발행하고 Cartographer는 map->odom만 추가한다.

include "map_builder.lua"
include "trajectory_builder.lua"

options = {
  map_builder = MAP_BUILDER,
  trajectory_builder = TRAJECTORY_BUILDER,
  map_frame = "map",
  tracking_frame = "imu_link",
  published_frame = "odom",
  odom_frame = "odom",
  provide_odom_frame = false,
  publish_frame_projected_to_2d = true,
  use_odometry = true,
  use_nav_sat = false,
  use_landmarks = false,
  num_laser_scans = 1,
  num_multi_echo_laser_scans = 0,
  num_subdivisions_per_laser_scan = 1,
  num_point_clouds = 0,
  lookup_transform_timeout_sec = 0.5,
  submap_publish_period_sec = 0.3,
  pose_publish_period_sec = 5e-3,
  trajectory_publish_period_sec = 30e-3,
  rangefinder_sampling_ratio = 1.,
  odometry_sampling_ratio = 1.,
  fixed_frame_pose_sampling_ratio = 1.,
  imu_sampling_ratio = 1.,
  landmarks_sampling_ratio = 1.,
}

MAP_BUILDER.use_trajectory_builder_2d = true

TRAJECTORY_BUILDER_2D.min_range = 0.12
-- LDS-03의 물리적 상한(12m)이다. 실제 매핑 신뢰 거리는 scan_qos_relay의
-- max_mapping_range_m(기본 5m)에서 더 보수적으로 제한한다. 임계 밖의 빔은
-- NaN으로 제거하여 새 벽(hit)과 기존 벽 삭제(miss) 모두에 쓰이지 않게 한다.
TRAJECTORY_BUILDER_2D.max_range = 12.0
TRAJECTORY_BUILDER_2D.missing_data_ray_length = 11.5
TRAJECTORY_BUILDER_2D.use_imu_data = false
TRAJECTORY_BUILDER_2D.use_online_correlative_scan_matching = true
TRAJECTORY_BUILDER_2D.motion_filter.max_angle_radians = math.rad(0.1)

-- ---------------------------------------------------------------------------
-- 움직이는 물건(의자 다리 등)이 지도에 박히는 것을 줄이려면
-- ---------------------------------------------------------------------------
-- 2D LiDAR 는 바닥에서 약 0.18m 한 높이만 보는데, 하필 그 높이에 의자·책상 다리가
-- 걸립니다. 매핑 중 서 있던 가구가 점유 셀로 박히면 나중에 치워도 지도에는 남고,
-- 그 지도를 Nav2(static layer) · AMCL · patrol_planner 가 함께 믿습니다.
--
-- 아래 값을 켜면 "비어 있다"는 관측이 셀을 더 빠르게 자유공간으로 되돌립니다
-- (miss_probability 는 0.5 아래일수록 강하게 지웁니다. 기본 0.49 는 거의
--  지우지 않는 값입니다).
--
--   TRAJECTORY_BUILDER_2D.submaps.range_data_inserter
--     .probability_grid_range_data_inserter.miss_probability = 0.47
--
-- **다만 한계가 분명합니다.** Cartographer 의 서브맵은 num_range_data(기본 90
-- 스캔)를 채우면 동결됩니다. 의자가 서브맵 N 에 박히면, 나중에 그 자리를 다시
-- 지나가며 빈 공간을 관측해도 그건 새 서브맵에 들어갈 뿐 옛 서브맵은 바뀌지
-- 않습니다. 즉 이 설정은 **서브맵 하나 안에서 생겼다 사라지는 잡음**만 줄입니다.
--
-- 확실한 해결은 두 가지입니다.
--   1. 매핑 전에 바닥을 치운다 (가장 싸고 확실합니다)
--   2. 저장된 지도를 후처리한다 — maps/clean_map.py
--
-- 기본값을 그대로 둔 이유: 실물 로봇에서 검증하지 않은 SLAM 튜닝이라, 켠 뒤에는
-- 얇은 벽이 끊기지 않는지 지도를 눈으로 확인해야 합니다. 확인할 수 있을 때
-- 위 한 줄의 주석을 풀어 시험해 보세요.

POSE_GRAPH.constraint_builder.min_score = 0.65
POSE_GRAPH.constraint_builder.global_localization_min_score = 0.7

return options
