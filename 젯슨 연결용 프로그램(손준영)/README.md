# 젯슨 연결용 프로그램 (손준영)

한이음 멘토링 프로젝트 중 손준영 담당 파트(Jetson/Docker에서 도는 NanoOWL 비전
추론, 그리고 그 결과를 ROS2로 넘겨주는 브릿지 노드)의 코드입니다.

전체 시스템에서 이 파트가 어디에 붙는지, 다른 파트와 어떤 순서로 실행해야 하는지는
루트 [`../../README.md`](../README.md)의 "전체 데이터 흐름" / "실행 순서" 절을
참고하세요. 이 문서는 이 폴더 안 코드 자체에 집중합니다.

## 폴더 구조

```
젯슨 연결용 프로그램(손준영)/
├── ai_inference_sender.py       # Jetson/Docker에서 도는 실제 추론 스크립트 (NanoOWL + StereoSGBM)
├── stereo_calibrate.py          # 스테레오 캘리브레이션 (촬영 → 계산 → 검증). 거리 정확도의 근거
├── stereo_calibration.npz       # 위 결과. 있으면 sender가 자동으로 읽습니다 (없으면 추측값 + 경고)
├── vision_inference_node.py     # ROS2 브릿지 노드 (UDP 9999 -> /safety_status)
├── requirements.txt             # pipeline/jetson_patrol_pipeline.py만 돌릴 때 쓰는 최소 의존성
├── requirements-vision.txt      # ai_inference_sender.py 실행에 필요한 pip 의존성
├── 손준영팀장님_실행안내(new).md  # 손준영 팀장님께 보내는 실행 안내 편지 (건드리지 않음)
└── jetson용_smart_factory_project_1/
    └── smart_factory_project/   # 과거 zip 제작용 뼈대(새 배포에는 사용하지 않음)
```

> 최신 Autopilot Jetson 배포 정본은 저장소 루트의
> [`../smart_factory_project/`](../smart_factory_project/)입니다. 이 폴더의 Python
> 파일은 개발 원본이고, `jetson용_smart_factory_project_1/`은 과거 안내 호환용입니다.

## 이 파트의 역할

Jetson(또는 Jetson 위 Docker 컨테이너)에 연결된 스테레오 카메라 영상에서
`ai_inference_sender.py`가

1. **NanoOWL(VLM, TensorRT 엔진)** 으로
   `["person", "safety helmet", "fire", "vehicle"]`를 탐지한 뒤,
   안전모가 4초 연속 보이지 않은 사람을 `person with no helmet`으로 판정하고,
2. **StereoSGBM** 으로 좌/우 영상 간 시차를 계산해 탐지된 객체까지의 거리를 구한 뒤,
3. 결과를 **MJPEG 영상 스트림**과 **UDP JSON**으로 실시간 송출합니다.

이 스크립트 하나가 이후 파이프라인 3곳(ROS2 브릿지, 데이터 플랫폼, 미니맵 렌더러)의
유일한 데이터 출처입니다. `vision_inference_node.py`는 그중 ROS2 쪽 수신만 담당하는
아주 얇은 브릿지입니다.

### `ai_inference_sender.py`

- **카메라 입력**: `/dev/video0`, 요청 해상도 2560x720 (좌 1280x720 + 우 1280x720 통합).
  `frame[:, 0:mid]`를 좌안, `frame[:, mid:w]`를 우안으로 분리해 사용합니다.
- **VLM 추론**: `/opt/nanoowl/data/owl_image_encoder_patch32.engine` TensorRT 엔진을
  로드한 `OwlPredictor`로 좌안 영상만 추론합니다(threshold=0.1).
- **안전모 시간 판정** (`HelmetComplianceTracker`): 부정문을 직접 탐지하지 않고
  `person`과 긍정 객체인 `safety helmet`을 각각 탐지합니다. 안전모 중심이 사람
  박스 상단 40%에 있으면 착용으로 보고, 4초 연속 안전모가 보이지 않을 때만
  `person with no helmet`으로 전환합니다. 안전모가 다시 보이면 즉시 `person`으로
  복귀하며, 판정용 `safety helmet` 박스 자체는 화면·UDP에 출력하지 않습니다.
- **겹친 탐지 정리** (`suppress_overlapping_detections`): 같은 클래스의 박스만
  IoU 0.5 이상일 때 하나로 줄입니다. `person` 안의 `safety helmet`은 정상적인
  중첩이므로 서로 지우지 않습니다.
- **Depth 계산**: `cv2.StereoSGBM_create(minDisparity=0, numDisparities=80, blockSize=5)`로
  좌/우 그레이스케일 이미지의 시차(disparity)를 구하고 `Z = f × B / d`로 거리를
  환산합니다. `f`(초점거리)와 `B`(렌즈 간격)는 **`stereo_calibration.npz`가 있으면
  실측값을, 없으면 예전 추측값(500.0 / 0.06)을** 씁니다 — 아래 캘리브레이션 절 참고.
  - 시차를 구하지 못한 픽셀(`disparity <= 0`)은 **-1.0으로 마스킹**합니다. 예전에는
    0.1로 대체해서 300m 근방에 해당하는 가짜 근접 거리값이 박스 내 median 계산에
    섞여 들어가는 버그가 있었는데(자세한 경위는
    [`데이터 플랫폼 및 대시보드(이상민)/docs/판정정책_갱신제안.md`](../데이터%20플랫폼%20및%20대시보드(이상민)/docs/판정정책_갱신제안.md)
    5.4절 참고), 지금은 고쳐진 상태입니다.
- **좌우 정렬(rectification)**: 캘리브레이션이 있으면 **캡처 스레드에서** 좌우
  영상을 정렬한 뒤 내보냅니다. 추론 직전이 아니라 캡처 단계에서 하는 이유는,
  추론·거리·박스·스트리밍이 모두 같은 좌표계를 보게 하기 위해서입니다(추론
  직전에 정렬하면 화면의 박스가 물체에서 어긋납니다).
- **영상 스트리밍**: `:8500`에서 MJPEG 이중 스트림 제공.
  - `/` — 탐지 박스가 그려진 RGB 영상
  - (`/depth` 컬러맵 스트림은 제거했습니다 — 관제 화면에는 카메라 영상과 SLAM 미니맵만 씁니다. 거리 계산은 그대로 동작합니다.)
  - `/health` — 추론 루프 상태(JSON). 아래 참고
- **`/health` — 영상만으로는 살아있는지 알 수 없습니다.**
  카메라 케이블이 빠지거나 추론 루프가 멈춰도 **마지막 프레임이 계속 재전송되어
  화면은 멀쩡해 보입니다.** `frame_age_sec`이 계속 커지면 추론이 멈춘 것입니다.

  ```json
  {"ok": true, "uptime_sec": 312.4, "frames": 9021, "frame_age_sec": 0.03,
   "video_frames": 41230, "video_age_sec": 0.03,
   "camera_errors": 0, "last_detection_count": 2, "udp_errors": 0}
  ```

  `frames`/`frame_age_sec` 은 **추론**, `video_frames`/`video_age_sec` 은 **카메라 캡처** 기준입니다. 둘은 별도 스레드라 따로 멈출 수 있습니다 — `video_age_sec` 만 커지면 화면이 정지한 것이고, `frame_age_sec` 만 커지면 영상은 흐르는데 박스만 굳은 것입니다. `ok` 는 추론 기준입니다. `camera_errors`가 늘어나면
  USB 연결을, `udp_errors`가 늘어나면 목적지 IP를 확인하세요.
- **UDP 전송 — 같은 탐지 결과를 세 포트에 동시 전송**합니다. **세 포트의 대상은
  서로 다른 기기이므로 IP 변수가 각각 따로 있습니다.**

  | 포트 | 대상 IP (환경변수) | 도는 곳 | 용도 | 페이로드 형식 |
  |---|---|---|---|---|
  | `9999` | `ROS2_BRIDGE_IP` | 젯슨 호스트 | ROS2 브릿지용 (`vision_inference_node.py`) | `{"timestamp": ..., "detections": [{"object", "bbox", "distance_meter"}, ...]}` |
  | `9998` | `DASHBOARD_IP` | 이상민님 PC | 대시보드/DB용 (`patrol_pipeline_rag.py` 등) | 위와 동일 |
  | `9091` | `DAEUN_LAPTOP_IP` (포트는 `DETECTION_UDP_PORT`) | 이다은님 노트북 | 미니맵 각도용 (`minimap_renderer.py`) | `{"label": ..., "angle_offset": ...}` (탐지 객체 1개당 1건씩 전송) |

  `9091`로 보내는 `angle_offset`은 depth 대신 "카메라 정면 기준 몇 도 방향인지"만
  전달합니다. bbox 중심 픽셀(`box_center_x`)과 화면 중심(`frame_center_x`)의 차이를
  `focal_length`로 나눠 `math.atan2`로 라디안 각도를 구합니다(오른쪽이 +, 왼쪽이 -).
  미니맵 렌더러가 SLAM 지도 위에 레이캐스팅할 때 이 각도만 사용하고 depth 값은 쓰지
  않습니다 — depth 자체가 아직 불안정하기 때문입니다.

### `stereo_calibrate.py` — 거리를 믿을 수 있게 만드는 단계

거리는 `Z = f × B / d` 로 나옵니다. `f`(초점거리, 픽셀)와 `B`(두 렌즈 간격)를
코드에 상수로 박아 두면 **그 값이 틀린 만큼 모든 거리가 그대로 틀립니다.**
예전에는 `f=500.0` / `B=0.06` 이 박혀 있었는데 둘 다 실측이 아니라 추측값이었습니다.

이게 왜 문제냐면, 같은 `f` 가 **두 곳**에 쓰이기 때문입니다.

```python
depth_map[valid] = (focal_length * baseline) / disparity[valid]     # 거리
angle_offset = math.atan2(box_center_x - principal_x, focal_length)  # 미니맵 방향각
```

- 거리가 틀리면 `common/schema.py` 의 1.5m 위험 / 3.0m 주의 기준이 그만큼 밀립니다.
  (실제 `f` 가 900인데 500을 쓰면 2.0m 에 선 사람이 **1.11m** 로 읽혀, "주의" 여야
  할 상황이 "위험" 으로 올라갑니다.)
- 방향각이 틀리면 미니맵의 작업자 마커가 엉뚱한 각도에 찍힙니다.

그리고 더 중요한 것이 **rectification(정렬)** 입니다. `cv2.StereoSGBM` 은 왼쪽
영상 N번째 줄의 점을 오른쪽 영상 **같은 N번째 줄에서만** 찾습니다. 실제 모듈은
조립 공차로 1~2° 씩 틀어져 있어서, 진짜 대응점이 302번째 줄에 있으면 못 찾습니다.
→ 시차가 안 잡혀 `distance_meter` 가 `-1` 로 자주 나옵니다.

#### 하는 법 (젯슨에서, 디스플레이 없이 됩니다)

체스보드를 인쇄해 단단한 판에 붙입니다. 기본값은 **10×7 칸 = 내부 코너 9×6**,
한 칸 25mm. 다르면 `--cols/--rows/--square` 로 알려주세요.

```bash
# 1) 20쌍 자동 수집. 좌우 **양쪽**에 체스보드가 다 보일 때만 저장됩니다.
python3 stereo_calibrate.py capture --count 20

# 2) 계산 (RMS 1.0px 넘으면 다시 촬영)
python3 stereo_calibrate.py calibrate

# 3) 검증 — 정렬 후 평균 수직 어긋남이 0.5px 아래면 성공
python3 stereo_calibrate.py verify

# 4) 결과 확인
python3 stereo_calibrate.py report
```

#### 촬영이 제일 중요합니다 — 기울기와 위치를 **따로** 섞으세요

- 가까이(0.5m)부터 멀리(1.5m)까지 거리를 바꿔 가며
- 화면 가운데뿐 아니라 **네 귀퉁이**에도 체스보드를 두고
- 정면뿐 아니라 **상하좌우로 20~30도씩** 기울여서

여기서 가장 중요한 것은 **기울기와 화면 위치가 서로 상관되지 않게** 하는 것입니다.
"왼쪽에 둘 때는 항상 왼쪽으로 기울인다" 같은 습관이 생기면, 최적화가 초점거리와
주점을 서로 상쇄시키는 해를 찾아냅니다. 합성 데이터로 실제 확인한 결과입니다.

| 촬영 세트 | 스테레오 RMS | 복원한 f (정답 900px) | 복원한 cx (정답 640px) |
|---|---|---|---|
| 기울기와 위치가 상관됨 | 0.76 px (낮음!) | **1388 px (+54%)** | 367 px |
| 기울기와 위치가 독립 | 0.14 px | 911 px (+1.2%) | 638 px |

**RMS 만으로는 이걸 못 거릅니다.** 위쪽도 RMS 는 1px 아래라 통과합니다. 그래서
`calibrate` 는 주점이 화면 중심에서 10% 넘게 벗어나면 따로 경고합니다 — 이 경고가
뜨면 RMS 가 좋아도 다시 촬영하세요.

`stereo_calibration.npz` 가 만들어지면 `ai_inference_sender.py` 가 **자동으로**
읽어 씁니다. 시작 로그에 실측 `f` / `B` / 주점이 찍히므로 적용됐는지 바로 압니다.
파일이 없으면 예전처럼 추측값으로 돌되 경고를 냅니다.

> **`event_detector.py` 에도 같은 `f` 를 알려줘야 합니다.** 그쪽은 거리를 지도
> 좌표로 투영할 때 `jetson_focal_length` 를 씁니다. `report` 가 복사해 붙일 수 있는
> `ros2 run ... --ros-args -p jetson_focal_length:=...` 한 줄을 출력합니다.

#### 캘리브레이션 없이 급하게 맞추기

체스보드가 없으면, 줄자로 정확히 2.0m 떨어진 곳에 사람을 세우고 `distance_meter`
가 뭐라고 나오는지 보세요. 1.1m 로 나오면 `f` 가 약 1.8배 작다는 뜻입니다.

```bash
export STEREO_FOCAL_PX=900        # 500 × (2.0 / 1.1)
export STEREO_BASELINE_M=0.06     # 자로 잰 두 렌즈 중심 간격
```

정렬은 여전히 안 되므로 임시방편입니다. 제대로 하려면 위 캘리브레이션을 하세요.

---

### `vision_inference_node.py`

- `127.0.0.1:9999`(기본값)를 **non-blocking UDP 소켓**으로 100Hz(0.01초 타이머)
  폴링합니다. 주소/포트는 `ROS2_BRIDGE_BIND_IP` / `ROS2_UDP_PORT` 환경변수 또는
  ROS2 파라미터(`-p bind_ip:=`, `-p udp_port:=`)로 바꿀 수 있습니다.
- **한 콜백에서 수신 큐를 끝까지 비웁니다**(최대 `MAX_PACKETS_PER_CALLBACK`=50건).
  콜백당 한 건만 읽으면 executor가 순간적으로 밀렸을 때 커널 UDP 버퍼가 넘쳐
  최신 패킷이 버려지기 때문입니다.
- 받은 바이트를 UTF-8 디코딩 + JSON 파싱으로 **검증한 뒤** `std_msgs/String`으로
  감싸 `/safety_status`에 발행합니다. 내용을 가공하지는 않지만, 깨진 패킷은
  폐기하고 카운트만 합니다 — 검증이 없으면 디코딩 실패가 타이머 콜백에서 예외로
  터져 노드 자체가 죽고, 깨진 JSON이 하류까지 그대로 흘러갑니다.
- 로그는 매 패킷이 아니라 **2초마다 요약**(발행 건수 / 폐기 건수)만 출력합니다.
  매 패킷 로그는 I/O가 콜백을 블로킹해서 수신을 밀리게 만듭니다.
- **반드시 `ai_inference_sender.py`와 같은 호스트에서 실행하세요.** 기본 바인드
  주소가 루프백(`127.0.0.1`)이라 다른 장비에서 오는 패킷은 받지 못합니다.
- 의존성은 `rclpy`뿐입니다. NanoOWL/torch/opencv 같은 무거운 패키지는 필요 없으므로
  `requirements-vision.txt`가 아니라 **ROS2(rclpy)가 설치된 환경**에서 실행하면 됩니다.
- `/safety_status`는 이다은 파트의
  [`ROS2_자율주행_및_연동(이다은)/smart_factory_sim/event_detector.py`](../ROS2_자율주행_및_연동(이다은)/smart_factory_sim/event_detector.py)의
  `safety_status_callback()`이 구독합니다. 이 콜백은 `jetson_focal_length=500.0`,
  `jetson_frame_width=1280.0`, `jetson_frame_height=720.0`이라는 카메라 고유 값으로
  픽셀+거리를 지도 좌표로 역산하는데, **이 세 값은 `ai_inference_sender.py`의 실제
  카메라 설정(좌안 1280x720, focal_length 500.0)과 반드시 일치해야 합니다.** 카메라를
  교체하거나 focal_length를 조정하면 양쪽 다 같이 고쳐야 합니다.

## 의존성 — 두 requirements 파일은 용도가 다릅니다

이 폴더에는 `requirements.txt`와 `requirements-vision.txt` 두 개가 있는데, **서로
다른 스크립트를 위한 것**이라 헷갈리지 않도록 주의하세요.

| 파일 | 대상 스크립트 | 설치 대상 | 내용 |
|---|---|---|---|
| `requirements-vision.txt` | `ai_inference_sender.py` | Jetson / Docker 컨테이너 | `opencv-python`, `numpy`, `Pillow`. `torch`/`nanoowl`은 Jetson L4T 컨테이너에 이미 포함되어 있다는 전제로 주석 처리되어 있습니다(일반 pip 배포판이 아닌 NVIDIA 전용 빌드가 필요). |
| `requirements.txt` | `pipeline/jetson_patrol_pipeline.py` (이상민 파트, Jetson에서 파이프라인만 단독 실행할 때) | Jetson | `psycopg2-binary` 하나뿐인 최소 의존성. |

`vision_inference_node.py`는 둘 중 어디에도 없습니다 — pip이 아니라 ROS2(rclpy)
환경이 필요하다고 `requirements-vision.txt` 안에 안내 주석으로만 남겨져 있습니다.

## 실행 방법

받는 쪽(ROS2 브릿지, 데이터 플랫폼 파이프라인)을 먼저 띄운 뒤 `ai_inference_sender.py`를
가장 마지막에 실행하세요. UDP는 리스너가 먼저 떠 있어야 패킷을 놓치지 않습니다.
전체 순서는 루트 README 2단계를 참고하세요.

```bash
# 0) (최초 1회) 환경변수 준비
cd ~/smart_factory_project
cp set_env.example.sh set_env.sh
nano set_env.sh          # DASHBOARD_IP, DAEUN_LAPTOP_IP 등을 실제 값으로
source set_env.sh

# 1) ROS2 브릿지 (ai_inference_sender.py와 같은 호스트에서)
python3 vision_inference_node.py

# 2) (최초 1회, 카메라를 바꿨다면 다시) 스테레오 캘리브레이션
#    이걸 건너뛰면 거리와 미니맵 방향각이 추측값으로 돌아갑니다. 위 절 참고.
python3 stereo_calibrate.py capture --count 20
python3 stereo_calibrate.py calibrate
python3 stereo_calibrate.py verify

# 3) (다른 터미널) 비전 추론 — Jetson/Docker, NanoOWL/torch 사전 설치 환경
python3 ai_inference_sender.py
```

확인: `http://<Jetson IP>:8500` (RGB) · `/health` (추론 상태 JSON).

시작 로그에서 캘리브레이션이 적용됐는지 확인하세요.

```
🎯 [캘리브레이션] 적용됨 — 좌우 영상을 정렬(rectify)합니다
     초점거리 f : 912.4 px (수평 화각 약 70도)
     렌즈 간격 B: 60.2 mm
```

`[주의] 스테레오 캘리브레이션이 없습니다` 가 뜨면 거리가 추측값입니다.

### ⚠ Docker 로 돌릴 때 — 컨테이너의 `127.0.0.1` 은 호스트가 아닙니다

`ai_inference_sender.py`는 보통 Jetson **위의 Docker 컨테이너 안**에서 돕니다
(NanoOWL/torch가 L4T 컨테이너에만 있으므로). 그런데 컨테이너 안에서 `127.0.0.1`은
호스트가 아니라 **컨테이너 자기 자신**입니다. 기본값 그대로 두면 호스트에서 도는
`vision_inference_node.py`는 아무것도 받지 못하는데, **UDP라 에러도 나지 않아
조용히 끊깁니다.** 영상 포트(8500)도 매핑하지 않으면 대시보드가 붙지 못합니다.

가장 간단한 해결은 호스트 네트워크를 쓰는 것입니다.

```bash
docker run --rm -it --runtime nvidia --network host \
  --device /dev/video0 \
  -v $(pwd):/work -w /work \
  <nanoowl 이미지> python3 ai_inference_sender.py
```

`--network host`를 못 쓰면 호스트 IP를 직접 주고 포트를 매핑해야 합니다.

```bash
docker run --rm -it --runtime nvidia \
  --device /dev/video0 -p 8500:8500 \
  -e ROS2_BRIDGE_IP=172.17.0.1 \
  -e DASHBOARD_IP=192.168.0.xxx \
  <nanoowl 이미지> python3 ai_inference_sender.py
```

컨테이너 안에서 목적지가 루프백이면 **시작할 때 경고가 출력됩니다.**
호스트 쪽 `vision_inference_node.py`도 루프백에만 바인딩되어 있으면 경고합니다
(그 경우 `ROS2_BRIDGE_BIND_IP=0.0.0.0`).

## 환경변수

| 변수 | 기본값 | 읽는 스크립트 | 의미 |
|---|---|---|---|
| `ROS2_BRIDGE_IP` | `HOST_IP` → `127.0.0.1` | sender | 탐지 결과(9999)를 보낼 대상 IP — `vision_inference_node.py`가 도는 **젯슨 호스트**. |
| `DASHBOARD_IP` | `HOST_IP` → `127.0.0.1` | sender | 탐지 결과(9998)를 보낼 대상 IP — 파이프라인이 도는 **이상민님 PC**. |
| `HOST_IP` | `127.0.0.1` | sender | 위 두 변수의 공통 기본값(하위 호환). 아래 주의 참고. |
| `DAEUN_LAPTOP_IP` | `203.0.113.20` | sender | 미니맵 각도 데이터(9091)를 보낼 대상 IP — 이다은님 노트북. |
| `ROS2_UDP_PORT` | `9999` | **sender + node** | 젯슨 → ROS2 브릿지 포트. 양쪽이 같은 변수를 읽습니다. |
| `ROS2_BRIDGE_BIND_IP` | `127.0.0.1` | node | 브릿지가 UDP를 수신할 주소. 송신 측이 `--network host`가 아닌 컨테이너면 `0.0.0.0`이 필요합니다. |
| `DASHBOARD_UDP_PORT` | `9998` | sender | 대시보드/DB 파이프라인으로 보낼 포트. |
| `DETECTION_UDP_PORT` | `9091` | sender | 미니맵 각도 데이터를 보낼 포트. |
| `JETSON_VIDEO_PORT` | `8500` | sender | MJPEG 스트리밍 서버 포트. |
| `STEREO_CALIBRATION_FILE` | 스크립트 옆 `stereo_calibration.npz` | sender + calibrate | 캘리브레이션 결과 파일 위치. |
| `STEREO_FOCAL_PX` | `500.0` | sender | **캘리브레이션이 없을 때만** 쓰는 초점거리(px). 있으면 실측값이 이깁니다. |
| `STEREO_BASELINE_M` | `0.06` | sender | **캘리브레이션이 없을 때만** 쓰는 렌즈 간격(m). |
| `PPE_NO_HELMET_CONFIRM_SEC` | `4.0` | sender | 안전모가 연속으로 안 보여야 미착용으로 확정하는 시간(초). |
| `PPE_HEAD_REGION_RATIO` | `0.40` | sender | 사람 박스 상단에서 안전모를 찾는 영역 비율. |
| `DEDUP_IOU_THRESHOLD` | `0.5` | sender | 같은 물체로 볼 박스 겹침 기준. |

**포트는 짝을 이루는 양쪽이 같은 환경변수를 읽습니다.** `ROS2_UDP_PORT`를 바꾸면
송신(`ai_inference_sender.py`)과 수신(`vision_inference_node.py`)이 함께 따라가므로,
한쪽에만 하드코딩된 값을 고쳐 조용히 데이터가 끊기는 사고를 막을 수 있습니다.

### ⚠ `HOST_IP` 하나로는 9999와 9998을 동시에 맞출 수 없습니다

예전에는 두 포트가 `HOST_IP` 하나를 함께 썼는데, 이 둘은 애초에 다른 기기입니다.

```
:9999 → vision_inference_node.py   젯슨 "호스트" (이 컨테이너 바깥)
:9998 → patrol_pipeline_rag.py     이상민님 PC
```

`HOST_IP`를 젯슨 호스트로 맞추면 대시보드 파이프라인이 아무것도 못 받고, 이상민님
PC로 맞추면 ROS2 브릿지가 죽습니다. **둘 다 만족시킬 값이 없었습니다.** UDP는
목적지가 없어도 에러를 주지 않으므로 증상이 "그냥 조용함"으로만 나타납니다.

지금은 목적지마다 변수가 따로 있고, `HOST_IP`는 둘의 공통 기본값으로만 남아
기존 설정이 깨지지 않게 합니다. 실제 연동 시에는 이렇게 나눠 주세요.

```bash
export ROS2_BRIDGE_IP=127.0.0.1        # 브릿지는 이 젯슨의 호스트에서 돎
export DASHBOARD_IP=192.168.0.xxx      # 이상민님 PC
export DAEUN_LAPTOP_IP=192.168.0.yyy   # 이다은님 노트북
```

두 목적지가 같은 IP면 시작할 때 경고가 출력됩니다.
`데이터 플랫폼 및 대시보드(이상민)/self_test/test_integration_wiring.py`가
이 배선을 자동으로 검사합니다.

```bash
# 예: 포트를 바꿔서 두 스크립트를 함께 띄우는 경우
export ROS2_UDP_PORT=19999
python3 vision_inference_node.py     # 19999에서 수신
python3 ai_inference_sender.py       # 19999로 송신
```

`focal_length`(500.0)는 코드에 하드코딩되어 있어 환경변수가 아닙니다 — 값을 바꾸면
`event_detector.py`의 `jetson_focal_length`도 같이 수정해야 합니다.

## 관련 문서

- [`손준영팀장님_실행안내(new).md`](손준영팀장님_실행안내(new).md) — Jetson 실물 장비에서
  이상민 파트 파이프라인과 함께 테스트할 때의 단계별 실행 안내(손준영 팀장님용 편지 형식).
- [`../smart_factory_project/README.md`](../smart_factory_project/README.md) — Autopilot
  기준 Jetson 실행 파일, PC 전용 호환 파일, 시작 순서를 정리한 최신 배포 문서.
- [`jetson용_smart_factory_project_1/smart_factory_project/README.md`](jetson용_smart_factory_project_1/smart_factory_project/README.md) —
  과거 zip 제작용 뼈대가 남아 있는 이유와 이전 안내.
- [`../README.md`](../README.md) — 전체 파이프라인 통합 실행 순서.
