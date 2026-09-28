#!/usr/bin/env bash
# Gazebo 맵 기반 디지털 트윈 + 관제 대시보드 전용 실행기.
#
# 실물 TurtleBot3, Jetson, /motor_power, 실물 Nav2를 전혀 시작하지 않는다.
# ROS 도메인 31을 기본값으로 사용해, 도메인 30의 실물 운용 스택과 토픽이 섞이지 않는다.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="$REPO_ROOT/데이터 플랫폼 및 대시보드(이상민)"
ROS_SETUP="/opt/ros/${ROS_DISTRO:-humble}/setup.bash"

TWIN_DOMAIN_ID="${TWIN_DOMAIN_ID:-31}"
TWIN_BIND="${TWIN_BIND:-127.0.0.1}"
TWIN_VIDEO_PORT="${TWIN_VIDEO_PORT:-8500}"
TWIN_MINIMAP_PORT="${TWIN_MINIMAP_PORT:-8092}"
TWIN_DASHBOARD_PORT="${TWIN_DASHBOARD_PORT:-8502}"
TWIN_UDP_PORT="${TWIN_UDP_PORT:-9996}"
TWIN_MIRROR_UDP_PORT="${TWIN_MIRROR_UDP_PORT:-9995}"
TWIN_DETECTION_UDP_PORT="${TWIN_DETECTION_UDP_PORT:-9994}"
REAL_DOMAIN_ID="${REAL_DOMAIN_ID:-30}"
GUI="${GUI:-true}"
USE_RVIZ="${USE_RVIZ:-true}"
PATROL="${PATROL:-false}"
# 기본 실행은 실물 위치를 따르는 안전한 미러 모드다. 가상 Nav2 순찰은
# LIVE_MIRROR=0 PATROL=true 로 명시한 경우에만 동작한다.
LIVE_MIRROR="${LIVE_MIRROR:-1}"
if [[ "$LIVE_MIRROR" == 1 ]]; then
  ENABLE_RAG="${ENABLE_RAG:-0}"
  LIVE_DETECTION_MODELS="${LIVE_DETECTION_MODELS:-1}"
else
  ENABLE_RAG="${ENABLE_RAG:-1}"
  LIVE_DETECTION_MODELS="${LIVE_DETECTION_MODELS:-0}"
fi
ENABLE_MOCK_EVENTS="${ENABLE_MOCK_EVENTS:-0}"
ENABLE_LLM="${ENABLE_LLM:-1}"
# 시뮬레이션 이벤트가 실물 Jetson TTS로 송신되지 않게 기본값은 0이다.
ENABLE_TTS="${ENABLE_TTS:-0}"
ENABLE_DASHBOARD="${ENABLE_DASHBOARD:-1}"

die() {
  echo "오류: $*" >&2
  exit 1
}

for toggle in ENABLE_RAG ENABLE_LLM ENABLE_TTS ENABLE_DASHBOARD LIVE_MIRROR \
    ENABLE_MOCK_EVENTS LIVE_DETECTION_MODELS; do
  value="${!toggle}"
  [[ "$value" == 0 || "$value" == 1 ]] || die "$toggle 은 0 또는 1이어야 합니다."
done

[[ -f "$ROS_SETUP" ]] || die "ROS 환경을 찾지 못했습니다: $ROS_SETUP"
if [[ -f "$REPO_ROOT/.runtime/ros-install/setup.bash" ]]; then
  WORKSPACE_SETUP="$REPO_ROOT/.runtime/ros-install/setup.bash"
  ROS_WS="$REPO_ROOT/.runtime"
elif [[ -f "$REPO_ROOT/install/setup.bash" ]]; then
  WORKSPACE_SETUP="$REPO_ROOT/install/setup.bash"
  ROS_WS="$REPO_ROOT"
else
  die "설치본이 없습니다. 먼저 .runtime에서 colcon build를 실행하세요."
fi

if find "$SCRIPT_DIR/smart_factory_sim" "$SCRIPT_DIR/launch" "$SCRIPT_DIR/config" \
    "$SCRIPT_DIR/worlds" "$SCRIPT_DIR/models" "$SCRIPT_DIR/setup.py" -type f \
    \( -name '*.py' -o -name '*.world' -o -name '*.sdf' -o -name '*.obj' \
       -o -name '*.json' -o -name 'setup.py' \) -newer "$WORKSPACE_SETUP" \
    -print -quit | grep -q .; then
  die "디지털 트윈 소스가 설치본보다 최신입니다. 아래 빌드 명령을 한 번 실행하세요."
fi

if [[ "$ENABLE_DASHBOARD" == 1 ]]; then
  [[ -x "$DASHBOARD_DIR/.venv/bin/streamlit" ]] || \
    die "대시보드 가상환경을 찾지 못했습니다: $DASHBOARD_DIR/.venv"
  [[ -f "$DASHBOARD_DIR/.streamlit/secrets.toml" ]] || \
    die "대시보드 DB 설정 파일이 없습니다: $DASHBOARD_DIR/.streamlit/secrets.toml"
fi
if [[ "$ENABLE_RAG" == 1 ]]; then
  [[ -x "$DASHBOARD_DIR/run_pipeline_rag.sh" ]] || \
    die "RAG 실행기를 찾지 못했습니다: $DASHBOARD_DIR/run_pipeline_rag.sh"
fi

export ROS_DOMAIN_ID="$TWIN_DOMAIN_ID"
export ROS_LOCALHOST_ONLY=1
export TURTLEBOT3_MODEL=burger
export MINIMAP_TOKEN="${MINIMAP_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
export MINIMAP_BIND="$TWIN_BIND"
export MINIMAP_PORT="$TWIN_MINIMAP_PORT"
export MINIMAP_ALLOW_GOTO=1
export DAEUN_LAPTOP_IP="$TWIN_BIND"
export JETSON_IP="$TWIN_BIND"
export JETSON_VIDEO_PORT="$TWIN_VIDEO_PORT"

# Gazebo 로봇의 자율주행과 실물 위치 반영을 동시에 켜면 서로 다른 위치를
# 계속 명령하게 된다. LIVE_MIRROR는 실물 AMCL 위치를 유일한 기준으로 쓴다.
if [[ "$LIVE_MIRROR" == 1 ]]; then
  USE_NAV2=false
  PATROL=false
else
  USE_NAV2=true
fi

source "$ROS_SETUP"
source "$WORKSPACE_SETUP"
set -u

RUN_DIR="$(mktemp -d /tmp/smart-factory-mapped-twin.XXXXXX)"
launch_pid=""
camera_pid=""
minimap_pid=""
rag_pid=""
dashboard_pid=""
forwarder_pid=""
detection_forwarder_pid=""

stop_process() {
  # 모든 주요 구성요소를 setsid로 띄웠다. 프로세스 그룹 전체를 종료해야
  # ros2 launch가 띄운 Gazebo/Nav2 자식이 남지 않는다.
  local pid="$1"
  local signal attempts
  [[ -n "$pid" ]] || return 0
  for signal in INT TERM KILL; do
    kill -"$signal" -- "-$pid" 2>/dev/null || kill -"$signal" "$pid" 2>/dev/null || true
    attempts=0
    while (( attempts < 20 )); do
      kill -0 -- "-$pid" 2>/dev/null || return 0
      sleep 0.1
      attempts=$((attempts + 1))
    done
  done
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  echo
  echo "디지털 트윈 관제 스택을 종료합니다..."
  stop_process "$dashboard_pid"
  stop_process "$rag_pid"
  stop_process "$minimap_pid"
  stop_process "$camera_pid"
  stop_process "$detection_forwarder_pid"
  stop_process "$forwarder_pid"
  stop_process "$launch_pid"
  echo "로그: $RUN_DIR"
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

echo "=================================================="
echo " Gazebo 맵 기반 디지털 트윈 + 대시보드"
echo " ROS 도메인:       $ROS_DOMAIN_ID (실물 도메인 30과 분리)"
echo " Gazebo / Nav2:    $([[ "$USE_NAV2" == true ]] && echo 실행 || echo '실물 위치 반영 모드')"
echo " Gazebo 카메라:    http://$TWIN_BIND:$TWIN_VIDEO_PORT/"
echo " 미니맵:           http://$TWIN_BIND:$TWIN_MINIMAP_PORT/?token=$MINIMAP_TOKEN"
echo " 대시보드:         http://$TWIN_BIND:$TWIN_DASHBOARD_PORT"
echo " mock 이벤트 UDP:  127.0.0.1:$TWIN_UDP_PORT"
echo " 가상 이벤트 표식:  $([[ "$ENABLE_MOCK_EVENTS" == 1 ]] && echo 표시 || echo '표시 안 함')"
echo " 실제 탐지 3D 모델: $([[ "$LIVE_DETECTION_MODELS" == 1 ]] && echo 표시 || echo '표시 안 함')"
echo " RAG/LLM:          $([[ "$ENABLE_RAG" == 1 ]] && echo 실행 || echo '사용 안 함')"
echo " Jetson TTS:       $([[ "$ENABLE_TTS" == 1 ]] && echo 실행 || echo '사용 안 함 (기본값)')"
if [[ "$LIVE_MIRROR" == 1 ]]; then
  echo " 실물 위치 미러:   ROS 도메인 $REAL_DOMAIN_ID map→base_footprint TF → UDP 127.0.0.1:$TWIN_MIRROR_UDP_PORT"
  [[ "$ENABLE_RAG" == 1 ]] && \
    echo " 주의: Gazebo mock 이벤트도 DB에 기록됩니다. 위치 비교만 할 때는 ENABLE_RAG=0을 권장합니다."
fi
echo "=================================================="

setsid ros2 launch smart_factory_sim mapped_digital_twin.launch.py \
  "gui:=$GUI" "use_nav2:=$USE_NAV2" "use_rviz:=$USE_RVIZ" "patrol:=$PATROL" \
  "mock_udp_host:=127.0.0.1" "mock_udp_port:=$TWIN_UDP_PORT" \
  "mirror_real_pose:=$([[ "$LIVE_MIRROR" == 1 ]] && echo true || echo false)" \
  "mirror_udp_host:=127.0.0.1" "mirror_udp_port:=$TWIN_MIRROR_UDP_PORT" \
  "mirror_real_detections:=$([[ "$LIVE_DETECTION_MODELS" == 1 ]] && echo true || echo false)" \
  "detection_udp_host:=127.0.0.1" "detection_udp_port:=$TWIN_DETECTION_UDP_PORT" \
  "enable_mock_events:=$([[ "$ENABLE_MOCK_EVENTS" == 1 ]] && echo true || echo false)" \
  >"$RUN_DIR/gazebo_nav2.log" 2>&1 &
launch_pid=$!

sleep 5
kill -0 "$launch_pid" 2>/dev/null || {
  tail -n 60 "$RUN_DIR/gazebo_nav2.log" >&2 || true
  die "Gazebo/디지털 트윈이 시작 직후 종료했습니다."
}

if [[ "$LIVE_MIRROR" == 1 ]]; then
  # 별도 프로세스에만 실물 도메인 30을 부여한다. 이 실행기는 위치를 읽어
  # localhost UDP로 전달할 뿐, 실물 /cmd_vel·모터 서비스에는 접속하지 않는다.
  setsid env ROS_DOMAIN_ID="$REAL_DOMAIN_ID" ROS_LOCALHOST_ONLY=0 \
    ros2 run smart_factory_sim real_pose_udp_forwarder --ros-args \
    -p udp_host:=127.0.0.1 -p udp_port:="$TWIN_MIRROR_UDP_PORT" \
    >"$RUN_DIR/real_pose_forwarder.log" 2>&1 &
  forwarder_pid=$!
  sleep 1
  kill -0 "$forwarder_pid" 2>/dev/null || {
    tail -n 60 "$RUN_DIR/real_pose_forwarder.log" >&2 || true
    die "실물 위치 전달기가 시작 직후 종료했습니다."
  }
fi

if [[ "$LIVE_DETECTION_MODELS" == 1 ]]; then
  setsid env ROS_DOMAIN_ID="$REAL_DOMAIN_ID" ROS_LOCALHOST_ONLY=0 \
    ros2 run smart_factory_sim real_detection_udp_forwarder --ros-args \
    -p udp_host:=127.0.0.1 -p udp_port:="$TWIN_DETECTION_UDP_PORT" \
    >"$RUN_DIR/real_detection_forwarder.log" 2>&1 &
  detection_forwarder_pid=$!
  sleep 1
  kill -0 "$detection_forwarder_pid" 2>/dev/null || {
    tail -n 60 "$RUN_DIR/real_detection_forwarder.log" >&2 || true
    die "실물 탐지 위치 전달기가 시작 직후 종료했습니다."
  }
fi

setsid ros2 run smart_factory_sim gazebo_camera_stream --ros-args \
  -p bind_host:="$TWIN_BIND" -p port:="$TWIN_VIDEO_PORT" \
  >"$RUN_DIR/camera.log" 2>&1 &
camera_pid=$!

setsid bash -c "cd \"$SCRIPT_DIR/dashboard_link\" && exec python3 minimap_renderer.py" \
  >"$RUN_DIR/minimap.log" 2>&1 &
minimap_pid=$!

sleep 2
for pair in "camera:$camera_pid:$RUN_DIR/camera.log" "minimap:$minimap_pid:$RUN_DIR/minimap.log"; do
  IFS=: read -r name pid log_file <<<"$pair"
  kill -0 "$pid" 2>/dev/null || {
    tail -n 60 "$log_file" >&2 || true
    die "$name 프로세스가 시작 직후 종료했습니다."
  }
done

if [[ "$ENABLE_RAG" == 1 ]]; then
  rag_args=(--udp-port "$TWIN_UDP_PORT")
  [[ "$ENABLE_LLM" == 1 ]] && rag_args+=(--use-llm --async-llm)
  [[ "$ENABLE_TTS" == 1 ]] && rag_args+=(--tts)
  setsid bash -c "cd \"$DASHBOARD_DIR\" && exec ./run_pipeline_rag.sh \"\$@\"" \
    twin-rag "${rag_args[@]}" >"$RUN_DIR/rag.log" 2>&1 &
  rag_pid=$!
  sleep 2
  kill -0 "$rag_pid" 2>/dev/null || {
    tail -n 60 "$RUN_DIR/rag.log" >&2 || true
    die "RAG 파이프라인이 시작 직후 종료했습니다."
  }
fi

if [[ "$ENABLE_DASHBOARD" == 1 ]]; then
  setsid bash -c '
    cd "$1"
    source ./set_env.sh >/dev/null
    export JETSON_IP="$2" JETSON_VIDEO_PORT="$3" DAEUN_LAPTOP_IP="$2"
    export MINIMAP_PORT="$4" MINIMAP_TOKEN="$5"
    exec .venv/bin/streamlit run dashboard/smart_factory_dashboard_v3.py \
      --server.address "$2" --server.port "$6"
  ' twin-dashboard "$DASHBOARD_DIR" "$TWIN_BIND" "$TWIN_VIDEO_PORT" \
    "$TWIN_MINIMAP_PORT" "$MINIMAP_TOKEN" "$TWIN_DASHBOARD_PORT" \
    >"$RUN_DIR/dashboard.log" 2>&1 &
  dashboard_pid=$!
  sleep 2
  kill -0 "$dashboard_pid" 2>/dev/null || {
    tail -n 60 "$RUN_DIR/dashboard.log" >&2 || true
    die "대시보드가 시작 직후 종료했습니다."
  }
fi

echo
echo "준비 완료. 브라우저에서 http://$TWIN_BIND:$TWIN_DASHBOARD_PORT 를 여세요."
if [[ "$LIVE_MIRROR" == 1 ]]; then
  echo "Gazebo 로봇은 실물 로봇을 따라가며, 실물 화재·작업자 탐지가 3D 모델로 표시됩니다."
else
  echo "대시보드에는 Gazebo 카메라 영상, 가상 로봇 미니맵, 시뮬레이션 이벤트가 표시됩니다."
fi
echo "종료: 이 터미널에서 Ctrl+C"
wait "$launch_pid"
