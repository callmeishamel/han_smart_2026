#!/usr/bin/env bash
# 저장 지도 기반 실물 TurtleBot3 관제 스택 실행기.
#
# 시작하는 것
#   1) Nav2/AMCL + RViz (저장 지도)
#   2) 실물 지도 경로를 받아 Nav2로 도는 patrol_node
#   3) 낮은 장애물 기록을 복원·갱신하는 stall_monitor
#   4) ROS2 미니맵 서버 (:8091, 지도 클릭 이동 포함)
#   5) Streamlit 관제 대시보드 (:8501)
#   6) 탐지 DB 적재 + RAG 대응안 + 비동기 LLM (:9998)
#
# Jetson의 turtlebot3_bringup robot.launch.py는 이 스크립트 밖에서 먼저 실행돼
# 있어야 한다. Ctrl+C를 누르면 여기서 시작한 세 프로세스를 함께 종료한다.

# ROS2의 setup.bash는 일부 환경변수가 비어 있는 상태를 허용한다. `set -u`를
# 먼저 켜면 Humble setup.bash가 AMENT_TRACE_SETUP_FILES를 읽는 과정에서 종료된다.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="$REPO_ROOT/데이터 플랫폼 및 대시보드(이상민)"
SFP_ROS_WS="${SFP_ROS_WS:-}"
MAP_FILE="${MAP_FILE:-$HOME/factory_map.yaml}"
POSE_FILE="${POSE_FILE:-${MAP_FILE%.yaml}.pose}"
# run_real_autonomous_mapping.sh의 `${MAP_OUTPUT}.blind_obstacles.json` 규칙과
# 맞춘다. 예: factory_map_auto.yaml -> factory_map_auto.blind_obstacles.json
BLIND_OBSTACLE_STORE="${BLIND_OBSTACLE_STORE:-${MAP_FILE%.yaml}.blind_obstacles.json}"
USE_SAVED_POSE="${USE_SAVED_POSE:-1}"
ENABLE_MOTOR="${ENABLE_MOTOR:-1}"
DISABLE_MOTOR_ON_EXIT="${DISABLE_MOTOR_ON_EXIT:-0}"
ROS_SETUP="/opt/ros/${ROS_DISTRO:-humble}/setup.bash"

PC_IP="${PC_IP:-$(hostname -I | awk '{print $1}')}"
JETSON_IP="${JETSON_IP:-203.0.113.10}"
MINIMAP_PORT="${MINIMAP_PORT:-8091}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
TTS_TCP_PORT="${TTS_TCP_PORT:-9997}"
EXPECTED_LDS_MODEL="${EXPECTED_LDS_MODEL:-LDS-03}"
ENABLE_RAG="${ENABLE_RAG:-1}"
ENABLE_LLM="${ENABLE_LLM:-1}"
ENABLE_TTS="${ENABLE_TTS:-1}"
SFP_PATROL_ZONE="${SFP_PATROL_ZONE:-auto}"

die() {
  echo "오류: $*" >&2
  exit 1
}

[[ -f "$MAP_FILE" ]] || die "저장 지도 파일이 없습니다: $MAP_FILE"
[[ -f "$ROS_SETUP" ]] || die "ROS 환경을 찾지 못했습니다: $ROS_SETUP"
if [[ -n "$SFP_ROS_WS" ]]; then
  ROS_WS="$SFP_ROS_WS"
  WORKSPACE_SETUP="$ROS_WS/install/setup.bash"
elif [[ -f "$REPO_ROOT/.runtime/ros-install/setup.bash" ]]; then
  # Nav2의 C++ YAML 로더가 한글이 든 install prefix를 잘못 해석하는 환경이
  # 있다. 저장소의 ASCII 런타임 빌드가 있으면 그것을 우선 사용한다.
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
  die "ROS 2 워크스페이스를 찾지 못했습니다. SFP_ROS_WS=~/colcon_ws 로 지정하거나 colcon build를 실행하세요."
fi
[[ -f "$WORKSPACE_SETUP" ]] || die "패키지 설치본이 없습니다: $WORKSPACE_SETUP"
if find "$SCRIPT_DIR/smart_factory_sim" "$SCRIPT_DIR/launch" "$SCRIPT_DIR/config" \
    "$SCRIPT_DIR/setup.py" "$SCRIPT_DIR/package.xml" -type f \
    \( -name '*.py' -o -name '*.lua' -o -name '*.yaml' -o -name '*.yml' \
       -o -name 'setup.py' -o -name 'package.xml' \) \
    -newer "$WORKSPACE_SETUP" -print -quit | grep -q .; then
  die "소스가 설치본보다 최신입니다. $ROS_WS 에서 colcon build --symlink-install 후 다시 실행하세요."
fi
[[ -x "$DASHBOARD_DIR/.venv/bin/streamlit" ]] || die "대시보드 가상환경을 찾지 못했습니다: $DASHBOARD_DIR/.venv"
[[ -x "$DASHBOARD_DIR/run_pipeline_rag.sh" ]] || die "RAG 실행기를 찾지 못했습니다: $DASHBOARD_DIR/run_pipeline_rag.sh"
[[ -f "$DASHBOARD_DIR/.streamlit/secrets.toml" ]] || die "대시보드 DB 설정 파일이 없습니다: $DASHBOARD_DIR/.streamlit/secrets.toml"
for _toggle_name in ENABLE_RAG ENABLE_LLM ENABLE_TTS; do
  _toggle_value="${!_toggle_name}"
  [[ "$_toggle_value" == "0" || "$_toggle_value" == "1" ]] || \
    die "$_toggle_name 은 0 또는 1이어야 합니다: $_toggle_value"
done

is_number() {
  [[ "$1" =~ ^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$ ]]
}

list_ros_nodes_direct() {
  local nodes
  # 기본 ros2 node list는 daemon이 보관한 과거 그래프를 돌려줄 수 있다.
  # 재실행 안전 검사는 지금 DDS 그래프를 직접 발견한 결과를 우선 사용한다.
  if nodes="$(ros2 node list --no-daemon --spin-time 2 2>/dev/null)"; then
    printf '%s\n' "$nodes"
  else
    # --no-daemon/--spin-time을 지원하지 않는 오래된 ros2cli용 호환 경로.
    ros2 node list 2>/dev/null || true
  fi
}

# 우선순위: 사용자가 세 값을 모두 지정 > 지도 옆 최신 .pose > (0, 0, 0).
explicit_pose_count=0
[[ -n "${INITIAL_X+x}" ]] && explicit_pose_count=$((explicit_pose_count + 1))
[[ -n "${INITIAL_Y+x}" ]] && explicit_pose_count=$((explicit_pose_count + 1))
[[ -n "${INITIAL_YAW+x}" ]] && explicit_pose_count=$((explicit_pose_count + 1))

if (( explicit_pose_count > 0 && explicit_pose_count < 3 )); then
  die "INITIAL_X, INITIAL_Y, INITIAL_YAW는 세 값을 모두 지정해야 합니다."
fi

POSE_SOURCE="기본값 (0, 0, 0)"
if (( explicit_pose_count == 3 )); then
  is_number "$INITIAL_X" && is_number "$INITIAL_Y" && is_number "$INITIAL_YAW" || \
    die "INITIAL_X/Y/YAW는 숫자여야 합니다."
  POSE_SOURCE="사용자 지정 환경변수"
elif [[ "$USE_SAVED_POSE" == "1" && -f "$POSE_FILE" && "$POSE_FILE" -nt "$MAP_FILE" ]]; then
  read -r saved_x saved_y saved_yaw extra_value < "$POSE_FILE" || \
    die "최종 좌표 파일을 읽지 못했습니다: $POSE_FILE"
  [[ -z "${extra_value:-}" ]] || die "최종 좌표 파일 형식이 잘못됐습니다: $POSE_FILE"
  is_number "$saved_x" && is_number "$saved_y" && is_number "$saved_yaw" || \
    die "최종 좌표 파일에 숫자가 아닌 값이 있습니다: $POSE_FILE"
  INITIAL_X="$saved_x"
  INITIAL_Y="$saved_y"
  INITIAL_YAW="$saved_yaw"
  POSE_SOURCE="자동 인계 ($POSE_FILE)"
else
  INITIAL_X="0.0"
  INITIAL_Y="0.0"
  INITIAL_YAW="0.0"
  if [[ "$USE_SAVED_POSE" == "1" && -f "$POSE_FILE" ]]; then
    echo "경고: 좌표 파일이 지도보다 오래되어 자동 사용하지 않습니다: $POSE_FILE" >&2
  fi
fi

# 한 번 생성한 토큰을 미니맵과 Streamlit 자식 프로세스가 함께 상속한다.
export MINIMAP_TOKEN="${MINIMAP_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
export MINIMAP_ALLOW_GOTO=1
export MINIMAP_BIND="$PC_IP"
export MINIMAP_PORT
export JETSON_IP
export TTS_TCP_PORT
export DAEUN_LAPTOP_IP="$PC_IP"
export CAMERA_YAW_OFFSET="${CAMERA_YAW_OFFSET:-0.0}"
export SFP_PATROL_ZONE
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-30}"
export ROS_LOCALHOST_ONLY=0

source "$ROS_SETUP"
source "$WORKSPACE_SETUP"
set -u

enable_motor_or_die() {
  local response sensor_state
  echo "대시보드 자율주행을 위해 모터 토크를 활성화하고 검증합니다..."
  if ! response="$(timeout 8 ros2 service call /motor_power \
      std_srvs/srv/SetBool "{data: true}" 2>&1)"; then
    echo "$response" >&2
    die "/motor_power true 호출에 실패했습니다."
  fi
  echo "$response"
  grep -Eq 'success[=:][[:space:]]*(true|True)' <<<"$response" || \
    die "/motor_power 응답이 success=true가 아닙니다."

  if ! sensor_state="$(timeout 5 ros2 topic echo /sensor_state \
      turtlebot3_msgs/msg/SensorState --once \
      --qos-reliability best_effort 2>&1)"; then
    echo "$sensor_state" >&2
    die "토크 활성화 후 /sensor_state를 확인하지 못했습니다."
  fi
  if ! grep -Eq '^[[:space:]]*torque:[[:space:]]*true' <<<"$sensor_state"; then
    echo "$sensor_state" >&2
    die "서비스는 성공했지만 실제 모터 torque가 true가 아닙니다."
  fi
  echo "모터 토크 확인 완료: torque=true"
}

ros_nodes="$(list_ros_nodes_direct)"
if grep -qx '/cartographer_node' <<<"$ros_nodes"; then
  die "Cartographer가 아직 실행 중입니다. 매핑 터미널을 완전히 종료한 뒤 다시 실행하세요."
fi
duplicate_nodes="$(grep -E \
  '^/(amcl|bt_navigator|controller_server|planner_server|patrol_node|stall_monitor|minimap_renderer)$' \
  <<<"$ros_nodes" || true)"
if [[ -n "$duplicate_nodes" ]]; then
  echo "발견된 기존 관제 노드:" >&2
  sed 's/^/  - /' <<<"$duplicate_nodes" >&2
  die "이전 Nav2/순찰 관제 스택이 아직 실행 중입니다. 이전 관제 터미널을 종료하세요."
fi

if [[ "$ENABLE_MOTOR" == "1" ]]; then
  motor_service_ready=0
  echo "Jetson /motor_power 서비스를 확인합니다(최대 20초)..."
  for _motor_wait_attempt in $(seq 1 20); do
    if ros2 service list 2>/dev/null | grep -qx '/motor_power'; then
      motor_service_ready=1
      break
    fi
    sleep 1
  done
  (( motor_service_ready )) || \
    die "/motor_power 서비스가 20초 동안 없습니다. Jetson의 robot.launch.py를 확인하세요."

  if ! timeout 5 ros2 topic echo /scan sensor_msgs/msg/LaserScan --once \
      --qos-reliability best_effort >/dev/null 2>&1; then
    ros2 topic info /scan -v >&2 || true
    die "/scan이 들어오지 않습니다. Jetson의 LDS_MODEL=${EXPECTED_LDS_MODEL} 설정과 LiDAR USB를 확인하세요."
  fi
  scan_publisher="$(ros2 topic info /scan -v 2>/dev/null | \
    awk '/Node name:/ {print $3; exit}' || true)"
  echo "LiDAR /scan 수신 확인: ${scan_publisher:-publisher 이름 확인 불가}"
  enable_motor_or_die
fi

RUN_DIR="$(mktemp -d /tmp/smart-factory-real-stack.XXXXXX)"
nav_pid=""
minimap_pid=""
dashboard_pid=""
rag_pid=""

wait_for_exit() {
  local pid="$1"
  local timeout_sec="$2"
  local checks=$((timeout_sec * 10))
  local _check
  [[ -n "$pid" ]] || return 0
  for ((_check = 0; _check < checks; _check++)); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
  done
  return 1
}

wait_for_group_exit() {
  local group_id="$1"
  local timeout_sec="$2"
  local checks=$((timeout_sec * 10))
  local _check
  [[ -n "$group_id" ]] || return 0
  for ((_check = 0; _check < checks; _check++)); do
    # 그룹 리더가 먼저 끝나도 launch 자식이 남을 수 있으므로 그룹 전체를 본다.
    kill -0 -- "-$group_id" 2>/dev/null || return 0
    sleep 0.1
  done
  return 1
}

stop_pid_bounded() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  if ! wait_for_exit "$pid" 3; then
    kill -KILL "$pid" 2>/dev/null || true
    wait_for_exit "$pid" 1 || true
  fi
  # D(uninterruptible sleep) 상태는 SIGKILL로도 바로 안 내려갈 수
  # 있다. 아직 살아 있는 PID를 wait하면 종료가 또 무한 대기한다.
  kill -0 "$pid" 2>/dev/null || wait "$pid" 2>/dev/null || true
}

stop_group_bounded() {
  local leader_pid="$1"
  [[ -n "$leader_pid" ]] || return 0
  kill -INT -- "-$leader_pid" 2>/dev/null || true
  if ! wait_for_group_exit "$leader_pid" 4; then
    kill -TERM -- "-$leader_pid" 2>/dev/null || true
    if ! wait_for_group_exit "$leader_pid" 2; then
      kill -KILL -- "-$leader_pid" 2>/dev/null || true
      wait_for_group_exit "$leader_pid" 1 || true
    fi
  fi
  kill -0 "$leader_pid" 2>/dev/null || wait "$leader_pid" 2>/dev/null || true
}

force_cleanup() {
  trap - EXIT INT TERM HUP
  echo
  echo "두 번째 Ctrl+C: 관제 프로세스를 즉시 강제 종료합니다."
  [[ -n "$dashboard_pid" ]] && kill -KILL "$dashboard_pid" 2>/dev/null || true
  [[ -n "$rag_pid" ]] && kill -KILL "$rag_pid" 2>/dev/null || true
  [[ -n "$minimap_pid" ]] && kill -KILL "$minimap_pid" 2>/dev/null || true
  [[ -n "$nav_pid" ]] && kill -KILL -- "-$nav_pid" 2>/dev/null || true
  [[ "$ENABLE_MOTOR" == "1" ]] && \
    timeout 1 ros2 service call /motor_power std_srvs/srv/SetBool "{data: false}" \
      >/dev/null 2>&1 || true
  exit 130
}

cleanup() {
  local status=$?
  trap - EXIT
  trap force_cleanup INT TERM HUP
  echo
  echo "관제 스택을 종료합니다..."
  # Nav2를 내리기 전에 먼저 정지 명령을 보낸다. 일반 종료에서는 토크를
  # 유지해 코드 재시작 사이에 꺼진 상태가 남지 않게 한다.
  # 네트워크/DDS가 끊겨도 종료가 무한 대기하지 않게 timeout을 둔다.
  for _stop_attempt in 1 2 3; do
    timeout 1 ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist \
      "{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}" \
      >/dev/null 2>&1 || true
  done
  [[ "$ENABLE_MOTOR" == "1" && "$DISABLE_MOTOR_ON_EXIT" == "1" ]] && \
    timeout 2 ros2 service call /motor_power std_srvs/srv/SetBool "{data: false}" \
      >/dev/null 2>&1 || true
  stop_pid_bounded "$dashboard_pid"
  stop_pid_bounded "$rag_pid"
  stop_pid_bounded "$minimap_pid"
  # Nav2 launch는 독립 프로세스 그룹으로 시작했으므로 자식까지 함께 내린다.
  stop_group_bounded "$nav_pid"
  echo "로그: $RUN_DIR"
  trap - INT TERM HUP
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

echo "=================================================="
echo " 실물 TurtleBot3 관제 스택 시작"
echo " 지도:       $MAP_FILE"
echo " 낮은 장애물 기록: $BLIND_OBSTACLE_STORE"
echo " 초기 위치:  x=$INITIAL_X, y=$INITIAL_Y, yaw=$INITIAL_YAW"
echo " 위치 출처:  $POSE_SOURCE"
echo " 관제 PC IP: $PC_IP"
echo " ROS 도메인: $ROS_DOMAIN_ID"
echo " ROS 워크스페이스: $ROS_WS"
echo " 미니맵:     http://$PC_IP:$MINIMAP_PORT/"
echo " 대시보드:   http://localhost:$DASHBOARD_PORT"
echo " RAG/LLM:    $([[ "$ENABLE_RAG" == "1" ]] && echo '실행' || echo '사용 안 함')"
echo " TTS 송신:   $([[ "$ENABLE_TTS" == "1" ]] && echo "$JETSON_IP:$TTS_TCP_PORT" || echo '사용 안 함')"
echo " 카메라 방향 보정: ${CAMERA_YAW_OFFSET}rad"
echo " LiDAR 드라이버: Jetson LDS_MODEL=${EXPECTED_LDS_MODEL} 기준"
echo "=================================================="
echo "Jetson의 robot.launch.py가 실행 중이어야 합니다."
echo "RViz의 LiDAR 모양은 모델 구분 없이 공용 lds.stl을 쓰므로 LDS-02처럼 보여도 정상입니다."
echo "낮은 장애물은 순찰 목표 생성과 Nav2 local/global KeepoutFilter에 반영됩니다."
echo

# 독립 프로세스 그룹으로 띄워야 Ctrl+C 시 launch와 모든
# Nav2/RViz 자식을 한 번에 종료할 수 있다.
setsid ros2 launch smart_factory_sim real_navigation.launch.py \
  "map:=$MAP_FILE" \
  "blind_obstacle_store:=$BLIND_OBSTACLE_STORE" \
  "initial_x:=$INITIAL_X" \
  "initial_y:=$INITIAL_Y" \
  "initial_yaw:=$INITIAL_YAW" >"$RUN_DIR/nav2.log" 2>&1 &
nav_pid=$!

# Nav2가 /map과 TF를 준비할 시간을 조금 준 뒤 미니맵을 붙인다.
sleep 4
kill -0 "$nav_pid" 2>/dev/null || {
  tail -n 40 "$RUN_DIR/nav2.log" >&2 || true
  die "Nav2가 시작 직후 종료했습니다."
}

(cd "$SCRIPT_DIR/dashboard_link" && exec python3 minimap_renderer.py) \
  >"$RUN_DIR/minimap.log" 2>&1 &
minimap_pid=$!

sleep 2
kill -0 "$minimap_pid" 2>/dev/null || {
  tail -n 40 "$RUN_DIR/minimap.log" >&2 || true
  die "미니맵 서버가 시작 직후 종료했습니다."
}

if [[ "$ENABLE_RAG" == "1" ]]; then
  rag_args=()
  if [[ "$ENABLE_LLM" == "1" ]]; then
    rag_args+=(--use-llm --async-llm)
  fi
  if [[ "$ENABLE_TTS" == "1" ]]; then
    rag_args+=(--tts)
    if ! timeout 2 bash -c "</dev/tcp/$JETSON_IP/$TTS_TCP_PORT" 2>/dev/null; then
      echo "경고: Jetson TTS 수신기($JETSON_IP:$TTS_TCP_PORT)가 아직 없습니다." >&2
      echo "      Jetson에서 source set_env.sh 후 python3 tts/Rx_pipeline.py 를 실행하세요." >&2
    fi
  fi

  (cd "$DASHBOARD_DIR" && exec ./run_pipeline_rag.sh "${rag_args[@]}") \
    >"$RUN_DIR/rag.log" 2>&1 &
  rag_pid=$!

  sleep 2
  kill -0 "$rag_pid" 2>/dev/null || {
    tail -n 60 "$RUN_DIR/rag.log" >&2 || true
    die "RAG/LLM 파이프라인이 시작 직후 종료했습니다. UDP 9998 중복 실행과 DB 설정을 확인하세요."
  }
fi

# RAG 실행기는 set_env.sh를 자체적으로 읽지만, Streamlit은 예전에 이
# 파일을 읽지 않았습니다. Jetson 수신기에 TTS_TOKEN이 설정되면 자동
# 방송은 성공해도 대시보드의 수동 방송·볼륨 제어는 인증 거부됐습니다.
# 대시보드도 같은 설정을 읽되, 이 스택 명령에 직접 준 IP/포트가
# set_env.sh 값으로 덮어쓰여지지 않도록 별도 변수로 보존합니다.
dashboard_jetson_ip="$JETSON_IP"
dashboard_tts_port="$TTS_TCP_PORT"
(cd "$DASHBOARD_DIR" && source ./set_env.sh >/dev/null && \
  export JETSON_IP="$dashboard_jetson_ip" TTS_TCP_PORT="$dashboard_tts_port" && \
  exec .venv/bin/streamlit run dashboard/smart_factory_dashboard_v3.py \
  --server.address 0.0.0.0 --server.port "$DASHBOARD_PORT") \
  >"$RUN_DIR/dashboard.log" 2>&1 &
dashboard_pid=$!

sleep 2
kill -0 "$dashboard_pid" 2>/dev/null || {
  tail -n 40 "$RUN_DIR/dashboard.log" >&2 || true
  die "대시보드가 시작 직후 종료했습니다."
}

if [[ "$POSE_SOURCE" == 자동\ 인계* || "$POSE_SOURCE" == "사용자 지정 환경변수" ]]; then
  echo "저장된 초기 위치를 AMCL에 전달했습니다. RViz에서 LaserScan과 벽이 겹치는지 확인하세요."
  echo "어긋날 때만 2D Pose Estimate로 위치를 다시 지정하세요."
else
  echo "자동 인계 좌표가 없습니다. RViz에서 먼저 2D Pose Estimate를 지정하세요."
fi
echo "그 뒤 대시보드 지도에서 흰색 탐사 영역을 더블클릭하면 확인 후 이동합니다."
echo "종료: 이 터미널에서 Ctrl+C"

wait "$dashboard_pid"
