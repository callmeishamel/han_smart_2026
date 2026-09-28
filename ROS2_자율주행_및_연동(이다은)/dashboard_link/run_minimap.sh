#!/bin/bash
# ------------------------------------------------------------
# 미니맵 렌더러 실행 스크립트 (이다은님 노트북용)
#
# 사용법:
#   1. SLAM(ROS2)이 먼저 실행되어 아래 두 가지가 준비돼 있어야 합니다.
#        - /map 토픽 (nav_msgs/OccupancyGrid)
#        - map -> base_footprint TF
#      로봇 위치는 tf2 로 직접 조회합니다. 예전에는 /amcl_pose 를 구독했지만
#      지금은 아닙니다 — 그 토픽이 없어도 TF만 있으면 동작합니다.
#   2. 이 파일을 실행하면 됩니다.
#        ./run_minimap.sh
# ------------------------------------------------------------

cd "$(dirname "$0")"

# 필요 시 아래 값들을 환경에 맞게 조정하세요 (기본값 그대로 써도 무방합니다).
# export MINIMAP_PORT=8091          # 대시보드/카메라 스트림이 접속할 포트
# export DETECTION_UDP_PORT=9091    # 젯슨의 각도 데이터를 받을 UDP 포트
# export CAMERA_YAW_OFFSET=0.0      # 카메라가 로봇 정면과 다른 방향을 보면 오차(라디안) 입력
# export MAPPED_OBSTACLE_MIN_CELLS=6          # 이보다 작은 점유 덩어리는 LiDAR 노이즈로 숨김
# export MAPPED_OBSTACLE_BOUNDARY_MARGIN_M=0.35 # 외곽 벽으로 볼 여백(m)
# export MAPPED_OBSTACLE_WALL_SPAN_RATIO=0.70 # 이 비율 이상 긴 성분은 벽으로 숨김
# export LIVE_OBSTACLE_MAP_MATCH_RADIUS_M=0.20 # 저장 지도 벽과 같은 scan 점으로 볼 반경(m)

# 접근 제한 (운영 시 권장). 이 서버는 공장 도면, 로봇 실시간 위치, 작업자
# 헬멧 미착용 이력을 인증 없이 내보냅니다.
# 토큰을 켜면 미니맵을 부르는 **양쪽 모두**(대시보드, event_logger)에 같은 값이
# 있어야 합니다. 한쪽만 넣으면 그쪽이 403 을 받고 조용히 멈춥니다.
# export MINIMAP_BIND=203.0.113.20
# export MINIMAP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")

echo "=================================================="
echo " 미니맵 렌더러를 시작합니다"
echo " 이 컴퓨터의 IP는 아래 명령으로 확인할 수 있습니다: hostname -I"
echo "=================================================="
echo ""

python3 minimap_renderer.py

echo ""
echo "렌더러가 종료되었습니다. 아무 키나 누르면 창이 닫힙니다."
read -n 1 -s
