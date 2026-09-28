# 실물 SLAM 맵 기반 Gazebo 디지털 트윈

`factory_map.pgm/.yaml`의 점유 셀을 2m 높이의 3D 벽으로 올리고, 실물
TurtleBot3와 AI 탐지 위치를 Gazebo에 실시간으로 반영하는 디지털 트윈
경로입니다.

## 새 맵으로 월드 다시 만들기

```bash
cd "ROS2_자율주행_및_연동(이다은)"
python3 maps/map_to_gazebo_world.py --map /home/<user>/factory_map_auto.yaml
```

생성/갱신되는 파일은 다음과 같습니다.

- `models/factory_map_twin/meshes/walls.obj`: 점유 셀을 압출한 3D 벽 메시
- `models/factory_map_twin/model.sdf`: 시각/추돌용 정적 Gazebo 모델
- `worlds/factory_digital_twin.world`: 벽과 로봇이 들어갈 Gazebo 월드
- `config/factory_digital_twin.json`: 맵에서 계산한 로봇 시작 위치·시험용 이벤트 위치·순찰 지점

원본 `factory_map.pgm/.yaml`은 절대 수정하지 않습니다. 기본적으로 1개 셀만 고립된 점유
점은 SLAM 노이즈로 간주해 벽에서 제외합니다. 고립 점도 모두 벽으로 올리려면:

```bash
python3 maps/map_to_gazebo_world.py --min-component-cells 1
```

생성기는 입력한 YAML의 절대 경로도 메타데이터에 기록합니다. 따라서 Gazebo 벽과
Nav2는 언제나 **같은** 저장 지도를 사용합니다.

벽 높이는 `--wall-height 2.5`처럼 바꿀 수 있습니다. 회색 미탐사 셀은 실제 구조를
알 수 없으므로 벽으로 만들지 않습니다.

## 빌드·실행

```bash
cd /home/<user>/han_2026
source /opt/ros/humble/setup.bash
colcon --log-base /home/<user>/han_2026/.runtime/ros-log build \
  --base-paths '/home/<user>/han_2026/ROS2_자율주행_및_연동(이다은)' \
  --build-base /home/<user>/han_2026/.runtime/ros-build \
  --install-base /home/<user>/han_2026/.runtime/ros-install \
  --symlink-install --packages-select smart_factory_sim
source .runtime/ros-install/setup.bash
ros2 launch smart_factory_sim mapped_digital_twin.launch.py
```

Nav2 기반 자동 순찰까지 보려면:

```bash
ros2 launch smart_factory_sim mapped_digital_twin.launch.py patrol:=true
```

launch는 생성 메타데이터의 한 좌표계를 사용하므로 Gazebo 로봇 스폰 위치,
AMCL 초기 위치, 이벤트 모델, 순찰 지점이 서로 어긋나지 않습니다.

## Gazebo와 대시보드를 동시에 보기

아래는 실물과 연결하지 않고 Gazebo만 자율 순찰하는 **독립 시뮬레이션
모드**입니다. 전용 실행기가 Gazebo, 가상 카메라 영상, 미니맵, RAG,
Streamlit 대시보드를 한 번에 실행합니다. ROS 도메인은 `31`이며 실물
도메인 `30`과 분리됩니다.

```bash
cd "/home/<user>/han_2026/ROS2_자율주행_및_연동(이다은)"
LIVE_MIRROR=0 PATROL=true ./run_mapped_digital_twin_dashboard_stack.sh
```

브라우저는 `http://127.0.0.1:8502`로 엽니다. 대시보드 안의 영상은 Gazebo
`/camera/image_raw`이고, 지도 위 로봇도 Gazebo의 위치입니다. mock 이벤트는
실물 수신 포트 `9998`이 아닌 전용 UDP `9996`을 사용하므로, 실물 RAG 기록과
섞이지 않습니다. 시뮬레이션 이벤트의 실물 음성 안내를 막기 위해 TTS는 기본으로
꺼져 있습니다.

GUI가 없는 PC에서 빠르게 확인할 때는 다음처럼 실행할 수 있습니다.

```bash
LIVE_MIRROR=0 GUI=false USE_RVIZ=false \
  ./run_mapped_digital_twin_dashboard_stack.sh
```

## 실물 로봇 위치를 Gazebo 쌍둥이에 실시간 반영

이 모드는 A(실물 관제 스택)와 B(Gazebo 트윈 스택)를 **동시에** 실행합니다.
먼저 실물 운용 스택(ROS 도메인 30)을 실행하고, 별도 PC 터미널에서 B를
실행합니다.

```bash
cd "/home/<user>/han_2026/ROS2_자율주행_및_연동(이다은)"
./run_mapped_digital_twin_dashboard_stack.sh
```

실행기는 실물 `map → base_footprint` TF를 읽는 도메인 30 전달기와 Gazebo 도메인
31 수신기를 함께 시작합니다. 전달기는 `127.0.0.1:9995`로 x/y/yaw만 단방향
전송하고, 수신기는 Gazebo `turtlebot3_burger` 모델을 그 좌표로 갱신합니다. 실물
`/cmd_vel`, 모터, Jetson에는 명령을 보내지 않습니다. 이 모드에서는 Gazebo Nav2
순찰을 자동으로 끄며, Gazebo 로봇은 실물 로봇이 멈추면 멈추고 움직이면 따라갑니다.

현재 기본 실행이 이 실물 미러 모드입니다. 미리 배치된 가상 사람·빨간 원기둥은
표시하지 않고, 실물 TF가 오지 않으면 Gazebo 로봇은 정지합니다. 가상 이벤트
시험이 정말 필요할 때만 `ENABLE_MOCK_EVENTS=1`을 추가하세요.

## 실물 AI 탐지를 Gazebo 3D 모델로 표시

기본 실물 미러 모드는 A의 확정된 탐지 목록과 지도 x/y 좌표도 도메인 30에서
`127.0.0.1:9994`로 단방향 전달합니다. Gazebo에서는 다음처럼 구분됩니다.

- `fire`: 빨강·주황·노랑 3중 불꽃 모델
- `person`: 파란 작업복과 노란 안전모를 착용한 일반 작업자
- `person with no helmet`: 빨간 작업복·맨머리·빨간 X로 표시한 안전모 미착용 작업자

모델은 AI가 보고한 카메라 거리와 실물 로봇 위치로 계산한 **지도 좌표**에
생성됩니다. 동일 물체가 연속 3회 탐지돼야 표시되며, 마지막 탐지 후 30초가
지나면 제거됩니다. 위치가 갱신되면 같은 3D 모델이 새 좌표로 이동합니다.

이 기능을 추가한 뒤에는 A의 PC `run_real_dashboard_stack.sh`를 한 번 재시작해야
`/tracked_detections` 발행기가 동작합니다. Jetson 비전·bringup·TTS 터미널은 그대로
둔 채 PC의 A와 B만 다시 시작하면 됩니다.
가상 이벤트 시험이 정말 필요할 때만 `ENABLE_MOCK_EVENTS=1`을 추가하세요.

두 시스템이 같은 `factory_map_auto.yaml`에서 만들어진 월드를 써야 좌표가 맞습니다.
실물 지도 생성 후에는 위의 **새 맵으로 월드 다시 만들기**와 빌드를 한 번 실행하세요.

## 현재 맵의 한계

현재 `factory_map` 100×106셀 중 실제로 확인된 자유공간은 1,711셀, 점유 장애물은
90셀이고 8,799셀은 미탐사입니다. 따라서 이 월드는 현재 매핑에서 관측한 약
5.0m×5.3m 구간만 복원합니다. 실제 공장과 같은 범위를 만들려면 실물 로봇으로
전체 구역을 다시 매핑한 뒤 생성기를 재실행해야 합니다.
