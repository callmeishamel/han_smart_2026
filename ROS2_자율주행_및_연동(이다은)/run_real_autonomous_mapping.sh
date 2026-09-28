#!/usr/bin/env bash
# 실물 TurtleBot3의 첫 지도 작성을 위한 감독형 자율 매핑 실행기.
# Jetson의 robot.launch.py가 먼저 실행되어 있어야 한다.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROS_SETUP="/opt/ros/${ROS_DISTRO:-humble}/setup.bash"
SFP_ROS_WS="${SFP_ROS_WS:-}"
MAP_OUTPUT="${MAP_OUTPUT:-$HOME/factory_map_auto}"
AUTO_MAX_RUNTIME_SEC="${AUTO_MAX_RUNTIME_SEC:-0}"
POSE_CAPTURE_LEAD_SEC="${POSE_CAPTURE_LEAD_SEC:-10}"
POSE_FILE="${POSE_FILE:-${MAP_OUTPUT}.pose}"
# LDS-03/실물 Burger에서 검증할 공격형 기본값. 속도를 높인 만큼 정지·감속
# 거리를 함께 늘려, 빠르게 훑되 장애물 안전 여유는 줄이지 않는다.
AUTO_LINEAR_SPEED="${AUTO_LINEAR_SPEED:-0.12}"
AUTO_TURN_SPEED="${AUTO_TURN_SPEED:-0.40}"
AUTO_SAFE_DISTANCE="${AUTO_SAFE_DISTANCE:-0.35}"
AUTO_SLOW_DISTANCE="${AUTO_SLOW_DISTANCE:-0.60}"
AUTO_DIAGNOSTIC_STATUS="${AUTO_DIAGNOSTIC_STATUS:-false}"
# 이 거리 밖의 유한 LiDAR 반환점은 Cartographer에 전달하지 않는다.
# 먼 오측정이 기존 벽을 지우거나 더 먼 곳에 새 벽을 만드는 것을 막는다.
MAPPING_TRUST_RANGE_M="${MAPPING_TRUST_RANGE_M:-5.0}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-30}"
# 일반 종료에서는 정지 명령만 보내고 토크는 하드웨어 bringup이 계속 관리한다.
# 로봇을 완전히 종료할 때만 1로 지정해 토크까지 해제한다.
DISABLE_MOTOR_ON_EXIT="${DISABLE_MOTOR_ON_EXIT:-0}"

# 탐색 성향. 로봇이 갔던 곳을 왕복하면 이 두 개를 먼저 만져 보세요.
#   AUTO_VISIT_WEIGHT   이미 지난 방향에 주는 벌점. 크게 할수록 새 구역을 고집합니다.
#                       0 이면 방문 기억을 끄고 예전(반응형)처럼 돕니다.
#   AUTO_REVISIT_LIMIT  같은 칸에 이 횟수만큼 들어오면 방향을 다시 잡습니다.
#                       작게 할수록 빨리 떠나지만, 너무 작으면 좁은 통로를
#                       끝까지 훑기 전에 나가버립니다. 0 이면 끕니다.
AUTO_VISIT_WEIGHT="${AUTO_VISIT_WEIGHT:-3.5}"
AUTO_REVISIT_LIMIT="${AUTO_REVISIT_LIMIT:-2}"
BLIND_OBSTACLE_STORE="${BLIND_OBSTACLE_STORE:-${MAP_OUTPUT}.blind_obstacles.json}"

if [[ -n "$SFP_ROS_WS" ]]; then
  ROS_WS="$SFP_ROS_WS"
  WORKSPACE_SETUP="$ROS_WS/install/setup.bash"
elif [[ -f "$REPO_ROOT/.runtime/ros-install/setup.bash" ]]; then
  # 한글 install prefix에서 Nav2/YAML 경로가 깨지는 시스템을 피한다.
  ROS_WS="$REPO_ROOT/.runtime"
  WORKSPACE_SETUP="$REPO_ROOT/.runtime/ros-install/setup.bash"
elif [[ -f "$SCRIPT_DIR/../install/setup.bash" ]]; then
  # 이 패키지가 <workspace>/ROS2_자율주행_및_연동(이다은)에 있는 저장소 구조.
  ROS_WS="$(cd "$SCRIPT_DIR/.." && pwd)"
  WORKSPACE_SETUP="$ROS_WS/install/setup.bash"
elif [[ -f "$SCRIPT_DIR/install/setup.bash" ]]; then
  ROS_WS="$SCRIPT_DIR"
  WORKSPACE_SETUP="$ROS_WS/install/setup.bash"
elif [[ -f "$HOME/colcon_ws/install/setup.bash" ]]; then
  ROS_WS="$HOME/colcon_ws"
  WORKSPACE_SETUP="$ROS_WS/install/setup.bash"
else
  echo "ROS 2 워크스페이스를 찾지 못했습니다." >&2
  echo "SFP_ROS_WS=~/colcon_ws 로 지정하거나 colcon build를 먼저 실행하세요." >&2
  exit 1
fi

[[ -f "$ROS_SETUP" ]] || { echo "ROS 환경을 찾지 못했습니다: $ROS_SETUP" >&2; exit 1; }
[[ -f "$WORKSPACE_SETUP" ]] || { echo "워크스페이스 install/setup.bash가 없습니다: $WORKSPACE_SETUP" >&2; exit 1; }
# __pycache__/*.pyc, README 같은 실행과 무관한 파일까지 비교하면 테스트/문서 열람만
# 해도 "소스가 설치본보다 최신"이라는 거짓 오류가 난다. 실제 설치 대상만 본다.
if find "$SCRIPT_DIR/smart_factory_sim" "$SCRIPT_DIR/launch" "$SCRIPT_DIR/config" \
    "$SCRIPT_DIR/setup.py" "$SCRIPT_DIR/package.xml" -type f \
    \( -name '*.py' -o -name '*.lua' -o -name '*.yaml' -o -name '*.yml' \
       -o -name 'setup.py' -o -name 'package.xml' \) \
    -newer "$WORKSPACE_SETUP" -print -quit | grep -q .; then
  echo "소스가 설치본보다 최신입니다. $ROS_WS 에서 colcon build --symlink-install 후 다시 실행하세요." >&2
  exit 1
fi
[[ "$AUTO_MAX_RUNTIME_SEC" =~ ^[0-9]+$ ]] || {
  echo "AUTO_MAX_RUNTIME_SEC는 0 이상의 정수여야 합니다." >&2; exit 1;
}
[[ "$POSE_CAPTURE_LEAD_SEC" =~ ^[0-9]+$ ]] || {
  echo "POSE_CAPTURE_LEAD_SEC는 0 이상의 정수여야 합니다." >&2; exit 1;
}
is_positive_number() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk "BEGIN { exit !($1 > 0) }"
}
is_nonnegative_number() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}
for _name in AUTO_LINEAR_SPEED AUTO_TURN_SPEED AUTO_SAFE_DISTANCE AUTO_SLOW_DISTANCE; do
  _value="${!_name}"
  is_positive_number "$_value" || {
    echo "$_name 는 0보다 큰 숫자여야 합니다: $_value" >&2; exit 1;
  }
done
is_positive_number "$MAPPING_TRUST_RANGE_M" || {
  echo "MAPPING_TRUST_RANGE_M는 0보다 큰 숫자여야 합니다: $MAPPING_TRUST_RANGE_M" >&2; exit 1;
}
awk "BEGIN { exit !($AUTO_SLOW_DISTANCE >= $AUTO_SAFE_DISTANCE) }" || {
  echo "AUTO_SLOW_DISTANCE는 AUTO_SAFE_DISTANCE 이상이어야 합니다." >&2; exit 1;
}
is_nonnegative_number "$AUTO_VISIT_WEIGHT" || {
  echo "AUTO_VISIT_WEIGHT는 0 이상의 숫자여야 합니다." >&2; exit 1;
}
[[ "$AUTO_REVISIT_LIMIT" =~ ^[0-9]+$ ]] || {
  echo "AUTO_REVISIT_LIMIT는 0 이상의 정수여야 합니다." >&2; exit 1;
}
[[ "$AUTO_DIAGNOSTIC_STATUS" == "true" || "$AUTO_DIAGNOSTIC_STATUS" == "false" ]] || {
  echo "AUTO_DIAGNOSTIC_STATUS는 true 또는 false여야 합니다." >&2; exit 1;
}

AUTO_DRIVE_RUNTIME_SEC="$AUTO_MAX_RUNTIME_SEC"
if (( AUTO_MAX_RUNTIME_SEC > 0 )); then
  if (( AUTO_MAX_RUNTIME_SEC <= POSE_CAPTURE_LEAD_SEC )); then
    echo "AUTO_MAX_RUNTIME_SEC는 좌표 안정화 시간(${POSE_CAPTURE_LEAD_SEC}초)보다 커야 합니다." >&2
    exit 1
  fi
  AUTO_DRIVE_RUNTIME_SEC=$((AUTO_MAX_RUNTIME_SEC - POSE_CAPTURE_LEAD_SEC))
fi

export ROS_DOMAIN_ID
export ROS_LOCALHOST_ONLY=0
source "$ROS_SETUP"
source "$WORKSPACE_SETUP"

cartographer_pid=""
mapper_pid=""
stall_pid=""
motion_stopped=0
cleanup_running=0
mapping_started=0
map_saved=0

enable_motor_or_die() {
  local response sensor_state
  echo "모터 토크를 활성화하고 응답을 검증합니다..."
  if ! response="$(timeout 8 ros2 service call /motor_power \
      std_srvs/srv/SetBool "{data: true}" 2>&1)"; then
    echo "$response" >&2
    echo "오류: /motor_power true 호출에 실패했습니다." >&2
    return 1
  fi
  echo "$response"
  if ! grep -Eq 'success[=:][[:space:]]*(true|True)' <<<"$response"; then
    echo "오류: /motor_power 응답이 success=true가 아닙니다." >&2
    return 1
  fi

  if ! sensor_state="$(timeout 5 ros2 topic echo /sensor_state \
      turtlebot3_msgs/msg/SensorState --once \
      --qos-reliability best_effort 2>&1)"; then
    echo "$sensor_state" >&2
    echo "오류: 토크 활성화 후 /sensor_state를 확인하지 못했습니다." >&2
    return 1
  fi
  if ! grep -Eq '^[[:space:]]*torque:[[:space:]]*true' <<<"$sensor_state"; then
    echo "$sensor_state" >&2
    echo "오류: 서비스는 성공했지만 실제 모터 torque가 true가 아닙니다." >&2
    return 1
  fi
  echo "모터 토크 확인 완료: torque=true"
}

wait_for_exit() {
  local pid="$1"
  local timeout_sec="$2"
  local attempts=$(( timeout_sec * 5 ))
  local _attempt
  for ((_attempt = 0; _attempt < attempts; _attempt++)); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  return 1
}

stop_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
  if ! wait_for_exit "$pid" 2; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

stop_motion() {
  if (( motion_stopped )); then
    return
  fi

  motion_stopped=1
  echo "[종료 1/3] 자율주행 정지..."

  # 먼저 매퍼에 SIGTERM을 보내면 이미 연결된 DDS publisher로 정지 명령을
  # 발행한다. ros2 CLI discovery를 기다리는 것보다 빠르다.
  if [[ -n "$mapper_pid" ]]; then
    stop_pid "$mapper_pid"
    mapper_pid=""
  fi

  # 매퍼가 이미 끝났거나 마지막 DDS 패킷을 놓친 경우를 위한 이중 안전장치.
  timeout 2 ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist \
    "{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}" \
    >/dev/null 2>&1 || true
  if [[ "$DISABLE_MOTOR_ON_EXIT" == "1" ]]; then
    timeout 2 ros2 service call /motor_power std_srvs/srv/SetBool "{data: false}" \
      >/dev/null 2>&1 || true
    echo "           요청에 따라 모터 토크를 해제했습니다."
  else
    echo "           모터 토크는 유지합니다. 완전 종료는 DISABLE_MOTOR_ON_EXIT=1을 사용하세요."
  fi

  if [[ -n "$stall_pid" ]]; then
    stop_pid "$stall_pid"
    stall_pid=""
  fi
  echo "           로봇 정지 완료."
}

force_cleanup() {
  trap - EXIT INT TERM
  set +e
  echo
  echo "두 번째 종료 요청: 지도 저장을 중단하고 즉시 종료합니다."
  # 강제 종료에서도 로봇 정지는 생략하지 않는다. 매퍼의 SIGTERM handler와
  # 모터 토크 해제를 짧은 제한시간 안에서 시도한 뒤 프로세스를 정리한다.
  [[ -n "$mapper_pid" ]] && kill -TERM "$mapper_pid" 2>/dev/null || true
  timeout 1 ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist \
    "{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}" \
    >/dev/null 2>&1 || true
  timeout 1 ros2 service call /motor_power std_srvs/srv/SetBool "{data: false}" \
    >/dev/null 2>&1 || true
  [[ -n "$mapper_pid" ]] && kill -KILL "$mapper_pid" 2>/dev/null || true
  [[ -n "$stall_pid" ]] && kill -KILL "$stall_pid" 2>/dev/null || true
  if [[ -n "$cartographer_pid" ]]; then
    kill -KILL -- "-$cartographer_pid" 2>/dev/null || true
  fi
  exit 130
}

cleanup() {
  local status=$?
  if (( cleanup_running )); then
    return
  fi
  cleanup_running=1
  # 저장 중 다시 Ctrl+C를 누르면 즉시 빠져나갈 수 있게 한다.
  trap force_cleanup INT TERM
  trap - EXIT
  set +e

  echo
  echo "종료 요청을 받았습니다. 로봇을 먼저 멈춘 뒤 지도를 저장합니다."
  echo "저장을 건너뛰고 즉시 끝내려면 Ctrl+C를 한 번 더 누르세요."
  stop_motion

  # Cartographer가 살아 있는 동안 /map을 받아 저장해야 한다.
  if [[ -n "$cartographer_pid" ]] && kill -0 "$cartographer_pid" 2>/dev/null; then
    echo "[종료 2/3] 현재 위치와 지도 저장..."
    pose_pending="${POSE_FILE}.pending.$$"
    pose_ready=0
    timeout 6 ros2 run smart_factory_sim capture_map_pose \
      "$pose_pending" --timeout 5.0 && pose_ready=1

    if timeout 8 ros2 run nav2_map_server map_saver_cli \
        -f "$MAP_OUTPUT" --free 0.196; then
      map_saved=1
      if (( pose_ready )); then
        mv -f "$pose_pending" "$POSE_FILE"
        # 대시보드는 pose가 지도보다 최신일 때만 자동 사용한다.
        touch "$POSE_FILE"
      else
        echo "최종 좌표 저장에 실패했습니다. 대시보드에서 2D Pose Estimate를 사용하세요." >&2
      fi
    else
      echo "지도 저장에 실패해 최종 좌표도 사용하지 않습니다." >&2
    fi
    rm -f "$pose_pending"
    # 별도 프로세스 그룹으로 띄웠으므로 launch와 RViz/Cartographer 자식들을
    # 한 번에 종료한다. 최종 최적화가 멈춰도 무한히 기다리지 않는다.
    echo "[종료 3/3] Cartographer와 RViz 종료..."
    kill -INT -- "-$cartographer_pid" 2>/dev/null || true
    if ! wait_for_exit "$cartographer_pid" 6; then
      echo "           정상 종료 제한시간 초과 — 강제 종료합니다."
      kill -TERM -- "-$cartographer_pid" 2>/dev/null || true
      if ! wait_for_exit "$cartographer_pid" 2; then
        kill -KILL -- "-$cartographer_pid" 2>/dev/null || true
      fi
    fi
    wait "$cartographer_pid" 2>/dev/null || true
    cartographer_pid=""
  fi
  if (( map_saved )); then
    echo "저장 위치: ${MAP_OUTPUT}.yaml / ${MAP_OUTPUT}.pgm"
    [[ -f "$POSE_FILE" ]] && echo "최종 좌표: $POSE_FILE"
  elif (( mapping_started )); then
    echo "경고: 이번 실행의 지도는 저장되지 않았습니다." >&2
  else
    echo "매핑 시작 전에 종료되었으며 기존 지도는 변경하지 않았습니다."
  fi
  echo "자율매핑 종료 완료."
  trap - INT TERM
  exit "$status"
}

request_cleanup() {
  echo
  echo "Ctrl+C 감지: 종료 절차를 시작합니다."
  exit 130
}

trap cleanup EXIT
trap request_cleanup INT TERM

echo "=================================================="
echo " 실물 TurtleBot3 감독형 자율 매핑 시작"
echo " 지도 저장 위치: ${MAP_OUTPUT}.yaml"
echo " 최종 좌표 파일: $POSE_FILE"
echo " ROS 도메인:     $ROS_DOMAIN_ID"
echo " ROS 워크스페이스: $ROS_WS"
echo " 전진 속도:      ${AUTO_LINEAR_SPEED}m/s"
echo " 회전 속도:      ${AUTO_TURN_SPEED}rad/s"
echo " 정지 거리:      ${AUTO_SAFE_DISTANCE}m"
echo " 매핑 신뢰거리: ${MAPPING_TRUST_RANGE_M}m"
echo " 방문 벌점:      ${AUTO_VISIT_WEIGHT} (0=끔)"
echo " 재방문 한계:    ${AUTO_REVISIT_LIMIT}회 (0=끔)"
echo " 장애물 기록:    ${BLIND_OBSTACLE_STORE}"
if (( AUTO_MAX_RUNTIME_SEC > 0 )); then
  echo " 자율 주행:      ${AUTO_DRIVE_RUNTIME_SEC}초"
  echo " 좌표 안정화:    ${POSE_CAPTURE_LEAD_SEC}초"
fi
echo "=================================================="
echo "주의: Nav2/대시보드 스택은 먼저 종료하고, 사람은 로봇 옆에서 감시하세요."
echo "중지: 이 터미널에서 Ctrl+C (정지 후 지도 저장)"
echo "강제 중지: 저장 중 Ctrl+C 한 번 더"
echo

# 이전 비정상 종료 프로세스가 남아 두 개의 map/TF를 발행하면 지도가
# 즉시 펼쳐져 보인다. 새 실행이 자동으로 덮어쓰지 않고 명확한 오류를 낸다.
existing_mapping_nodes="$(
  ros2 node list 2>/dev/null | \
    grep -E '^/(auto_mapper|stall_monitor|cartographer_node|cartographer_occupancy_grid_node|scan_qos_relay|odom_filter_relay|rviz2)$' \
    || true
)"
if [[ -n "$existing_mapping_nodes" ]]; then
  echo "오류: 이전 매핑 노드가 아직 실행 중입니다:" >&2
  echo "$existing_mapping_nodes" >&2
  echo "pgrep -af 'rviz2|cartographer|auto_mapper|scan_qos_relay|odom_filter_relay'로 정리 후 다시 실행하세요." >&2
  exit 1
fi

cmd_vel_publishers="$(ros2 topic info /cmd_vel 2>/dev/null | \
  awk '/Publisher count:/ {print $3; exit}' || true)"
if [[ -n "$cmd_vel_publishers" && "$cmd_vel_publishers" != "0" ]]; then
  echo "오류: 자율매핑 시작 전에 /cmd_vel publisher가 ${cmd_vel_publishers}개 있습니다." >&2
  ros2 topic info /cmd_vel -v >&2 || true
  echo "Nav2, teleop 또는 이전 주행 노드를 종료한 뒤 다시 실행하세요." >&2
  exit 1
fi

for _required_pkg in cartographer_ros turtlebot3_cartographer nav2_map_server; do
  if ! ros2 pkg prefix "$_required_pkg" >/dev/null 2>&1; then
    echo "오류: 실물 매핑에 필요한 ROS 패키지가 없습니다: $_required_pkg" >&2
    exit 1
  fi
done

# robot.launch.py의 turtlebot3_node가 살아 있어야만 실제 바퀴가 움직인다.
# Jetson 기동 직후에는 DDS 발견이 몇 초 느릴 수 있으므로 즉시 실패하지
# 않고 최대 20초 기다린다.
motor_service_ready=0
echo "Jetson /motor_power 서비스를 확인합니다(최대 20초)..."
for _motor_wait_attempt in $(seq 1 20); do
  if ros2 service list 2>/dev/null | grep -qx '/motor_power'; then
    motor_service_ready=1
    break
  fi
  sleep 1
done
if (( ! motor_service_ready )); then
  echo "오류: /motor_power 서비스가 20초 동안 발견되지 않았습니다." >&2
  echo "Jetson의 robot.launch.py와 ROS_DOMAIN_ID=30을 확인하세요." >&2
  exit 1
fi

# 서비스가 보여도 turtlebot3_node의 속도 구독이 빠져 있으면 auto_mapper가
# /cmd_vel을 정상 발행해도 로봇은 움직이지 않는다. 실제 하드웨어 구독자를
# 매핑 시작 전에 확인해 "명령 발행 성공"을 "구동 성공"으로 오인하지 않는다.
cmd_vel_subscribers="$(ros2 topic info /cmd_vel 2>/dev/null | \
  awk '/^(Subscription|Subscriber) count:/ {print $3; exit}' || true)"
if [[ -z "$cmd_vel_subscribers" || "$cmd_vel_subscribers" == "0" ]]; then
  echo "오류: /cmd_vel을 받는 구독자가 없습니다." >&2
  ros2 topic info /cmd_vel -v >&2 || true
  echo "Jetson의 turtlebot3_node가 실행 중인지와 ROS_DOMAIN_ID=30을 확인하세요." >&2
  exit 1
fi
echo "/cmd_vel 하드웨어 구독자 확인 완료: ${cmd_vel_subscribers}개"

if ! timeout 5 ros2 topic echo /scan sensor_msgs/msg/LaserScan \
    --once --qos-reliability best_effort >/dev/null 2>&1; then
  echo "오류: /scan 라이다 데이터가 들어오지 않습니다. Jetson의 lidar_node를 확인하세요." >&2
  exit 1
fi

if ! timeout 5 ros2 topic echo /odom nav_msgs/msg/Odometry --once \
    --qos-reliability best_effort \
    >/dev/null 2>&1; then
  echo "오류: /odom 바퀴 오도메트리가 들어오지 않습니다. Jetson의 turtlebot3_node를 확인하세요." >&2
  exit 1
fi

enable_motor_or_die || exit 1

# 별도 프로세스 그룹으로 시작해야 종료 시 launch의 모든 자식도 함께 정리된다.
setsid ros2 launch smart_factory_sim real_cartographer.launch.py \
  mapping_trust_range_m:="$MAPPING_TRUST_RANGE_M" &
cartographer_pid=$!
mapping_started=1

sleep 8
kill -0 "$cartographer_pid" 2>/dev/null || {
  echo "Cartographer가 시작 직후 종료했습니다." >&2
  exit 1
}

# LiDAR 높이 아래의 문턱·케이블처럼 바퀴를 막지만 스캔에는 안 잡히는 장애물을
# 주행 결과로 감지한다. /cmd_vel은 구독만 하므로 auto_mapper와 충돌하지 않는다.
ros2 run smart_factory_sim stall_monitor --ros-args \
  -p use_sim_time:=false \
  -p store_path:="$BLIND_OBSTACLE_STORE" &
stall_pid=$!

sleep 1
if ! kill -0 "$stall_pid" 2>/dev/null; then
  wait "$stall_pid" || true
  stall_pid=""
  echo "오류: 낮은 장애물 감시 노드가 시작 직후 종료했습니다." >&2
  exit 1
fi

# launch와 주행 노드 모두 같은 워크스페이스 설치본을 사용한다.
ros2 run smart_factory_sim auto_mapper --ros-args \
  -p use_sim_time:=false \
  -p linear_speed:="$AUTO_LINEAR_SPEED" \
  -p turn_speed:="$AUTO_TURN_SPEED" \
  -p safe_distance:="$AUTO_SAFE_DISTANCE" \
  -p slow_distance:="$AUTO_SLOW_DISTANCE" \
  -p diagnostic_status:="$AUTO_DIAGNOSTIC_STATUS" \
  -p visit_weight:="$AUTO_VISIT_WEIGHT" \
  -p revisit_threshold:="$AUTO_REVISIT_LIMIT" \
  -p max_runtime_sec:="$AUTO_DRIVE_RUNTIME_SEC" &
mapper_pid=$!

sleep 2
if ! kill -0 "$mapper_pid" 2>/dev/null; then
  wait "$mapper_pid" || true
  echo "오류: 자율 매핑 노드가 시작 직후 종료했습니다. 위 Python 오류를 확인하세요." >&2
  exit 1
fi
if ! timeout 5 ros2 topic echo /cmd_vel geometry_msgs/msg/Twist --once \
    --filter 'abs(m.linear.x) > 0.001 or abs(m.angular.z) > 0.001' \
    >/dev/null 2>&1; then
  echo "오류: auto_mapper가 5초 안에 비정지 /cmd_vel을 발행하지 않습니다." >&2
  exit 1
fi
set +e
wait "$mapper_pid"
mapper_status=$?
set -e
mapper_pid=""

if (( mapper_status == 2 )); then
  echo "오류: 이동/회전 명령에 실제 움직임이 없어 OpenCR/DYNAMIXEL 구동계 이상으로 매핑을 중지했습니다." >&2
  exit 2
elif (( mapper_status != 0 )); then
  echo "오류: auto_mapper가 종료 코드 ${mapper_status}로 끝났습니다." >&2
  exit "$mapper_status"
fi

if (( AUTO_MAX_RUNTIME_SEC > 0 && POSE_CAPTURE_LEAD_SEC > 0 )); then
  stop_motion
  echo "주행을 멈췄습니다. 최종 좌표 안정화를 위해 ${POSE_CAPTURE_LEAD_SEC}초 기다립니다..."
  sleep "$POSE_CAPTURE_LEAD_SEC"
fi
