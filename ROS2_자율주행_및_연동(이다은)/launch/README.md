# launch — ROS2 launch 파일 4종

세 launch 파일은 서로 겹치지 않는 별개의 시나리오를 담당합니다. 동시에 여러 개를
띄우도록 만들어진 게 아니라, **하려는 작업에 맞춰 하나만** 골라서 씁니다.

| launch 파일 | 시나리오 | Gazebo | Nav2/AMCL | RViz | 순찰 |
|---|---|---|---|---|---|
| `digital_twin.launch.py` | **순찰 데모(기본)** — 랙 6개 공장 | ✅ `digital_twin_1.world` | ✅ | ✅ | ✅ `patrol:=true` |
| `simulation.launch.py` | 넓은 장애물 월드에서 주행 시험 | ✅ `smart_factory.world` | ✅ | ✅ | ✅ `patrol:=true` |
| `real_cartographer.launch.py` | 실물 로봇 SLAM 매핑 | ❌ (실물 로봇 전제) | ❌ (Cartographer가 대신 매핑) | ✅ | ❌ |
| `real_navigation.launch.py` | **저장한 실물 지도 재사용** | ❌ (실물 로봇 전제) | ✅ | 선택 | ✅ (관제 경로 대기) |

---

## 월드 · 지도 · 웨이포인트는 항상 한 묶음입니다

이 셋 중 하나라도 어긋나면 순찰 목표가 전부 실패합니다. 실제로 예전에는
"웨이포인트가 맞는 월드에는 Nav2가 없고, Nav2가 있는 월드에는 웨이포인트가 안 맞는"
상태였습니다. 지금은 launch별로 짝이 맞춰져 있습니다.

| launch | 월드 | 지도 | 웨이포인트 | 장애물까지 최소 여유 |
|---|---|---|---|---|
| `digital_twin.launch.py` | `worlds/digital_twin_1.world` | `maps/digital_twin_map.yaml` | `patrol_node`의 기본값 7개 | **1.05m** |
| `simulation.launch.py` | `worlds/smart_factory.world` | `maps/smart_factory_map.yaml` | launch 안 `SMART_FACTORY_WAYPOINTS` 8개 | **1.80m** |

지도는 월드 파일에서 자동 생성됩니다. **월드를 고쳤다면 반드시 지도를 다시 만드세요.**

```bash
python3 maps/generate_map.py
colcon build --packages-select smart_factory_sim   # 설치본에 반영
```

### 시작 좌표는 세 곳이 일치해야 합니다

로봇 스폰 위치 `(0.5, 3.5)`는 아래 세 곳에 각각 적혀 있고 서로 같아야 합니다.
하나라도 다르면 AMCL이 엉뚱한 위치에서 시작해 경로가 전부 실패합니다.

| 위치 | 값 |
|---|---|
| launch 파일 상단 `START_X` / `START_Y` | `0.5` / `3.5` |
| `config/nav2_params.yaml`의 `amcl.initial_pose` | `x: 0.5` / `y: 3.5` |
| `initial_pose_pub` 노드의 `x` / `y` 파라미터 | launch가 `START_X`/`START_Y`를 넘겨줌 |

---

## 공통 실행 인자

`digital_twin.launch.py`와 `simulation.launch.py`는 같은 인자를 받습니다.

| 인자 | 기본값 | 의미 |
|---|---|---|
| `use_nav2` | `true` | Nav2 스택(map_server/AMCL/플래너/컨트롤러) + `initial_pose_pub` 실행 여부. SLAM으로 새 지도를 만들 때는 `false`로 꺼야 합니다(AMCL과 SLAM을 동시에 켜면 안 됨) |
| `use_rviz` | `true` | RViz 실행 여부 |
| `patrol` | `false` | Nav2 기동 20초 뒤 `patrol_node` 자동 시작 |
| `gui` | `true` | Gazebo GUI 창 표시 여부 |

`patrol`의 20초 지연은 Nav2 lifecycle 활성화와 AMCL 수렴을 기다리기 위한 것입니다.

---

## digital_twin.launch.py — 순찰 데모용 기본 launch

```bash
ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true
```

랙 6개가 3m 간격 격자로 배치된 12x8m 공장에서 순찰을 돕니다. `patrol_node`의
기본 웨이포인트가 이 월드의 랙 사이 통로 정중앙에 맞춰져 있습니다.

**포함되는 것**

- `GAZEBO_MODEL_PATH`에 이 패키지의 `models/`를 앞에 붙이고 `TURTLEBOT3_MODEL=burger` 설정
- Gazebo — `worlds/digital_twin_1.world`
- `robot_state_publisher` — `urdf/turtlebot3_burger_camera.urdf`
- `spawn_entity.py` — `models/turtlebot3_burger_camera/model.sdf`를 `(0.5, 3.5, 0.01)`에 스폰
- `nav2_bringup` — `map=maps/digital_twin_map.yaml`, `params_file=config/nav2_params.yaml`, `autostart=True`, `use_sim_time=True`
- `rviz2` — `config/rviz_config.rviz`
- `initial_pose_pub` — AMCL이 받을 때까지 반복 발행 (아래 참고)
- `event_detector` — `use_mock=True`
- ~~`web_dashboard`~~ — **더 이상 자동 실행하지 않습니다** (아래 참고)
- `patrol_node` — `patrol:=true`일 때만, 20초 지연

**모든 노드에 `use_sim_time=True`가 설정됩니다.** 예전에는 `robot_state_publisher`와
`rviz2`에만 있어서 커스텀 노드들이 Gazebo 시계와 어긋났습니다.

---

## simulation.launch.py — 넓은 월드 주행 시험

```bash
ros2 launch smart_factory_sim simulation.launch.py patrol:=true
```

사방 벽으로 둘러싸인 20x20m 공간에 박스/원기둥 장애물 11개가 흩어져 있는
월드입니다. 순찰보다는 주행·회피·매핑을 넓은 공간에서 시험하는 용도입니다.

구성은 `digital_twin.launch.py`와 같고 월드/지도/웨이포인트만 다릅니다.
`SMART_FACTORY_WAYPOINTS`는 생성된 지도 위에서 장애물까지의 여유가 가장 큰
지점을 8방향으로 탐색해 넣은 값입니다.

> **주의**: 이 월드에서 `patrol_node`의 **기본** 웨이포인트를 그대로 쓰면 안 됩니다.
> 그 값은 랙 공장용이라, 여기서는 (6.5, 6.0)이 원기둥 장애물 표면에서 0.118m까지
> 붙어 로봇 반경(0.13m)보다 가까워집니다. launch가 넘겨주는
> `SMART_FACTORY_WAYPOINTS`를 쓰세요.

---

## real_cartographer.launch.py — 실물 로봇 SLAM

**실물 TurtleBot3**로 지도를 새로 만들 때 쓰는 launch 파일입니다. Gazebo나 로봇
스폰 관련 내용이 전혀 없고, 이미 실물 로봇 브링업(`/scan` 등 발행 중)이 되어
있다는 것을 전제로 Cartographer만 띄웁니다.

```bash
ros2 launch smart_factory_sim real_cartographer.launch.py
```

기본적으로 5m보다 먼 LiDAR 반환점은 지도 갱신에 쓰지 않습니다. 멀리서 튀는
거리값이 이미 그린 벽을 지우고 더 먼 곳에 새 벽을 만드는 것을 막기 위해서입니다.
미탐사 영역은 로봇이 이 거리 안으로 접근했을 때 매핑됩니다. 직접 launch할 때는
다음처럼 조정할 수 있습니다.

```bash
ros2 launch smart_factory_sim real_cartographer.launch.py mapping_trust_range_m:=4.0
```

사람이 로봇 옆에서 감시할 수 있는 환경이라면, 프로젝트 최상위의 실행기로
Cartographer와 저속 장애물 회피 자율 매핑을 함께 시작할 수 있습니다. Nav2/대시보드
스택은 먼저 종료해야 하며, `Ctrl+C`를 누르면 로봇을 정지한 뒤 지도를 저장합니다.

```bash
cd ~/han_2026/"ROS2_자율주행_및_연동(이다은)"
./run_real_autonomous_mapping.sh
# 기본 저장 위치: ~/factory_map_auto.yaml, ~/factory_map_auto.pgm
```

`AUTO_MAX_RUNTIME_SEC`를 지정하면 마지막 10초 전에 주행을 먼저 멈춘다.
정지 상태에서 `map → base_footprint` 자세가 안정됐는지 확인한 뒤 지도와 같은 이름의
`.pose` 파일을 저장한다. 예를 들어 `factory_map_front.yaml`과 함께
`factory_map_front.pose`가 생성된다. 로봇을 물리적으로 옮기지 않았다면 관제 실행기가
이 파일을 자동으로 읽어 AMCL 초기 위치로 넘긴다.

```bash
MAP_OUTPUT=$HOME/factory_map_front AUTO_MAX_RUNTIME_SEC=600 \
  ./run_real_autonomous_mapping.sh

MAP_FILE=$HOME/factory_map_front.yaml ./run_real_dashboard_stack.sh
```

관제는 가능하면 실행 터미널에서 `Ctrl+C`로 종료하세요. 터미널 창 자체를 닫아도
실행기가 `SIGHUP`을 받아 Nav2 자식까지 정리합니다. 재실행 때 “이전 관제 스택이
아직 실행 중”이라고 나오면 먼저 현재 그래프를 daemon 없이 확인합니다.

```bash
export ROS_DOMAIN_ID=30
ros2 node list --no-daemon --spin-time 2
```

여기에 관제 노드가 없으면 과거 daemon 캐시일 뿐입니다. 노드가 실제로 남아 있으면
`ps -eo pid,pgid,cmd`로 해당 프로세스를 확인한 뒤 기존 관제 터미널에서 종료하세요.
새 실행기는 중복 검사 자체도 `--no-daemon` 결과를 사용합니다.

실행기는 기본적으로 자기 폴더의 `install/setup.bash`를 사용합니다. 패키지를 표준
`~/colcon_ws/src/smart_factory_sim`에 배치했다면 아래처럼 워크스페이스를 명시하세요.

```bash
SFP_ROS_WS=$HOME/colcon_ws MAP_OUTPUT=$HOME/factory_map_front \
  ./run_real_autonomous_mapping.sh
```

시작할 때 `/motor_power` 응답과 `/sensor_state`의 실제 `torque=true`를 모두 확인하며,
둘 중 하나라도 실패하면 Cartographer와 주행 노드를 시작하지 않습니다. 일반 종료는
`/cmd_vel=0`으로만 정지해 재시작 사이에도 토크를 유지합니다. 로봇을 완전히 종료하며
토크까지 끄려면 `DISABLE_MOTOR_ON_EXIT=1`을 함께 지정하세요.

매핑 중 전진 명령이 있는데 지도상 못 움직였을 때는 양쪽 `/sensor_state` 엔코더를
확인합니다. 엔코더도 변하지 않으면 낮은 장애물로 저장하지 않고 `/actuation_fault`로
자율매핑을 안전 정지합니다. 장애물 기록은 기본적으로 지도 출력 옆의
`${MAP_OUTPUT}.blind_obstacles.json`에 저장됩니다.

매핑 후 로봇을 손으로 옮겼다면 자동 pose를 쓰면 안 된다. 그때는
`USE_SAVED_POSE=0`으로 실행하고 RViz의 `2D Pose Estimate`를 사용한다.

**포함되는 것**

- `cartographer_node` — 이 패키지의 `config/real_turtlebot3_lds_2d.lua`,
  `use_sim_time=False`. 기동 직후 epoch(1970) 시각의 `/odom`은
  `odom_filter_relay`가 제거하고, 정상 odometry는 LiDAR scan matching 예측에
  사용합니다.
- `scan_qos_relay` — LDS-03의 `/scan`(best-effort)을 `/scan_reliable`(reliable)로
  중계하고 0/중복/역행 timestamp와 신뢰 거리 밖의 반환점을 제거합니다.
- `odom_filter_relay` — 기동 직후 timestamp=0 또는 시간이 역행하는
  `/odom`만 제거해 `/odom_filtered`로 중계합니다. 정상 odometry는
  회전 중 지도가 펼쳐지는 현상을 줄이는 보조 입력으로 사용합니다.
- `cartographer_occupancy_grid_node` — `resolution=0.05`, `publish_period_sec=1.0`
- `rviz2` — `turtlebot3_cartographer` 패키지 자체의 `tb3_cartographer.rviz`

> `use_sim_time=False`가 하드코딩되어 있어 **시뮬레이션에서는 쓸 수 없습니다.**
> 시뮬레이션에서 SLAM을 돌리려면 이 값을 launch 인자로 빼야 합니다.

지도를 다 그린 뒤에는 이 launch가 저장까지 해주지 않으므로 직접 저장합니다.

```bash
ros2 run nav2_map_server map_saver_cli -f factory_map --free 0.196
```

`--free 0.196`을 생략하면 저장된 미탐사 여백이 재로딩 때 자유 공간으로 바뀌어
대시보드의 순찰 영역이 맵 전체 사각형으로 잡힐 수 있습니다.

## real_navigation.launch.py — 저장 지도로 현재 위치 표시/자율주행

매핑을 끝내고 저장한 `factory_map.yaml`을 다시 쓸 때 실행합니다. 실물 로봇의
`robot.launch.py`는 계속 실행되어 있어야 하며, **Cartographer는 먼저 종료**하세요.
Cartographer와 AMCL/Nav2를 함께 띄우면 두 노드가 모두 `/map`과 좌표변환을 다뤄
현재 위치가 흔들리거나 표시되지 않습니다.

```bash
# 터미널 1: 이미 실행 중인 실물 로봇 브링업 (LiDAR /scan 포함)
ros2 launch turtlebot3_bringup robot.launch.py

# 터미널 2: 저장 지도 + AMCL/Nav2
ros2 launch smart_factory_sim real_navigation.launch.py \
  map:=$HOME/factory_map.yaml initial_x:=0.0 initial_y:=0.0 initial_yaw:=0.0
```

`initial_x`, `initial_y`, `initial_yaw`에는 실제 로봇을 지도에서 놓은 위치를 넣으세요.
RViz에서 `2D Pose Estimate`로 다시 지정해도 됩니다. 이 launch가 발행하는 `/map`과
`map → base_footprint` 변환을 미니맵이 받아 로봇 현재 위치를 표시합니다.

**시뮬레이션 지도는 SLAM으로 만들 필요가 없습니다.** `maps/generate_map.py`가
월드 파일에서 직접 생성하므로 항상 정확합니다. 이 launch는 실물 공장을 매핑할
때만 쓰세요. 자세한 내용은 [`../maps/README.md`](../maps/README.md).

---

## 매핑할 때 조합

시뮬레이션에서 SLAM을 시험해보고 싶다면 Nav2를 끄고 띄운 뒤 수동/자동으로
로봇을 움직입니다.

```bash
# 터미널 1 — Nav2 없이 월드만
ros2 launch smart_factory_sim digital_twin.launch.py use_nav2:=false

# 터미널 2 — SLAM (slam_toolbox 등)
# 터미널 3 — 로봇을 움직인다
ros2 run smart_factory_sim auto_mapper --ros-args -p use_sim_time:=true
#   또는 수동 조종
ros2 run smart_factory_sim step_teleop
```

## web_dashboard 를 launch 에서 뺀 이유

관제 화면은 이상민 파트의 Streamlit 대시보드로 일원화됐고, 지도와 탐지 목록은
`dashboard_link/minimap_renderer.py`(:8091)가 담당합니다. Streamlit 대시보드가
미니맵을 iframe 으로 끌어다 쓰고, `event_logger.py` 와 데이터 파이프라인의
`--zone auto` 도 같은 서버를 부릅니다.

반면 `web_dashboard`(:8080)를 부르는 코드는 저장소에 **하나도 없습니다.**
그런데도 launch 가 항상 띄우고 있어서 두 가지 문제가 있었습니다.

1. 아무도 안 보는 화면을 위해 `/camera/image_raw` 를 프레임마다 `imgmsg_to_cv2` 로
   변환했습니다. 시청자 수를 보지 않는 콜백이라 항상 돕니다. 이 노트북은 Nav2·
   Cartographer·RViz·미니맵이 이미 붙어 있는 가장 바쁜 기기입니다.
2. 공장 도면·로봇 위치·탐지 이력이 인증 없이 `0.0.0.0:8080` 에 열렸습니다.
   미니맵에는 그동안 `MINIMAP_TOKEN` 이 생겼지만 여기는 없습니다.

**파일과 실행 진입점은 그대로 남아 있습니다.** 디버깅할 때는 직접 띄우세요.

```bash
ros2 run smart_factory_sim web_dashboard --ros-args \
  -p port:=8080 -p map_name:=digital_twin_map
```

> 실물 주행에서는 `/camera/image_raw` 가 아예 없어서(Gazebo 전용 토픽) 영상 칸이
> 비어 있습니다. 시뮬레이션 디버깅 용도로만 의미가 있습니다.
