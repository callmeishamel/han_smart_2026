# smart_factory_sim — ROS2 노드 상세

`ROS2_자율주행_및_연동(이다은)` 패키지의 파이썬 소스가 들어있는 폴더입니다.
`setup.py`의 `entry_points`에 등록된 7개 실행 파일과, 그 파일들이 서빙하는
`templates/index.html`이 있습니다. 빌드/설치 방법은 상위
[`../README.md`](../README.md)를 참고하세요.

```
smart_factory_sim/
├── __init__.py            # 패키지 마커 (내용 없음)
├── event_detector.py      # 이벤트 감지 → 지도 좌표 투영 → RViz/대시보드 발행
├── patrol_node.py         # Nav2 NavigateToPose로 웨이포인트 순찰
├── initial_pose_pub.py    # AMCL 초기 위치 1회 발행
├── auto_mapper.py         # A* 프런티어 감독형 자동 매핑
├── mock_event_bridge.py   # Gazebo mock 이벤트 → 데이터/RAG UDP 브리지
├── step_teleop.py         # 키보드 단계 이동(수동 조종, 터미널 raw mode)
├── web_dashboard.py       # 자체 웹 대시보드 HTTP 서버
└── templates/index.html   # web_dashboard.py가 서빙하는 프런트엔드 페이지
```

모든 노드는 빌드 후 `ros2 run smart_factory_sim <executable>`로 개별 실행할 수
있습니다. entry_points 매핑:

| `ros2 run` 실행 파일명 | 모듈:함수 |
|---|---|
| `event_detector` | `smart_factory_sim.event_detector:main` |
| `web_dashboard` | `smart_factory_sim.web_dashboard:main` |
| `patrol_node` | `smart_factory_sim.patrol_node:main` |
| `initial_pose_pub` | `smart_factory_sim.initial_pose_pub:main` |
| `step_teleop` | `smart_factory_sim.step_teleop:main` |
| `auto_mapper` | `smart_factory_sim.auto_mapper:main` |
| `mock_event_bridge` | `smart_factory_sim.mock_event_bridge:main` |

---

## event_detector.py

노드 이름: `event_detector`. 이 파트의 핵심 노드로, 세 가지 서로 다른 탐지 소스를
같은 로직(`_register_event` / `_publish_markers`)으로 합쳐 지도 위 이벤트로
발행합니다.

### 탐지 라벨 철자는 젯슨과 같아야 합니다

같은 개념에 철자가 셋이었습니다.

| 어디 | 예전 철자 |
|---|---|
| mock 탐지 (`run_mock_detection`) | `"no safety helmet"` |
| OWL-ViT 질의 (`self.queries`) | `"person without helmet"` |
| 젯슨 `ai_inference_sender.py` | `"person with no helmet"` |

`common/schema.py`의 `classify_risk()`는 이 문자열을 `ALWAYS_DANGER_OBJECTS` 집합과
**그대로 비교**합니다. 철자가 하나만 어긋나도 조용히 "미분류 → 주의"로 떨어지고,
파이프라인은 위험 등급에서만 대응안을 만들기 때문에 **그 상황에는 대응안이 아예
생성되지 않습니다.** 지금은 셋 다 `"person with no helmet"`로 통일했습니다.

`데이터 플랫폼 및 대시보드(이상민)/self_test/test_integration_wiring.py`가 이 철자가
다시 갈라지지 않는지 검사합니다.

**파라미터**

| 이름 | 기본값 | 용도 |
|---|---|---|
| `use_mock` | `True` | `True`면 Gazebo `ModelStates` 기반 mock 탐지, `False`면 OWL-ViT 실물 탐지 시도 |
| `detection_threshold` | `0.1` | OWL-ViT 탐지 신뢰도 임계값(`use_mock=False`일 때만 사용) |
| `camera_frame` | `camera_rgb_optical_frame` | 시뮬레이션 카메라 TF 프레임 |
| `map_frame` | `map` | 투영 대상 좌표계 |
| `jetson_camera_frame` | `camera_rgb_optical_frame` | **젯슨 실물 카메라**용 TF 프레임 (`ai_inference_sender.py`가 쓰는 값과 맞춰야 함) |
| `jetson_focal_length` | `500.0` | 젯슨 카메라 초점거리(px 단위 근사) |
| `jetson_frame_width` | `1280.0` | 젯슨 카메라 프레임 폭 |
| `jetson_frame_height` | `720.0` | 젯슨 카메라 프레임 높이 |
| `max_events` | `200` | 지도에 유지할 이벤트 개수 상한. 넘으면 가장 오래 재검출되지 않은 것부터 버립니다 |
| `event_retention_sec` | `0.0` | 0보다 크면 그 시간(초) 동안 재검출 없는 이벤트를 삭제. 기본 0은 "시간으로는 안 지움" |
| `marker_publish_period` | `0.5` | RViz 마커 발행 주기(초). `marker.lifetime`(5초)보다 충분히 짧아야 마커가 깜빡이지 않습니다 |

> **이벤트 목록과 마커 발행에 대해.** `detected_events_db`는 이력을 남기려고
> 일부러 오래 유지합니다. 다만 예전에는 상한이 없었고, 마커 발행도 탐지가 처리될
> 때마다 일어났습니다. `/safety_status`는 젯슨 프레임마다(최대 30Hz) 오므로
> **초당 30번씩 누적된 모든 이벤트의 마커를 새로 만들어 발행**했고, RViz에 보이는
> 그림은 그대로인데 CPU와 DDS 대역폭만 먹었습니다. 지금은 마커 발행이 고정 주기
> 타이머(`marker_publish_period`)로 분리돼 있어 탐지가 아무리 자주 와도 부하가
> 일정하고, 목록 크기는 `max_events`가 제한합니다.

**구독**

- `/camera/camera_info` (`sensor_msgs/CameraInfo`) — 첫 수신 시 `fx/fy/cx/cy`, 해상도를 실제 intrinsics로 갱신(기본값은 554.25/554.25/320/240, 640x480 하드코딩 초기값)
- `/camera/image_raw` (`sensor_msgs/Image`) — 최신 RGB 프레임 캐시
- `/camera/depth/image_raw` (`sensor_msgs/Image`) — 깊이 이미지. `32FC1`(미터)과 `16UC1`(밀리미터, 1000으로 나눠 변환) 둘 다 지원
- `/gazebo/model_states` (`gazebo_msgs/ModelStates`) — `use_mock=True`일 때만 구독. 이름에 `fire_event_target`/`helmet_violation_event_target`이 포함된 모델의 실제 좌표를 캐시
- **`/safety_status`** (`std_msgs/String`, JSON) — 젯슨(손준영)의 `ai_inference_sender.py` → `vision_inference_node.py`를 거쳐 재발행되는 실물 탐지 결과. `safety_status_callback()`이 처리

**발행**

- `/event_markers` (`visualization_msgs/MarkerArray`) — RViz 시각화용. 이벤트마다 SPHERE 마커(화재는 빨강, 그 외는 주황/노랑) + TEXT_VIEW_FACING 라벨 마커 2개, `lifetime=5초`이지만 계속 재탐지되면 매번 다시 발행되어 사실상 유지됨
- `/detected_events` (`std_msgs/String`, JSON) — `smart_factory_sim/web_dashboard.py`와 `dashboard_link/minimap_renderer.py`가 구독. mock 이벤트는 `mock_event_bridge.py`가 표준 UDP:9998 payload로도 변환해 데이터/RAG 파이프라인에 전달

**탐지 경로 3가지**

1. **Mock 탐지** (`run_mock_detection`, 5Hz 타이머, `use_mock=True`): TF로 `map→camera_frame` 변환을 구해 Gazebo 모델의 월드 좌표를 카메라 좌표계로 역투영하고, 카메라 전방(z: 0.1~4.5m) 안에 들어오면서 화면(width x height) 안에 찍히는 것만 가짜 바운딩박스로 만들어 탐지 처리. 영상 인식을 하는 게 아니라 **"화면에 보이는가"만 기하학으로 판정**하고 라벨·좌표는 Gazebo에서 그대로 받아옵니다(Gazebo 렌더링 영상으로는 화재 검출을 시험할 수 없으므로, 검출은 정답으로 대체하고 그 뒤 단계만 검증하는 구조).

   > 두 월드 모두 `libgazebo_ros_state.so`를 통해 `/gazebo/model_states`를 발행하며,
   > 화재와 안전모 미착용 표적 모델도 포함합니다. `digital_twin.launch.py`는 순찰
   > 시작점 카메라에 보이는 위치에 표적을 두어 시연 시작 직후 검증할 수 있습니다.
2. **실물 OWL-ViT 탐지** (`run_real_detection`, `use_mock=False`): `init_real_detector()`에서 `torch` + `transformers`(`Owlv2Processor`/`Owlv2ForObjectDetection`, `google/owlv2-base-patch16-ensemble`)를 로드해 `["fire", "person without helmet", "person wearing helmet"]` 쿼리로 탐지. **로드 실패 시 자동으로 `use_mock=True`로 폴백**합니다. 깊이는 중심 픽셀 주변 5x5 윈도우의 중앙값(`get_robust_depth`)을 사용.
3. **젯슨 실물 탐지** (`safety_status_callback`): `/safety_status` JSON(`{"detections":[{"object","bbox","distance_meter"},...]}`)을 파싱. `distance_meter<=0`이거나 `bbox`가 없는 항목(=젯슨이 `-1.0`으로 마스킹한 무효 탐지)은 건너뜁니다. **시뮬레이션 depth 카메라 intrinsics(fx/fy/cx/cy)가 아니라, 젯슨 카메라 고유의 `jetson_focal_length`/`jetson_frame_width`/`jetson_frame_height`로 핀홀 모델을 계산**합니다(두 카메라 렌즈가 다르므로 intrinsics를 공유할 수 없기 때문).

세 경로 모두 최종적으로 `_register_event(label, map_x, map_y, map_z)`를 호출합니다.
같은 라벨의 기존 이벤트가 1.0m 이내에 있으면 EMA(`alpha=0.2`)로 좌표를 부드럽게
갱신하고, 없으면 `{label}_{counter}` 형식의 새 이벤트로 등록합니다.

## Gazebo mock → 대시보드 · RAG 연동

두 시뮬레이션 launch는 `mock_event_bridge`를 함께 실행합니다. 이 노드는
`/detected_events`를 Jetson NanoOWL와 동일한 UDP JSON 형식으로 바꿔 데이터
파이프라인의 `:9998`로 전송합니다. 따라서 `patrol_pipeline_rag.py`는 mock
화재와 안전모 미착용을 실제 이벤트와 똑같이 `위험`으로 판정하고, DB 기록과
RAG 대응안을 생성합니다. 기본 대상은 같은 PC의 `127.0.0.1:9998`이며 다른
PC에서 파이프라인을 돌릴 때는 launch 인자를 넘기세요.

```bash
ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true \
  mock_udp_host:=203.0.113.21 mock_udp_port:=9998
```

---

## patrol_node.py

노드 이름: `patrol_node`. Nav2 `NavigateToPose` 액션으로 웨이포인트를 순환 순찰합니다.

- 구독: `/patrol_control`(`String`, JSON) — `start` / `stop` / `pause` / `resume`
- 발행: `/patrol_route`(TRANSIENT_LOCAL, 관제 화면이 경로를 그리는 용도),
  `/patrol_status`(1Hz, `state` = `idle` / `patrolling` / `paused`)

### 경로를 얻는 두 가지 방법

| 방법 | 언제 |
|---|---|
| **관제에서 받기 (권장)** | 실물 로봇. `wait_for_route:=true` 로 띄우면 경로가 올 때까지 기다리고, 관제의 '순찰 시작'이 지금 지도로 만든 경로를 실어 보냅니다 |
| 파라미터로 주기 | 시뮬레이션. `waypoints:=[x1,y1,yaw1, x2,y2,yaw2, ...]` |

`DEFAULT_WAYPOINTS` 는 **`digital_twin_1.world`(시뮬레이션) 전용**입니다. 실물
로봇이 만든 지도는 크기도 원점도 다르므로 그대로 쓰면 지도 밖이거나 벽 속이고,
그러면 Nav2 가 모든 목표를 ABORTED 로 돌려줍니다. 그런데 patrol_node 는 재시도 후
다음 지점으로 넘어가므로 **로그만 보면 순찰이 도는 것처럼 보입니다.**

### `/patrol_control` 메시지

```jsonc
{"command": "start",              // 경로를 함께 보내면 그걸로 갈아끼웁니다
 "waypoints": [{"x": 1.2, "y": 0.5, "yaw": 1.57}, ...],
 "loop": true}

{"command": "stop"}               // 진행 중 목표를 취소하고 첫 지점으로 되감기
{"command": "pause"}              // 그 자리에서 멈춤 (관제 이동 명령이 자동으로 보냄)
{"command": "resume"}             // 멈춘 지점부터 이어서
```

`start` 로 경로를 갈아끼울 때 **진행 중이던 Nav2 목표를 먼저 취소합니다.** 이걸
안 하면 새 경로를 넣어도 로봇은 옛 목표로 계속 달려갑니다 — Nav2 는 새 목표를
받기 전까지 하던 일을 계속하기 때문입니다.

### 파라미터

| 이름 | 기본값 | 용도 |
|---|---|---|
| `waypoints` | 시뮬레이션용 7지점 | x, y, yaw 를 이어붙인 평평한 목록 |
| `wait_for_route` | `false` | `true` 면 파라미터를 무시하고 관제 경로를 기다립니다 |
| `dwell_seconds` | `3.0` | 각 지점에서 감시하는 시간 |
| `retry_per_waypoint` | `2` | 같은 지점 재시도 횟수 |
| `retry_delay` | `2.0` | 재시도 간격(초) |
| `loop` | `true` | 마지막 지점 뒤 처음으로 되돌아갈지 |

### 예전 버전에서 고친 것

| 문제 | 내용 |
|---|---|
| 성공/실패를 구분하지 않음 | 지도와 월드가 어긋나 모든 목표가 ABORTED 여도 3초마다 다음 지점으로 넘어가며 정상처럼 보였음 → 상태를 판정하고 재시도, 연속 실패 시 원인을 짚어주는 경고 |
| 목표 거부 시 영구 정지 | `accepted == False` 에서 그냥 return → 노드는 살아 있는데 아무 일도 안 함 → 다음 목표를 예약 |
| 타이머 누수 | 목표마다 새 타이머를 만들고 cancel 만 함 → `destroy_timer` 로 해제 |
| `header.stamp` 미설정 | 채웁니다 |
| 취소를 실패로 셈 | 관제가 정지시킨 것도 "실패"로 세어 연속 실패 경고가 잘못 떴음 → `STATUS_CANCELED` 는 실패로 세지 않음 |

---

## patrol_planner.py

노드가 아니라 **라이브러리 모듈**입니다(ROS2 없이 numpy + OpenCV 만으로 돕니다).
SLAM 지도 한 장에서 순찰 경로를 만듭니다. `minimap_renderer.py` 의 `/patrol_plan`
과 `/patrol` start 가 이걸 부릅니다.

### 만드는 순서

1. **여유 공간** — 자유 셀에서 벽까지의 거리(`distanceTransform`)를 구하고,
   `min_clearance_m` 을 못 채우는 자리는 후보에서 뺍니다. **미탐사(-1)도 장애물로
   칩니다** — 아직 본 적 없는 곳 옆에 지점을 두면, 나중에 그 자리가 벽으로
   밝혀졌을 때 목표가 벽 속에 들어갑니다.
2. **도달 가능성** — 로봇이 있는 연결 성분만 남깁니다. 문이 닫힌 옆방이 지도에
   찍혀 있어도 그쪽은 후보가 되면 안 됩니다.
3. **구역 분할** — 미니맵이 이미 나눠 둔 구역을 그대로 받아 씁니다. 따로 나누면
   화면의 "A 구역"과 순찰의 "A 구역"이 달라져 관제자가 대조할 수 없습니다.
4. **구역별 지점** — 넓은 구역일수록 지점을 더 둡니다(`area_per_point_m2`).
   각 지점은 `여유거리 − 0.15 × 중심까지거리` 가 가장 큰 셀입니다 — 벽에서 멀되
   구석에 처박히지 않는 자리입니다.
5. **순서** — **여기가 핵심입니다.** 직선거리로 순서를 정하면 *벽을 뚫고 가는
   순서*가 나옵니다. 그래서 지도 위를 실제로 걸어서 구한 거리(측지거리)로 거리
   행렬을 만들고, 최근접 이웃 + 2-opt 로 순회 경로를 개선합니다. 거리 계산은
   0.2m 성긴 격자에서 다익스트라로 하므로 큰 지도에서도 0.4초면 끝납니다.
6. **바라볼 방향** — 각 지점의 yaw 는 다음 지점을 향합니다. 카메라가 진행 방향을
   보므로 이동 중에도 탐지가 됩니다.

### 왜 측지거리인가

빗 모양 통로에서 옆 통로의 지점은 **직선으로 4.0m 지만 실제로는 11.4m** 를
돌아가야 합니다. 직선거리로 순서를 정하면 로봇이 통로를 몇 번씩 오르내립니다.
자체 테스트 기준 실제 이동거리는 측지거리 순서 37.2m / 직선거리 순서 40.0m 입니다.

> 열린 공간 위주인 지금 지도(`smart_factory_map`)에서는 두 순서가 같은 결과를
> 냅니다. 측지거리는 **나쁜 순서가 나오지 않도록 막는** 장치이고, 벽이 많아질수록
> 차이가 커집니다. 그리고 보고되는 "한 바퀴 거리"가 실제 값이 됩니다.

### 실패할 때

지도가 아직 없거나 너무 좁으면 **예외 대신 `ok=False` 와 이유**를 돌려줍니다.
관제 화면이 계획 실패로 함께 죽으면 안 되기 때문입니다.

### 검증

`데이터 플랫폼 및 대시보드(이상민)/self_test/test_patrol_planner.py`

---


## stall_monitor.py

노드 이름: `stall_monitor`. **LiDAR 에 안 보이는 낮은 장애물**을 "못 나가는 것"
으로 알아채고 지도에 남깁니다.

### 무슨 문제인가

2D LiDAR 는 바닥에서 약 0.18m **한 높이만** 봅니다. 그보다 낮은 것은 영영 안
보입니다 — 문턱, 케이블 트레이, 파렛트 하단, 배수구 덮개, 낮은 받침대. 로봇은
앞이 비어 있다고 믿고 계속 밀어붙이고 바퀴만 헛돕니다. 범퍼가 눌리면
`auto_mapper` 가 비상정지하지만, 살짝 걸린 정도로는 범퍼도 안 눌립니다.

센서가 못 보면 **행동으로 알아내야 합니다.** 다만 "전진 명령을 주고 있는데 지도상
위치가 몇 초째 그대로다"만으로는 낮은 장애물과 OpenCR/DYNAMIXEL 미구동을 구분할 수
없습니다. 그래서 `/sensor_state`의 양쪽 엔코더도 함께 확인합니다.

### 왜 `/odom` 이 아니라 TF 인가

바퀴가 헛돌면 **오도메트리는 거짓말을 합니다.** 파렛트를 밀고 있는 동안 바퀴는
돌고 `/odom` 은 "잘 가고 있다" 고 보고합니다. 반면 `map → base_footprint` TF 는
odometry를 보조 입력으로 쓰는 Cartographer의 라이다 스캔 매칭 결과이므로,
실제로 안 움직였으면 그대로입니다.

| 신호 | 뜻 |
|---|---|
| 전진 명령 있음 + 지도 위치 그대로 + 양쪽 엔코더 변화 | 낮은 장애물/바퀴 헛돎 후보 → `/blind_obstacles`에 기록 |
| 전진 명령 있음 + 지도 위치 그대로 + 엔코더 변화 없음 | OpenCR/DYNAMIXEL 구동계 이상 → `/actuation_fault` 발행, 매퍼 안전 정지 |
| 한쪽 엔코더만 변화 또는 torque=false | 한쪽 바퀴/토크 이상 → `/actuation_fault` 발행, 매퍼 안전 정지 |

### 이 노드가 하지 않는 것

**`/cmd_vel` 을 쓰지 않습니다.** 순찰 중에는 Nav2 가, 매핑 중에는 `auto_mapper`
가 `/cmd_vel` 의 주인입니다. 여기서 또 쓰면 두 명령이 서로를 밀어내며 로봇이
갈팡질팡합니다. 이 노드는 **알아내서 알리는 일만** 합니다. 실제 바퀴가 돈 뒤
못 나간 경우만 각자가 `/blind_obstacles` 를 보고 피하고, 구동계 이상은
`/actuation_fault`로 매퍼를 멈춥니다.

| 누가 | 어떻게 피하나 |
|---|---|
| `auto_mapper` | 그 방향 점수를 `BLIND_OBSTACLE_PENALTY`(120) 만큼 깎아 다시 안 갑니다 |
| `patrol_planner` | 그 자리를 후보에서 파내 순찰 지점을 두지 않습니다 |
| Nav2 | `/keepout_filter_mask` 를 물리면 경로 계획에서 피합니다 (아래) |
| 미니맵 | 주황 X 로 표시 — 사람이 치우러 갈 수 있게 |

### 기억은 파일에 남습니다

문턱이나 케이블 트레이는 치우지 않는 한 계속 거기 있습니다. 재시작할 때마다
잊으면 매번 다시 들이받습니다. 기본 단독 실행 경로는 `~/blind_obstacles.json`이지만,
실물 매핑 실행기는 `${MAP_OUTPUT}.blind_obstacles.json`을 사용해 지도·실패 세션 간
기록이 섞이지 않게 합니다. 치운 뒤에는 기록을 비우세요.

```bash
ros2 topic pub --once /blind_obstacle_control std_msgs/msg/String   '{data: "{\"command\": \"clear\"}"}'
```

### 파라미터

| 이름 | 기본값 | 용도 |
|---|---|---|
| `window_sec` | `3.0` | 이 시간 동안 못 나가면 걸린 것으로 판정 |
| `min_cmd_speed` | `0.02` | 이 속도 이상 명령했을 때만 셉니다 |
| `min_move_m` | `0.05` | 이 거리도 못 갔으면 "안 나감" |
| `cooldown_sec` | `15.0` | 같은 자리를 도배하지 않게 |
| `front_offset_m` | `0.20` | 로봇 중심에서 이만큼 앞을 장애물로 찍음 |
| `obstacle_radius_m` | `0.20` | 표시/회피 반경 |
| `merge_distance_m` | `0.40` | 이 안의 재발견은 같은 물체로 합침 |
| `max_age_sec` | `0.0` | 0 = 영구 보관 (문턱은 안 없어지므로) |
| `store_path` | `~/blind_obstacles.json` | 기록 파일 |
| `encoder_min_delta` | `2` | 이 값보다 양쪽 엔코더가 변해야 실제 바퀴가 돈 것으로 판정 |
| `sensor_state_stale_sec` | `1.0` | 이 시간보다 `/sensor_state`가 오래되면 구동계 이상으로 안전 정지 |

### Nav2 에 물리려면 (선택)

`/keepout_filter_mask`(OccupancyGrid)와 `/costmap_filter_info` 를 함께 발행합니다.
`config/nav2_params.yaml` 의 두 코스트맵에 필터를 추가하면 경로 계획에서 피합니다.

```yaml
global_costmap:
  global_costmap:
    ros__parameters:
      filters: ["keepout_filter"]
      keepout_filter:
        plugin: "nav2_costmap_2d::KeepoutFilter"
        enabled: True
        filter_info_topic: "/costmap_filter_info"
```

### 실행

```bash
ros2 run smart_factory_sim stall_monitor
```

### 검증

`데이터 플랫폼 및 대시보드(이상민)/self_test/test_stall_monitor.py` —
특히 **헛알람을 안 내는지**를 중점적으로 봅니다. 멀쩡한 통로를 장애물로 찍으면
순찰 경로에서 그 길이 영영 빠지기 때문입니다.

---

## initial_pose_pub.py

노드 이름: `initial_pose_pub`. AMCL이 실제로 받아들일 때까지
`/initialpose`(`geometry_msgs/PoseWithCovarianceStamped`)를 **반복 발행**합니다.

**파라미터**

| 이름 | 기본값 | 용도 |
|---|---|---|
| `x` / `y` / `yaw` | `0.5` / `3.5` / `0.0` | 발행할 초기 위치 |
| `period` | `1.0` | 재발행 간격(초) |
| `max_attempts` | `30` | 이만큼 시도하고 포기 |
| `tolerance` | `0.30` | `/amcl_pose`가 이 거리 안이면 반영된 것으로 판정 |

- 공분산 대각 성분(`[0], [7], [35]`)을 `0.05`로 설정해 높은 신뢰도로 초기화
- `/amcl_pose`를 구독해 AMCL이 실제로 그 위치로 옮겨갔는지 확인하고, 확인되면
  발행을 멈추고 종료합니다
- 발행자 구독자 수(`get_subscription_count`)를 보며 로그를 남기므로, AMCL이
  아직 안 떴는지 바로 알 수 있습니다
- 두 launch 파일에서 `use_nav2:=true`일 때만 포함됩니다(`IfCondition`)

**좌표는 세 곳이 일치해야 합니다** — launch의 `START_X`/`START_Y`(= `spawn_entity`),
`config/nav2_params.yaml`의 `amcl.initial_pose`, 그리고 이 노드의 `x`/`y`.

**예전 버전에서 고친 것**

| 문제 | 증상 |
|---|---|
| `import tf_transformations` | package.xml에 없는 패키지라 미설치 시 ImportError로 즉사. launch는 멈추지 않아 트레이스백이 Gazebo/Nav2 로그에 묻혔음. 실제로 쓰던 건 `quaternion_from_euler(0,0,0)` = 상수뿐이라 제거 |
| 5초 뒤 1회 발행 후 7초에 자살 | Gazebo + Nav2는 활성화까지 5~15초가 걸려서, AMCL이 아직 구독을 열기 전이면 메시지가 사라졌음 |
| `use_sim_time` 미설정 | 타이머가 실시간 5초에 발화. 시뮬레이션이 느릴수록 더 자주 어긋남 |
| `nav2_params.yaml`은 `(0, 0)` | 두 값이 3.54m 어긋나 있었음. 지금은 `(0.5, 3.5)`로 통일 |

---

## auto_mapper.py

노드 이름: `auto_mapper`. SLAM 매핑 중 사람이 직접 로봇을 몰지 않아도 되도록,
Nav2 없이 LiDAR·Cartographer 지도·TF로 A* 웨이포인트를 추종하는 **감독형**
자율 매핑 노드입니다.
실물 로봇에서는 최상위의 `run_real_autonomous_mapping.sh`가 Cartographer 시작 ·
자율 주행 · 지도 저장을 묶어 줍니다.

- 구독: `/scan`(`LaserScan`), `/sensor_state`(`SensorState`, 범퍼), `/map`(`OccupancyGrid`)
- TF: `map → base_footprint`(Cartographer가 채움)로 현재 위치를 읽습니다
- 발행: `/cmd_vel`(`Twist`), 10Hz 타이머
- 주행: A* 경로 추적 / 원호 조향 / 큰 각도만 제자리 회전 / 차단 시 안전 복구

### 갔던 곳을 왕복하던 문제

예전 버전은 **지나온 자리를 전혀 기억하지 않는 순수 반응형 랜덤워크**였습니다.
"꼼꼼해서 다시 도는 것"이 아니라, 어디를 갔었는지 개념 자체가 없었습니다.

| 원인 | 증상 |
|---|---|
| 개활지에서 방향이 무작위 | 좌우 거리 차 < 0.15m 이거나 둘 다 사거리 밖(`inf`)이면 `random.choice`. 넓은 공장 한가운데서는 거의 항상 이 경우라 사실상 제자리 랜덤워크 |
| 회전 방향을 매번 새로 뽑음 | 1.25초 돌고 `FORWARD`로 나갔다가 다시 막히면 방향을 **다시** 선택 → 좌 → 우 → 좌 로 뒤집히며 벽 앞에서 흔들림 |
| 다 본 구역을 떠날 이유 없음 | 앞만 뚫려 있으면 계속 직진하므로 이미 훑은 통로를 몇 번이고 재통과 |

지금은 다음을 더합니다. 정지 거리·범퍼·scan 타임아웃 등 안전 조건은 항상
주행 명령보다 우선합니다.

1. **팽창 지도 A* 경로** — `/map`의 자유공간–미탐사 경계를 목표로 고르고,
   점유 장애물을 로봇 여유 반경만큼 팽창한 뒤 A* 경로를 만듭니다. 벽 건너
   목표를 직선으로 보며 돌던 대신, 경로 위 0.45m 앞 웨이포인트를 따라갑니다.
2. **방문 기억** — 지나온 자리를 굵은 격자(기본 0.5m)에 세어 두고, 이미 여러 번
   지난 방향은 점수를 깎습니다. 세는 것은 **진입 횟수**이지 체류 시간이 아닙니다 —
   0.12m/s로 주행하는 로봇에서 시간으로 세면 *처음 지나가는 칸*도 곧바로 "여러 번
   왔다"로 판정됩니다.
3. **원호 조향 + TF 회전 피드백** — 웨이포인트 오차가 70도 미만이면
   멈추지 않고 전진하며 회전속도를 조절합니다. 큰 각도만 제자리
   회전하고, 예상 시간만 믿지 않고 TF yaw로 목표 각도 도달을 확인합니다.
   회전 후에는
   전방이 안전한 한 1.2초 전진해 다시 돌기만 하는 상태를 끊습니다.
4. **회전 상한과 안전 복구** — 장애물 회전이 상한에 닿았는데도 앞이 막히면,
   후방 LiDAR가 안전할 때만 짧게 후진해 회전 공간을 만듭니다. 후방도 막혔으면
   더 돌지 않고 정지하여 사람의 확인을 요청합니다.
5. **경로 진행 감시** — 도달 가능한 프런티어를 목표로 유지하되, A* 남은 경로가
   `frontier_progress_timeout_sec` 동안 줄지 않으면 그 목표를 90초
   제외하고 다른 미탐사 경계로 전환합니다. 벽을 따라 원을 그리며 같은 목표를
   반복 시도하는 상황을 끊기 위한 장치입니다.

**지도나 TF가 아직 없으면** 미탐사/방문 판단만 빠지고 예전과 같은 순수 반응형
주행으로 동작합니다. 멈추지는 않습니다.

### 파라미터

| 이름 | 기본값 | 용도 |
|---|---|---|
| `linear_speed` | `0.12` | 전진 속도(m/s) |
| `turn_speed` | `0.40` | 실물 LDS-03 왜곡과 탐색 속도를 절충한 회전 속도(rad/s) |
| `safe_distance` | `0.35` | 전방 이 거리 미만이면 회전으로 전환 |
| `slow_distance` | `0.60` | 전방 이 거리 미만이면 감속(0.55배) |
| `front_half_angle_deg` | `35.0` | 전방 장애물로 보는 좌우 반각. 측면 구간과 겹쳐 모서리 사각을 없앰 |
| `min_valid_scan_points` | `5` | 한 스캔의 유효점이 이보다 적으면 센서 이상으로 보고 정지 |
| `turn_duration` | `0.85` | **최소** 장애물 회전 시간(초) |
| `max_turn_duration` | `3.0` | 연속 장애물 회전 상한(초). 상한에도 막히면 후진 또는 안전 정지 |
| `max_goal_turn_duration` | `8.5` | 180도 목표 회전을 허용하되 이 시간 너머로 계속 돌지 않는 상한 |
| `post_turn_forward_sec` | `1.2` | 회전 뒤 전방이 안전할 때 재회전 판단을 유예하고 전진하는 시간 |
| `backup_speed` / `backup_duration_sec` / `rear_safe_distance` | `0.06` / `0.8` / `0.35` | 회전 실패 시 후방이 안전할 때만 적용하는 제한 후진 |
| `scan_timeout` | `0.75` | `/scan`이 이보다 오래 끊기면 안전 정지 |
| `max_runtime_sec` | `0` | 0이면 무제한. 실행 스크립트가 정수 초로 전달 |
| `map_topic` / `map_frame` / `base_frame` | `/map` / `map` / `base_footprint` | 지도·좌표계 이름 |
| `probe_range` | `2.5` | 방향 점수를 볼 거리(m) |
| `probe_half_angle_deg` | `35.0` | 방향 점수 부채꼴 반각 |
| `visit_cell_size` | `0.5` | 방문 기억 격자 한 칸(m) |
| `visit_entry_min_travel_m` | `0.20` | TF가 격자 경계에서 흔들릴 때 허위 재진입을 막는 최소 이동거리 |
| `visit_cap` | `10` | 한 칸의 진입 횟수 상한(한 칸이 영구 금지되지 않게) |
| `visit_weight` | `3.5` | 방문 벌점 가중치. **0이면 방문 기억을 끕니다** |
| `turn_commit_sec` | `3.0` | 이 시간 안에는 회전 방향을 뒤집지 않음(진동 방지) |
| `revisit_threshold` | `2` | 같은 칸에 이만큼 들어왔으면 방향 재조정. 0이면 끔 |
| `redirect_cooldown_sec` | `3.0` | 재조정 최소 간격(제자리 회전 방지) |
| `redirect_margin` | `10.0` | 재조정은 직진보다 이만큼 나은 방향이 있을 때만 |
| `stuck_radius` / `stuck_timeout` | `0.5` / `12.0` | 이 반경 안에서 이 시간을 못 벗어나면 갇힘으로 보고 탈출 |
| `frontier_progress_min_m` / `frontier_progress_timeout_sec` | `0.20` / `12.0` | 프런티어 목표와 거리가 이만큼·이 시간 동안 줄지 않으면 실패로 처리 |
| `frontier_failure_radius_m` / `frontier_failure_cooldown_sec` | `1.4` / `90.0` | 실패한 목표 주변을 이 반경·시간 동안 다시 고르지 않음 |
| `frontier_heading_tolerance_deg` / `frontier_pivot_angle_deg` | `8.0` / `70.0` | 직진 허용 오차 / 원호 조향 대신 제자리 회전을 시작하는 오차 |
| `frontier_clearance_m` / `frontier_lookahead_m` | `0.18` / `0.45` | A* 장애물 팽창 여유 / 추적 웨이포인트 앞보기 거리 |
| `frontier_path_replan_sec` / `frontier_route_visit_weight` | `2.0` / `0.35` | A* 경로 갱신 주기 / 이미 지난 경로의 비용 가중치 |

### 왜 프론티어 탐사(explore_lite)를 쓰지 않는가

`config/explore_params.yaml`이 m-explore/explore_lite용으로 있고, 그쪽이 교과서적인
정답입니다. 다만 **Nav2 스택이 함께 떠 있어야 하고**(실행 스크립트는 Nav2를 끄고
돌리도록 되어 있습니다) 로봇에 패키지를 따로 설치해야 합니다. 이 노드는 그 의존성
없이 도는 것이 목적이라, 현재 알려진 `/map` 안에서만 가벼운 A* 경로를 만듭니다.
Nav2 costmap·behavior tree·복구 행동은 없으므로 넓은 공장을 빠짐없이 훑어야 한다면 explore_lite를
설치하고 Nav2와 함께 띄우는 쪽이 낫습니다.

### 예전 버전에서 고친 것 — 개활지 무한 회전

전방 콘에 유효 거리값이 하나도 없으면(= 라이다 사거리 안에 아무것도 없으면)
상태 전환 전에 `return`해버렸습니다. 그래서 `TURN` 상태에서 뚫린 방향을 보면
영영 `FORWARD`로 돌아가지 못했습니다. 20×20m 방 한가운데처럼 사방이 사거리 밖이면
**제자리에서 무한히 회전**했습니다. 지금은 유효값이 없으면 `inf`로 두어
"앞이 뚫려 있다"로 해석합니다.

### 검증

`데이터 플랫폼 및 대시보드(이상민)/self_test/test_auto_mapper.py`가 rclpy 없이
방향 판단 로직만 검증합니다(실제 주행은 로봇/시뮬레이터가 필요하므로 제외).

```bash
ros2 run smart_factory_sim auto_mapper --ros-args -p use_sim_time:=true
```

---

## step_teleop.py

노드 이름: `step_teleop`. 터미널에서 키 하나 누를 때마다 **고정된 거리/각도만큼만**
움직이는 반자동 조종 노드입니다(연속 조종이 아니라 한 스텝씩 딱딱 끊어서 이동 —
정밀한 위치 조정이나 데이터 수집에 유용).

- 발행: `cmd_vel`(`geometry_msgs/Twist`)
- 키 입력: `termios`/`tty`로 터미널을 raw 모드로 바꿔 한 글자씩 읽음(Linux 전용 방식)
- 조작키:
  | 키 | 동작 |
  |---|---|
  | `w` / `s` | 전진 / 후진 (약 0.2m, `linear_vel=0.2 m/s`) |
  | `a` / `d` | 좌회전 / 우회전 (약 28도, `angular_vel=0.5 rad/s`) |
  | `x` | 즉시 정지 |
  | `Ctrl-C` | 종료 |
- 한 스텝은 `step_duration=1.0`초 동안 `publish_rate=10`Hz로 같은 `Twist`를
  반복 발행한 뒤 자동으로 정지(`stop_robot()`)합니다.

---

## web_dashboard.py

노드 이름: `web_dashboard`. RViz 없이 브라우저로 로봇 상태를 볼 수 있게 해주는
자체 대시보드(이상민 파트의 Streamlit 대시보드와는 완전히 별개 프로젝트입니다).

> **launch 에서 자동 실행하지 않습니다.** 관제 화면은 Streamlit 대시보드로,
> 지도/탐지 목록은 `minimap_renderer.py`(:8091)로 일원화됐고, 이 노드를 부르는
> 코드는 저장소에 없습니다. 시뮬레이션 디버깅에 필요할 때만 직접 띄우세요.
> 자세한 경위는 [`../launch/README.md`](../launch/README.md) 참고.
>
> ```bash
> ros2 run smart_factory_sim web_dashboard --ros-args -p port:=8080
> ```

**파라미터**

| 이름 | 기본값 | 용도 |
|---|---|---|
| `port` | `8080` | HTTP 서버 포트 |
| `robot_frame` | `base_footprint` | 로봇 위치를 조회할 TF 프레임 |
| `map_frame` | `map` | 기준 좌표계 |
| `map_name` | `factory_map` | 표시할 지도 이름(확장자 제외). **Nav2에 넘긴 지도와 같은 값**을 launch가 넣어줍니다 — `digital_twin_map` 또는 `smart_factory_map` |

**구독**: `/camera/image_raw`(영상 프레임 캐시), `/detected_events`(id로 upsert해
`global_state['events']` 리스트 유지)

**동작 방식**: `ThreadingTCPServer` + `BaseHTTPRequestHandler`로 만든 HTTP 서버를
별도 daemon 스레드에서 돌립니다. TF 조회(`map→robot_frame`)는 5Hz 타이머로 갱신되고,
쿼터니언을 직접 `atan2`로 풀어 yaw를 계산합니다.

**엔드포인트**

| 경로 | 내용 |
|---|---|
| `/`, `/index.html` | `templates/index.html`을 그대로 서빙 (설치된 share 디렉터리 우선, 없으면 소스 트리 상대경로로 폴백) |
| `/video_feed` | MJPEG 스트림(`multipart/x-mixed-replace`, ~30fps). 프레임이 없으면 "No Video Signal" 플레이스홀더 이미지 송출 |
| `/map_image` | `maps/<map_name>.pgm`을 JPEG로 변환해 서빙 |
| `/map_info` | `maps/<map_name>.yaml`을 **직접 라인 파싱**(정식 YAML 파서 아님)해 `resolution`/`origin`만 추출. 파일을 못 찾으면 응답에 `"fallback": true`가 함께 들어옵니다 |

> 예전에는 지도 경로가 `factory_map`으로 하드코딩되어, Nav2는 A 지도로 주행하는데
> 대시보드는 B 지도를 그리는 상태였습니다. 또 파일을 못 찾으면 실제와 무관한
> `origin=[-6.0, -3.0]`을 **조용히** 반환해서 구별할 방법이 없었습니다.
| `/events` | Server-Sent Events 스트림. 접속 시 현재 상태를 즉시 1회 전송하고, 이후 포즈/이벤트 갱신마다 `threading.Event`로 깨워서 push |

---

## templates/index.html

`web_dashboard.py`가 `/`로 서빙하는 프런트엔드 페이지(637줄). `setup.py`의
`package_files('smart_factory_sim/templates')`로 `colcon build` 시 함께
패키징되도록 최근에 추가되었습니다(빠져 있으면 설치본에서 `/` 접속 시
500 에러가 납니다).
