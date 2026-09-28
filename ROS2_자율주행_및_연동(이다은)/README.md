# ROS2 자율주행 · 디지털 트윈 · 미니맵 (이다은 파트)

한이음 2026 스마트 팩토리 순찰 로봇 프로젝트에서 **TurtleBot3 기반 ROS2 패키지**
(`smart_factory_sim`)와 **미니맵 렌더러**(`dashboard_link/minimap_renderer.py`)를
담당하는 파트입니다. Gazebo 디지털 트윈 시뮬레이션, Nav2 자율주행/순찰, SLAM 매핑,
그리고 젯슨(손준영 파트)이 보내는 실물 카메라 탐지 결과를 지도 좌표로 변환해
RViz/웹 대시보드/미니맵에 뿌려주는 역할을 합니다.

전체 파이프라인에서 이 파트가 어디에 끼워지는지, 다른 파트와 어떤 순서로 실행해야
하는지는 저장소 최상위 문서를 먼저 보세요.

> **전체 실행 순서 / 데이터 흐름 통합 안내: [`../README.md`](../README.md)**

이 문서에서는 이 파트 안에서만 필요한 내용(폴더 구조, 빌드 방법, 각 조각의 역할)을
다룹니다.

---

## 1. 이 파트가 담당하는 역할

- **디지털 트윈**: Gazebo에서 가상 공장(`worlds/smart_factory.world` — 사방 벽이
  20m 길이 박스 4개로 둘러싸인 약 20x20m 공간에 선반/기둥 형태의 장애물이
  배치된 구조)을 만들고, 커스텀 카메라가 달린 TurtleBot3 Burger를 스폰합니다.
- **자율주행/순찰**: Nav2(AMCL + 경로계획)로 지도 위 웨이포인트를 순환 순찰합니다.
- **SLAM 매핑**: 시뮬레이션(Gazebo) 또는 실물 로봇(Cartographer)으로 지도를
  새로 만들 수 있습니다.
- **이벤트 감지 → 지도 투영**: Gazebo mock 탐지, 로컬 OWL-ViT 탐지, 그리고 **젯슨
  실물 카메라가 보낸 `/safety_status` 탐지 결과**까지 전부 같은 파이프라인
  (`event_detector.py`)에서 지도 좌표(map frame)로 변환해 RViz 마커와
  `/detected_events`로 발행합니다.
- **미니맵**: 다은님 노트북에서 실제로 돌아가는 `dashboard_link/minimap_renderer.py`가
  SLAM 지도 + 로봇 위치 + 젯슨이 보낸 탐지 각도를 결합해, 레이캐스팅으로 실좌표를
  역산하고 MJPEG/JSON으로 외부(이상민 파트 대시보드)에 제공합니다.

### 젯슨 → 이 파트로 들어오는 데이터

`젯슨 연결용 프로그램(손준영)/ai_inference_sender.py`는 감지 결과를 UDP 세 포트로
동시에 보냅니다. 이 파트가 받는 두 갈래는 다음과 같습니다.

| 포트 | 받는 쪽(이 파트) | 형식 | 이후 |
|---|---|---|---|
| `9999` | `vision_inference_node.py`(손준영 폴더)가 받아 ROS2 `/safety_status`로 재발행 | `std_msgs/String`, JSON | `smart_factory_sim/event_detector.py`의 `safety_status_callback()`이 구독 |
| `9091` | `dashboard_link/minimap_renderer.py` | `{"label", "angle_offset"}` (라디안, 카메라 정면 기준) | 레이캐스팅으로 지도 좌표 역산 |

`9998`(대시보드/DB용)은 이 파트를 거치지 않고 이상민 폴더로 직접 갑니다.

---

## 2. 폴더 구조

```
ROS2_자율주행_및_연동(이다은)/
├── smart_factory_sim/          # ROS2 파이썬 패키지 본체 (노드 6개)
│   ├── event_detector.py       # 이벤트 감지 → 지도 투영 → RViz/대시보드 발행
│   ├── patrol_node.py          # Nav2 액션으로 웨이포인트 순찰
│   ├── initial_pose_pub.py     # AMCL 초기 위치 발행
│   ├── auto_mapper.py          # A* 프런티어 감독형 자동 매핑
│   ├── step_teleop.py          # 키보드 단계 이동(수동 조종)
│   ├── web_dashboard.py        # 자체 웹 대시보드(HTTP 서버)
│   └── templates/index.html    # web_dashboard.py가 서빙하는 페이지
├── dashboard_link/
│   ├── minimap_renderer.py     # 미니맵 서버 (다은님 노트북에서 실행)
│   ├── minimap_web.html        # 브라우저용 미니맵 페이지 (서버가 '/' 로 서빙)
│   └── run_minimap.sh          # 위 스크립트 실행 래퍼
├── launch/                     # ROS2 launch 파일 3종
├── maps/                       # 지도 3벌 + 월드에서 지도를 만드는 생성 스크립트
├── config/                     # Nav2 파라미터, explore 파라미터, RViz 설정
├── models/                     # Gazebo 모델(로봇 카메라, 화재/헬멧 이벤트 마커)
├── urdf/                       # TurtleBot3 Burger + 카메라 URDF
├── worlds/                     # Gazebo 월드 파일 2종
├── resource/smart_factory_sim  # ament_python 리소스 마커(빈 파일)
├── package.xml / setup.py / setup.cfg   # ROS2 colcon 패키지 정의
```

각 하위 폴더의 상세 내용은 폴더별 README를 참고하세요.

| 하위 문서 | 내용 |
|---|---|
| [`smart_factory_sim/README.md`](smart_factory_sim/README.md) | 노드 6개 파일별 상세 설명(토픽/파라미터/실행법) |
| [`dashboard_link/README.md`](dashboard_link/README.md) | 미니맵 엔드포인트/환경변수/접근 제어/실행법 |
| [`launch/README.md`](launch/README.md) | launch 파일 3종 비교와 실행 인자, 월드·지도·경로 짝 |
| [`maps/README.md`](maps/README.md) | 월드에서 지도를 생성하는 방법과 지도 3벌의 용도 |

`config/`, `models/`, `urdf/`, `worlds/`는 별도 README 없이 여기서 간단히 짚습니다.

- **`config/nav2_params.yaml`**: Nav2 스택(AMCL, 플래너, 컨트롤러, costmap 등) 표준 파라미터. 두 launch 파일의 nav2_bringup에 전달됩니다. `amcl.initial_pose`는 로봇 스폰 좌표 `(0.5, 3.5)`와 반드시 같아야 합니다.
- **`config/explore_params.yaml`**: [m-explore/explore_lite](https://github.com/robo-friends/m-explore-ros2) 계열의 frontier 탐색 파라미터(`explore_node`, `explore_costmap`). 이 패키지 안 어떤 노드/launch에서도 참조하지 않으므로, explore_lite를 별도로 띄울 때 쓰는 참고용 설정입니다. `auto_mapper.py`(Nav2 없이 `/map`에서 A* 웨이포인트를 추종하는 감독형 탐색기)와는 별개입니다.
- **`config/rviz_config.rviz`**: 두 launch가 띄우는 RViz의 저장된 뷰 설정(맵, 이벤트 마커 등 표시).
- **`models/`**: Gazebo SDF 모델 3종 — `turtlebot3_burger_camera`(커스텀 카메라 부착 로봇), `fire_event`/`helmet_violation_event`(event_detector.py의 mock 탐지 대상이 되는 표식 모델).
- **`urdf/turtlebot3_burger_camera.urdf`**: 위 로봇 모델의 URDF. `robot_state_publisher`가 로드해 TF를 발행합니다.
- **`worlds/digital_twin_1.world`**: 랙 6개가 3m 간격으로 놓인 12x8m 공장. `digital_twin.launch.py`가 쓰는 **순찰 데모용 메인 월드**입니다. **`worlds/smart_factory.world`**: 20x20m 공간에 장애물 11개. `simulation.launch.py`가 쓰는 주행 시험용 월드.

> **월드를 수정했다면 반드시 지도를 다시 만드세요.** `python3 maps/generate_map.py`가
> 월드 파일을 파싱해서 지도를 생성하므로, 다시 돌리지 않으면 지도와 월드가
> 어긋나 순찰이 전부 실패합니다.

---

## 3. 빌드 & 실행

ROS2 colcon 워크스페이스 안에 이 폴더(정확히는 `smart_factory_sim` 패키지)가
들어있어야 합니다. `package.xml`의 `<build_type>`이 `ament_python`이므로 pip이
아니라 colcon으로 빌드합니다.

```bash
# 워크스페이스 루트에서
colcon build --packages-select smart_factory_sim
source install/setup.bash

# ROS 패키지와 미니맵의 하드웨어 없는 회귀 테스트 9개
colcon test --packages-select smart_factory_sim --event-handlers console_direct+
colcon test-result --verbose
```

`colcon test`는 자동 매핑·순찰 계획·미니맵·낮은 장애물·실물 내비게이션
배선 테스트를 실제로 실행합니다. 0건을 실행하고 성공으로 표시하는 구성은
허용하지 않습니다.

의존 패키지(`package.xml` 기준): `rclpy`, `sensor_msgs`, `geometry_msgs`,
`visualization_msgs`, `nav2_msgs`, `tf2_ros`, `cv_bridge`, `std_msgs`,
`gazebo_msgs`, `nav_msgs`, `action_msgs`. 실행 의존성으로
`robot_state_publisher`, `gazebo_ros`, `nav2_bringup`, `rviz2`가 선언되어 있습니다.

```bash
rosdep install --from-paths src --ignore-src -r -y
```

그 외 `event_detector.py`의 실물 탐지 모드(`use_mock:=false`)를 쓰려면
`torch`, `transformers`도 필요합니다(없으면 자동으로 mock 모드로 폴백).

> `gazebo_msgs`·`nav_msgs`·`action_msgs`는 코드가 쓰고 있는데 선언이 빠져 있던
> 것들입니다. 특히 `initial_pose_pub.py`가 선언되지 않은 `tf_transformations`를
> import해서, 미설치 환경에서는 노드가 조용히 죽고 로봇 위치만 틀어졌습니다.
> 지금은 그 import 자체를 없앴습니다.

빌드 후에는 `ros2 launch smart_factory_sim <launch파일>` 또는
`ros2 run smart_factory_sim <executable>`로 실행합니다. 구체적인 실행 조합은
[`launch/README.md`](launch/README.md)와 [`smart_factory_sim/README.md`](smart_factory_sim/README.md)를
참고하고, 전체 파이프라인 순서는 [`../README.md`](../README.md)를 따르세요.

미니맵 렌더러(`dashboard_link/minimap_renderer.py`)는 colcon 패키지가 아니라
독립 파이썬 스크립트입니다. ROS2 환경(`source install/setup.bash`)만 되어 있으면
`rclpy`를 그대로 쓸 수 있고, `flask`, `opencv-python`, `numpy`는 pip으로 따로
설치해야 합니다. 자세한 내용은 [`dashboard_link/README.md`](dashboard_link/README.md).

---

## 4. 자율주행 실행 방법

**랙 배치 공장에서 순찰(기본)**

```bash
ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true
```

**넓은 장애물 월드에서 주행 시험**

```bash
ros2 launch smart_factory_sim simulation.launch.py patrol:=true
```

공통 인자: `use_nav2`(기본 true), `use_rviz`(기본 true), `patrol`(기본 false),
`gui`(기본 true). 순찰만 따로 붙이려면 Nav2가 뜬 뒤
`ros2 run smart_factory_sim patrol_node --ros-args -p use_sim_time:=true`.

### 세 값은 항상 함께 움직입니다

월드 / 지도 / 웨이포인트가 어긋나면 순찰이 전부 실패합니다. launch별 짝은 이렇습니다.

| launch | 월드 | 지도 | 웨이포인트 | 최소 여유 |
|---|---|---|---|---|
| `digital_twin.launch.py` | `digital_twin_1.world` | `maps/digital_twin_map.yaml` | `patrol_node` 기본값 7개 | 1.05m |
| `simulation.launch.py` | `smart_factory.world` | `maps/smart_factory_map.yaml` | launch 안 `SMART_FACTORY_WAYPOINTS` 8개 | 1.80m |

지도는 **월드 파일에서 자동 생성**합니다. 월드를 고쳤다면 반드시 다시 만드세요.

```bash
python3 maps/generate_map.py     # 두 지도 모두 재생성
```

로봇 시작 좌표 `(0.5, 3.5)`는 세 곳이 일치해야 합니다 — launch의 `START_X/START_Y`,
`config/nav2_params.yaml`의 `amcl.initial_pose`, `initial_pose_pub`의 `x`/`y` 파라미터.

---

## 5. 알려진 이슈 / 참고사항

- **`maps/factory_map.pgm`/`.yaml`은 실물 로봇 SLAM 산출물입니다.** 5.0×5.3m,
  83%가 미탐사라 시뮬레이션에는 쓸 수 없습니다. `generate_map.py`는 이 파일을
  건드리지 않고 `smart_factory_map`/`digital_twin_map`을 따로 만듭니다.
  자세한 내용은 [`maps/README.md`](maps/README.md) 참고.
- **`config/explore_params.yaml`은 이 패키지의 어떤 노드도 참조하지 않습니다.**
  explore_lite를 별도로 띄울 때 쓰는 참고용 설정입니다. 지금 도는 자율 매핑은
  `auto_mapper.py`이고, 둘의 차이와 한계는 아래 "자율 매핑의 한계"를 보세요.
- **Gazebo mock 탐지가 동작합니다.** 두 월드 모두 `/gazebo/model_states`를
  발행하며, 화재·안전모 미착용 표적 모델을 제공합니다. 이벤트는 RViz/ROS2 웹
  대시보드/미니맵에 표시되고, `mock_event_bridge`가 데이터/RAG 파이프라인의 UDP
  `:9998`로도 전달합니다. Jetson 없이 전체 시연을 하려면 최상위 README의
  “Gazebo mock 시연 경로” 순서를 따르세요.
- **`real_cartographer.launch.py`는 `use_sim_time=False`가 하드코딩되어 있어
  시뮬레이션에서 쓸 수 없습니다.** 실물 로봇 전용입니다. 시뮬레이션 지도는
  `generate_map.py`로 만드는 것이 정확하므로 SLAM을 돌릴 필요가 없습니다.
- **실물 지도를 저장한 뒤에는** `real_cartographer.launch.py`를 끄고
  `real_navigation.launch.py map:=<저장한 yaml>`로 AMCL/Nav2를 실행하세요. 이때
  `/map`과 `map → base_footprint`가 다시 생기므로 미니맵이 저장 지도와 현재
  위치를 함께 보여줍니다. (LiDAR 장애물 빨간 점 레이어는 벽을 물체로 오인하는
  일이 잦아 제거했습니다 — [`dashboard_link/README.md`](dashboard_link/README.md) 참고.)
- **미니맵 렌더러는 `dashboard_link/minimap_renderer.py` 하나뿐입니다.**
  이상민 폴더에 있던 `edge_video/minimap_renderer.py` 사본은 원본과 갈라진 채로
  방치돼 있어 삭제했습니다. 로직은 이 폴더에서만 고치세요.

---

## 6. 자율 매핑(`auto_mapper.py`)의 한계

`run_real_autonomous_mapping.sh`가 돌리는 자율 매핑은 **Nav2 없이 `/scan`,
`/map`, `map → base_footprint` TF를 결합하는 감독형 프런티어 탐색기**입니다.
점유 장애물을 팽창한 지도에서 A* 경로를 만들고 근거리 웨이포인트를 따라가지만,
Nav2의 costmap·회복 행동을 모두 갖춘 완전한 전역 플래너는 아닙니다.

### 알려진 지도 안에서만 경로를 계획합니다

- **벽으로 확인된 셀은 피하지만**, 아직 미탐사인 공간 안쪽을 지나는 경로는
  만들지 않습니다. 현재 알려진 자유공간과 미탐사 경계까지만 이동합니다.
- **잘못 기록된 벽이나 너무 좁은 통로**는 A*가 우회하거나 경로 없음으로
  판정할 수 있습니다. 로봇 반경을 반영한 장애물 팽창은 충돌 위험을 줄이기 위한
  의도된 보수적 판단입니다.
- **커버리지를 보장하지 않습니다.** 끝났는지 스스로 판단하지 못하므로,
  `AUTO_MAX_RUNTIME_SEC`으로 시간을 정하거나 사람이 보고 Ctrl+C로 끊습니다.

### 왜 그런데도 이걸 쓰나

교과서적인 정답은 프론티어 탐사(m-explore / explore_lite)이고, 설정 파일
(`config/explore_params.yaml`)도 이미 있습니다. 다만 그쪽은

- **Nav2 스택이 함께 떠 있어야 합니다.** `run_real_autonomous_mapping.sh`는
  Nav2를 끄고 실행하도록 되어 있습니다(같이 띄우면 두 노드가 `/cmd_vel`을
  서로 밀어내며 로봇이 갈팡질팡합니다).
- **로봇에 패키지를 따로 설치해야 합니다** (`m-explore-ros2`).

지금 단계에서는 "Cartographer + A* 프런티어 주행"으로 첫 지도를 뜨는
것이 목적이라, 추가 패키지 없이 도는 쪽을 택했습니다.

### 언제 explore_lite로 넘어가야 하나

**넓은 공장을 빠짐없이 훑어야 할 때**입니다. 아래 중 하나라도 걸리면 바꾸는 것을
검토하세요.

- 방이 여러 개라 로봇이 한 방에서 못 벗어난다
- 매핑을 여러 번 돌려도 지도에 미탐사 구멍이 남는다
- 사람이 옆에서 계속 로봇을 옮겨 줘야 한다

넘어갈 때 필요한 작업은 대략 이렇습니다.

1. 로봇에 `m-explore-ros2` 설치
2. Cartographer + **Nav2**(맵 없이 SLAM 모드) + `explore_node`를 함께 띄우는
   launch 작성 — `run_real_autonomous_mapping.sh`는 Nav2를 끄는 전제라 그대로는
   못 씁니다
3. `config/explore_params.yaml`의 `robot_base_frame`/`costmap_topic`이 실제
   토픽·프레임과 맞는지 확인

### 먼저 해 볼 수 있는 튜닝

현재 자동매퍼는 `/map`에서 **도달 가능한 프런티어(자유공간과 미탐사 공간의 경계)**를
다음 목표로 잡고 A* 웨이포인트를 따라갑니다. 70도 미만의 방향 오차는 멈추지 않고
원호로 보정하며, 회전 후에는 장애물이 없는 한 1.2초 전진해 연속 제자리 회전을
끊습니다. 실패한 목표 주변은 90초 동안 제외합니다. 따라서 기본값으로
먼저 실행해 보고, 여전히 좁은 통로에서 왕복할 때만 아래 값을 조정하세요. 자세한
설명은 [`smart_factory_sim/README.md`](smart_factory_sim/README.md)의 `auto_mapper.py` 절.

코드를 수정한 뒤에는 빌드하고, Jetson `robot.launch.py`와 `/scan`을 확인하고
Nav2·대시보드·teleop을 종료한 뒤 실행합니다.

```bash
colcon build --symlink-install --packages-select smart_factory_sim

MAP_OUTPUT=~/factory_map_auto \
AUTO_MAX_RUNTIME_SEC=600 \
./run_real_autonomous_mapping.sh
```

```bash
AUTO_VISIT_WEIGHT=4.5 AUTO_REVISIT_LIMIT=2 ./run_real_autonomous_mapping.sh
```

| 변수 | 기본값 | 올리면 / 내리면 |
|---|---|---|
| `AUTO_LINEAR_SPEED` | `0.12` | 전진 속도(m/s). 실물 감독하에서만 올리세요 |
| `AUTO_TURN_SPEED` | `0.40` | 회전 속도(rad/s). 너무 높으면 LDS-03 스캔이 왜곡될 수 있습니다 |
| `AUTO_SAFE_DISTANCE` / `AUTO_SLOW_DISTANCE` | `0.35` / `0.60` | 정지·감속을 시작하는 전방 거리(m) |
| `MAPPING_TRUST_RANGE_M` | `5.0` | 이 거리(m) 밖의 LiDAR 반환점은 새 벽/자유공간 갱신 모두에서 제외합니다. 낮추면 기존 벽은 더 안정적이지만 로봇이 더 가까이 가야 새 영역이 매핑됩니다 |
| `AUTO_VISIT_WEIGHT` | `3.5` | 올리면 이미 지난 쪽을 더 강하게 피합니다. `0`이면 방문 기억을 끄고 예전(순수 반응형)처럼 돕니다 |
| `AUTO_REVISIT_LIMIT` | `2` | 같은 구역에 두 번째 들어오면 바로 다른 프런티어를 찾습니다. 좁은 통로를 충분히 못 훑는다면 `3`으로 올리세요. `0`이면 끕니다 |
