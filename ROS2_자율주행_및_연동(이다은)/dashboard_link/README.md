# dashboard_link — 미니맵 렌더러

```
dashboard_link/
├── minimap_renderer.py   # ROS2 노드 + Flask 서버 (다은님 노트북에서 실행)
├── minimap_web.html      # 브라우저용 미니맵 페이지 (서버가 '/' 로 서빙)
└── run_minimap.sh        # 실행 래퍼
```

`minimap_renderer.py`는 **다은님 노트북**(ROS2 + SLAM이 실제로 돌아가는 PC)에서
실행됩니다. SLAM이 만든 지도 위에, 젯슨이 보낸 "탐지 방향각"만으로 물체의 실제
위치를 역산해 보여줍니다.

> **미니맵 렌더러는 이 파일 하나뿐입니다.** 예전에는 이상민 폴더에
> `edge_video/minimap_renderer.py` 사본이 있었지만, 원본과 갈라진 채로 아무
> 테스트도 거치지 않아 삭제했습니다. 레이캐스팅/트래커 로직은 여기서만 고치면 됩니다.
> `self_test/test_minimap_logic.py`가 rclpy/flask 스텁으로 **이 파일을** 직접 검증합니다.

---

## 표시 방식이 바뀌었습니다 (MJPEG → 브라우저 렌더링)

```
예전: 서버가 매 프레임 지도 전체를 JPEG로 굽고 MJPEG로 스트리밍
      → 확대도, 클릭도, 탐지 목록도 없는 정지영상
      → 프레임 제한이 없어 JPEG 인코딩이 코어 하나를 100% 점유

지금: 지도는 최초 1회만 전송(/map_data), 이후엔 좌표 JSON만(/state)
      → 브라우저가 캔버스에 그림. 대역폭이 수백 바이트/회로 줄어듦
      → 5Hz 폴링 사이를 보간해서 로봇이 부드럽게 움직임
```

기존 `/minimap_feed`(MJPEG)도 **그대로 살아 있습니다.** 대시보드를 예전 방식으로
되돌리고 싶으면 이상민 폴더에서 `MINIMAP_LEGACY_MJPEG=1`로 실행하면 됩니다.

### minimap_web.html — 브라우저 미니맵

서버가 `/`로 서빙하는 단일 HTML 파일입니다. 외부 리소스를 일절 받지 않습니다
(폐쇄망 전제 — 글꼴은 시스템 내장 스택, CSP `default-src 'self'`).

- 지도 팬(드래그) · 줌(휠) · 로봇 자동 추적
- 진행 방향 시야 콘, 이동 궤적(오래될수록 흐려짐)
- 탐지 마커 — 라벨별 색(화재 주홍 / 헬멧 미착용 황색 / 작업자 청색), 최근 탐지는 파문
- 우측 탐지 목록 — 좌표, 로봇으로부터의 거리, 확신도 막대, 경과 시간. 클릭하면 그 위치로 이동
- 레이어 토글(미탐사 영역 / 궤적 / 라벨 / 1m 격자), 축척 바, 커서 좌표 readout
- 라이트·다크 자동 대응, 키보드 단축키(`F` 전체 보기, `R` 추적, `+`/`-` 배율)
- **응답이 4초 이상 끊기면** 캔버스를 덮고 "서버 응답 없음"을 표시합니다.
  낡은 로봇 위치를 실시간처럼 읽는 일이 없도록 하기 위한 것입니다.

주소 끝에 `?demo=1`을 붙이면 ROS 없이 가짜 데이터로 화면을 볼 수 있습니다.
이때는 전면에 해저드 스트라이프와 "데모 데이터 · 실제 현장 정보 아님" 워터마크가
깔립니다. **서버에 못 닿았다고 해서 데모가 자동으로 뜨지는 않습니다** — 안전
화면에서 가짜 탐지를 실제와 헷갈리게 보여주지 않기 위한 설계입니다.

---

## 왜 depth 대신 각도(angle_offset)인가

- 젯슨은 depth를 보내지 않습니다. 바운딩박스 중심 픽셀 위치로부터 "카메라 정면
  기준 몇 도 방향에 물체가 있는지"만 계산해 UDP로 보냅니다.
- 이 노트북은 이미 갖고 있는 SLAM occupancy grid + 로봇 위치/방향을 이용해, 그
  각도 방향으로 가상의 레이를 쏴서 처음 부딪히는 장애물 위치를 거리로
  씁니다(`raycast_to_obstacle`). depth 카메라 없이도 동작하고 노이즈에 훨씬 강합니다.

### 다만 **사람은 SLAM 지도에 없습니다**

occupancy grid는 벽·설비 같은 고정 구조물만 담습니다. 사람은 지나가면 사라지므로
매핑되지 않습니다. 그래서 `raycast_to_obstacle()` 하나만 쓰면 젯슨이 `person`을
아무리 정확히 잡아도 미니맵에는 작업자가 나타나지 않았습니다.

| 상황 | 예전 동작 |
|---|---|
| 열린 통로에 선 작업자 | 레이가 아무것도 못 맞혀 `None` → 탐지가 **통째로 버려짐** |
| 벽 앞에 선 작업자 | 레이가 사람을 통과해 뒤쪽 벽에 찍힘 (몇 m 오차) |

한편 이 노드는 `extract_live_scan_obstacles()`로 "지금 LiDAR에는 보이는데 저장 지도에는
없는 물체" — 즉 그 작업자 — 를 이미 뽑고 있었습니다. 빨간 점으로 그리는 데만 쓰고 위치
추정에는 쓰지 않았을 뿐입니다. 지금은 `raycast_to_live_obstacle()`이 **그 실시간
장애물을 먼저 보고**, 없을 때만 지도 위 고정 구조물로 떨어집니다. (빨간 점 표시 자체는
그 뒤에 제거했지만, 이 계산은 작업자 마커 때문에 그대로 남아 있습니다.)

판정은 레이와의 수직 거리(`DETECTION_RAY_CORRIDOR_M`, 기본 0.45m)로 합니다. 레이
방향 앞쪽에 있고 옆으로 그 폭 이내인 것 중 **가장 가까운** 것을 고릅니다.

남는 한계: LiDAR 사거리(터틀봇 LDS 약 3.5m) 밖의 사람은 실시간 장애물로 잡히지
않으므로, 여전히 "그 방향의 벽" 근사값으로 떨어집니다.

---

## 동작 구조

1. **`MinimapNode`** (ROS2 노드, `minimap_renderer`)
   - `/map`(`nav_msgs/OccupancyGrid`) 구독 → Cartographer의 실시간 지도와 Nav2
     map_server의 저장 지도(transient-local)를 모두 받아 최신 지도 갱신
   - `/scan`(`sensor_msgs/LaserScan`, best-effort QoS) 구독 → 저장 지도에 없는 현재
     물체만 추출(지도 벽과 가까운 스캔 반환점은 제외). **화면에는 그리지 않고**,
     젯슨 탐지 각도를 지도 좌표로 옮길 때 `raycast_to_live_obstacle()`이 씁니다
   - `tf2`로 `map → base_footprint`를 10Hz로 조회해 로봇 위치/방향 갱신
     (예전엔 `/amcl_pose` 구독 방식. `_on_pose`는 쓰이지 않는 빈 함수로 남아 있음)
2. **ROS2 직접 이벤트 수신** (`/detected_events`)
   - `event_detector.py`가 Gazebo mock 또는 카메라 결과를 map 좌표로 투영해 발행
   - 좌표를 그대로 `SpatialObjectTracker`에 넣어 화면·`/detections`·DB 기록에 반영
3. **UDP 수신 스레드** (`detection_listener_thread`)
   - `{"label": "person", "angle_offset": 0.12}` JSON 수신(라디안, 오른쪽이 +)
   - 영상은 오른쪽이 `+`, ROS yaw는 왼쪽(반시계)이 `+`이므로
     `absolute_angle = robot_yaw + CAMERA_YAW_OFFSET - angle_offset` →
     ① `raycast_to_live_obstacle()`로 지도에 없는 실시간 LiDAR 장애물(사람·지게차 등)을
     먼저 찾고, ② 없으면 `raycast_to_obstacle()`로 지도 위 고정 구조물을 씁니다
     (위 "사람은 SLAM 지도에 없습니다" 참고)
4. **`SpatialObjectTracker` / `TrackedObject`**
   - 같은 라벨이라도 개체별로 따로 추적
   - `DETECTION_DIST_THRESHOLD` 이내면 EMA로 위치만 보정, 아니면 새 객체로 등록
   - `DETECTION_MIN_HITS`회 이상 검출된 것만 노출, `DETECTION_STALE_SEC` 후 삭제
5. **Flask 앱** — 아래 엔드포인트 제공

ROS2 spin / Flask / UDP 리스너가 각각 별도 스레드로 돌고 `threading.Lock`으로
공유 상태를 보호합니다.

### render_frame() 에서 고친 것 (MJPEG 쪽)

| 문제 | 내용 |
|---|---|
| 방향 표시 없음 | 초록 점만 찍혀 로봇이 어디를 보는지 알 수 없었음 → 진행 방향 선 복원 |
| `data == 100`만 장애물 | SLAM이 내놓는 중간 확률값(65, 80 등)이 미탐사 회색으로 잘못 그려졌음 → `>= 50`으로 변경 (레이캐스팅 판정과 동일한 `OCC_THRESHOLD`) |
| 프레임 제한 없음 | JPEG 인코딩이 무한 루프로 돌아 CPU를 점유 → `MINIMAP_MJPEG_FPS`(기본 5) |
| 탐지가 전부 같은 빨간 점 | 작업자·화재·안전모 미착용이 구분되지 않았음 → `detection_marker_style()`로 `minimap_web.html` 범례와 **같은 색**을 씁니다. 표기는 ASCII(`WORKER`/`FIRE`/`NO-HELMET`/`VEHICLE`) 입니다 — `cv2.putText`에 한글 글꼴이 없어 한글은 `???`로 찍힙니다. 한글 이름은 브라우저로 여는 미니맵 페이지가 보여줍니다 |

---

## 제공 엔드포인트 (`http://<이 PC의 IP>:${MINIMAP_PORT}`)

| 경로 | 내용 |
|---|---|
| `/` | **미니맵 페이지**(`minimap_web.html`). 대시보드가 iframe으로 부르는 주소 |
| `/map_data` | occupancy grid를 셀당 1바이트 base64로. `version`이 셀 내용 해시라 지도가 실제로 바뀔 때만 값이 달라지고, `?v=<현재버전>`을 보내면 내용이 같을 때 `cells`를 생략합니다 |
| `/state` | `{ready, stamp, robot:{x,y,yaw}, detections:[...], control_enabled, blind_obstacles:[...]}`. 약 5Hz로 폴링됨 |
| `/patrol_plan` | **GET.** 지금 지도로 만든 순찰 경로 미리보기(실행 안 함). `{ok, waypoints:[{x,y,yaw,zone}], zones, total_distance_m, warnings}` |
| `/patrol` | **POST.** `{"command": "start"\|"stop"\|"pause"\|"resume"}`. `start` 는 경로를 **새로 만들어 함께 보냅니다.** `/goto` 와 같은 규칙으로 `MINIMAP_TOKEN` 이 없으면 열리지 않습니다 |
| `/detections` | JSON `{"detections":[{"id","label","x","y","hit_count","last_seen"}, ...]}`. **이상민 폴더의 `edge_video/event_logger.py`가 폴링**해 DB(`detection_events`)에 적재. **형식은 그대로입니다** |
| `/minimap_feed` | MJPEG 스트림(구 방식, 호환용) |
| `/health` | `{"map_ready": bool, "pose_ready": bool}` |

`/state`의 `stamp`와 각 탐지의 `last_seen`은 **둘 다 이 서버의 시계** 기준입니다.
브라우저는 둘의 차이만 쓰므로 관제 PC와 시계가 어긋나도 경과 시간이 틀어지지
않습니다.

---

## 환경변수

`run_minimap.sh` 또는 셸에서 `export`로 덮어쓸 수 있고, 전부 기본값이 있습니다.

### 동작

| 변수 | 기본값 | 의미 |
|---|---|---|
| `MINIMAP_PORT` | `8091` | Flask HTTP 서버 포트 |
| `DETECTION_UDP_PORT` | `9091` | 젯슨의 각도 데이터를 받을 UDP 포트 |
| `CAMERA_YAW_OFFSET` | `0.0` | 카메라가 로봇 정면과 다른 방향을 볼 때의 보정값(라디안). 뒤를 보는 장착은 `3.141592653589793` |
| `DETECTION_MAX_RANGE` | `8.0` | 레이캐스팅 최대 탐색 거리(m) |
| `DETECTION_RAY_CORRIDOR_M` | `0.45` | 탐지 레이에서 이 거리(m) 안에 있는 실시간 LiDAR 장애물을 "그 방향의 물체"로 봄. 좁히면 사람을 지나쳐 뒤쪽 벽을 잡고, 넓히면 옆 물체를 잘못 집음 |
| `PATROL_AREA_PER_POINT_M2` | `9.0` | 이 면적마다 순찰 지점 하나. 줄이면 촘촘히 돌지만 한 바퀴가 길어짐 |
| `PATROL_MAX_POINTS_PER_ZONE` | `3` | 한 구역의 지점 수 상한 (넓은 구역이 경로를 독차지하는 것 방지) |
| `PATROL_MIN_CLEARANCE_M` | `MINIMAP_GOTO_CLEARANCE_M`(0.25) | 순찰 지점이 벽에서 떨어져 있어야 할 거리 |
| `PATROL_MIN_SPACING_M` | `1.5` | 이보다 가까운 두 지점은 하나로 합침 |
| `DETECTION_DIST_THRESHOLD` | `0.35` | 이 거리(m) 이내면 같은 물체로 병합 |
| `DETECTION_MIN_HITS` | `3` | 표시하기 전 최소 누적 검출 횟수 |
| `DETECTION_EMA_ALPHA` | `0.25` | 위치 보정 강도(낮을수록 부드럽게 고정) |
| `DETECTION_STALE_SEC` | `30.0` | 이 시간 재검출이 없으면 트래커에서 삭제 |
| `MINIMAP_MJPEG_FPS` | `5` | `/minimap_feed`의 프레임률 |
| `LIVE_OBSTACLE_MAP_MATCH_RADIUS_M` | `0.20` | 이 거리 안에 지도 점유 셀이 있으면 현재 scan 점을 벽/기존 설비로 보고 숨김 |
| `LIVE_OBSTACLE_CLUSTER_M` | `0.20` | 같은 실시간 물체로 묶을 LiDAR 점 간격(m) |
| `LIVE_OBSTACLE_MIN_RETURNS` | `3` | 실시간 장애물로 볼 최소 LiDAR 반환점 수 |
| `LIVE_OBSTACLE_STALE_SEC` | `1.5` | `/scan`이 끊긴 뒤 마지막 실시간 장애물 좌표를 믿는 시간(초) |

### 접근 제어

이 서버가 내보내는 것은 **공장 도면, 로봇 실시간 위치, 화재 이벤트, 작업자 위치와
헬멧 미착용 이력**입니다. 마지막 항목은 개인정보 성격이 있습니다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `MINIMAP_BIND` | `0.0.0.0` | 바인딩할 인터페이스. **운영 시에는 내부망 IP 하나만 지정하세요** |
| `MINIMAP_TOKEN` | (없음) | 설정하면 모든 요청에 `?t=<토큰>` 또는 `X-Minimap-Token` 헤더 필요 |
| `MINIMAP_FRAME_ANCESTORS` | `*` | 이 페이지를 iframe으로 감쌀 수 있는 출처 |

```bash
export MINIMAP_BIND=203.0.113.20
export MINIMAP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
```

`0.0.0.0` + 토큰 없음이면 기동 시 경고를 출력합니다. 미니맵 페이지를
`?t=<토큰>`으로 열면 그 토큰이 페이지의 API 요청에도 자동으로 이어집니다.

**CORS 헤더는 의도적으로 넣지 않습니다.** `Access-Control-Allow-Origin: *`를 켜면
관제 PC 사용자가 방문한 임의의 웹사이트가 공장 지도를 읽어갈 수 있습니다.

> `frame-ancestors`는 CSP 사양상 `<meta>`로 전달하면 **무시됩니다.** 그래서
> HTTP 헤더로 보냅니다(`MINIMAP_FRAME_ANCESTORS`). 대시보드가 다른 호스트/포트에서
> 돌기 때문에 기본값은 허용(`*`)이고, 실질적인 차단은 `MINIMAP_BIND`와
> `MINIMAP_TOKEN`이 담당합니다.

---

## 실행 방법

**사전 조건**: SLAM 또는 Nav2가 먼저 실행되어 `/map`이 발행되고 있어야 하고,
TF에 `map → base_footprint` 변환이 존재해야 합니다.

```bash
cd "ROS2_자율주행_및_연동(이다은)/dashboard_link"
./run_minimap.sh
```

### 실물 LiDAR 지도와 함께 실행하기

매핑 중에는 `real_cartographer.launch.py`가 `/map`을 만들고, 저장 뒤에는
`real_navigation.launch.py`가 저장 지도를 다시 발행합니다. 두 경우 모두 아래
미니맵 명령은 동일합니다.

```bash
# 이 PC에서 — ROS_DOMAIN_ID는 로봇과 반드시 같아야 함
export ROS_DOMAIN_ID=30
source /opt/ros/$ROS_DISTRO/setup.bash
source ~/YOUR_WS/install/setup.bash
cd "ROS2_자율주행_및_연동(이다은)/dashboard_link"
./run_minimap.sh

# 브라우저: http://<이 PC의 IP>:8091/
```

> **LiDAR 장애물 빨간 점은 제거했습니다.** 점유 지도에는 물체 종류 정보가 없어서
> 벽과 적재물을 모양(길이·두께)으로만 구분해야 했고, 그 판정이 빗나가면 벽 위에
> 빨간 점이 줄줄이 찍혔습니다. 관제 화면에는 지도 + 로봇 + 탐지 마커만 남깁니다.
>
> `/scan` 기반 실시간 장애물 계산(`extract_live_scan_obstacles`)은 **그대로
> 남아 있습니다.** 화면 표시용이 아니라, 사람이 SLAM 지도에 없기 때문에 작업자
> 마커를 찍을 자리를 정하는 데 필요합니다(위 "사람은 SLAM 지도에 없습니다" 참고).

**필요 pip 패키지**: `rclpy`(ROS2 환경에 포함), `flask`, `opencv-python`, `numpy`.

```bash
pip install flask opencv-python numpy
```

### ROS 없이 화면만 확인하기

브라우저에서 `minimap_web.html`을 직접 열고 `?demo=1`을 붙이거나, 이상민 폴더의
가짜 서버를 띄우면 됩니다.

```bash
python self_test/fake_minimap_server.py    # 이상민 폴더에서
# http://127.0.0.1:8091/
```

전체 파이프라인에서 이 렌더러를 언제 띄워야 하는지는
[`../../README.md`](../../README.md)를 참고하세요.

## 순찰 구역 자동 분할

구역 좌표를 손으로 지정해 두지 않았으므로, SLAM 지도가 들어오면 자유 공간을
면적 기준으로 자동 분할합니다(`compute_zones`). k-means 를 셀 좌표에 돌리기
때문에 벽 모양을 따라 덩어리지고, 격자로 자를 때처럼 한 구역이 두 방에 걸치지
않습니다. `cv2.setRNGSeed(0)` 과 열 단위 정렬로 실행할 때마다 A/C 가 뒤바뀌는
것을 막았습니다.

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `MINIMAP_ZONE_COUNT` | `3` | 나눌 구역 개수 |
| `MINIMAP_ZONE_NAMES` | `A,B,C,D,E,F` | 구역 이름 (왼쪽 열부터 위→아래 순) |
| `MINIMAP_ZONE_SAMPLE_STRIDE` | `3` | k-means 표본 간격 (클수록 빠름) |

- `GET /zones` — 구역 라스터(base64)와 이름·중심·면적. 지도처럼 버전이 같으면 셀을 생략합니다.
- `GET /current_zone` — 로봇이 지금 있는 구역 이름만. 파이프라인의 `--zone auto` 가 이걸 폴링합니다.

> 이 구역은 **표시와 적재 양쪽에 쓰입니다.** `--zone auto` 를 주지 않으면
> `patrol_logs.zone` 은 여전히 실행 인자로 고정됩니다.

## 로봇 제어 (디지털 트윈)

- `POST /goto` — `{x, y, stop_patrol}`. 지도를 더블클릭하면 호출됩니다. 벽·미탐사·지도 밖이면 거부하고 이유를 돌려줍니다.
- `POST /patrol` — `{command: pause|resume}`.

> **이 두 엔드포인트는 `MINIMAP_TOKEN` 이 설정돼 있어야만 동작합니다.**
> 나머지는 읽기 전용이라 토큰이 선택이지만, 여기는 실물 로봇을 움직입니다.
> 토큰 없이 열어두면 같은 망의 누구나 공장 로봇을 조종할 수 있습니다.
> `MINIMAP_ALLOW_GOTO=0` 으로 완전히 끌 수 있습니다.

## 로봇 상태

`/state` 의 `robot_status` 에 배터리(`/battery_state`)·속도(`/odom`)·순찰
진행(`/patrol_status`)이 실립니다. 각 항목에 `*_age_sec` 이 함께 오고,
`MINIMAP_STATUS_STALE_SEC`(기본 10초)를 넘으면 값을 감춥니다 — 배터리처럼
천천히 변하는 값은 멈춰 있어도 그럴듯해 보이기 때문입니다.

터틀봇이 배터리를 안 달았거나 시뮬레이션이면 `/battery_state` 자체가 오지
않습니다. 그때 `battery` 는 `null` 이며, 대시보드는 이를 `0%` 가 아니라
"연결 안 됨" 으로 표시합니다.
