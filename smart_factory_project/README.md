# smart_factory_project — Autopilot Jetson 배포본

이 폴더는 실물 로봇 Autopilot 운용 시 Jetson으로 배포하는 **정본 번들**입니다.
저장소 안의 개발 원본을 실제 Jetson 경로 `~/smart_factory_project/`에 맞춰 모아 둡니다.

여기서 Autopilot은 다음 분담을 뜻합니다.

- Jetson: TurtleBot3 하드웨어, 카메라/NanoOWL 추론, ROS2 탐지 브리지, 현장 TTS 수신
- ROS2 관제 PC: Nav2/AMCL, 순찰 노드, 미니맵
- 데이터 PC: PostgreSQL 적재, RAG 대응안, Streamlit 대시보드, TTS 송신

## Jetson에서 실제로 사용하는 파일

```text
~/smart_factory_project/
├── ai_inference_sender.py       # [상시/Docker] 카메라·NanoOWL·거리·영상·UDP 송신
├── vision_inference_node.py     # [상시/호스트 ROS2] UDP :9999 → /safety_status
├── stereo_calibrate.py          # [최초/카메라 변경 시] 스테레오 보정 CLI
├── stereo_calibration.npz       # [장비에서 생성] 보정 결과, Git에는 포함하지 않음
├── requirements-vision.txt      # [최초] 비전 Python 의존성 안내
├── set_env.example.sh           # [최초] set_env.sh 생성용 템플릿
└── tts/
    ├── Rx_pipeline.py           # [선택/상시] TCP :9997 → Piper → aplay
    ├── setup_tts.sh             # [최초] Piper·모델·ALSA 점검
    ├── ko_KR-kss-medium.onnx    # [setup_tts.sh 생성] Git에는 포함하지 않음
    └── ko_KR-kss-medium.onnx.json
```

Jetson에 설치된 ROS2 패키지의
`ros2 launch turtlebot3_bringup robot.launch.py`도 상시 실행하지만, 외부 TurtleBot3
패키지 파일이므로 이 저장소 번들에는 들어있지 않습니다.

### 파일별 실행 위치

| 파일 | 위치 | 상시 실행 | 비고 |
|---|---|---:|---|
| `ai_inference_sender.py` | NanoOWL/L4T Docker | 예 | `/dev/video0`, NVIDIA runtime, 보통 `--network host` 필요 |
| `vision_inference_node.py` | Jetson 호스트 ROS2 | 예 | sender와 같은 Jetson에서 실행 |
| `tts/Rx_pipeline.py` | Jetson 호스트 | TTS 사용 시 | Piper와 ALSA 필요 |
| `stereo_calibrate.py` | 카메라에 접근 가능한 환경 | 아니요 | 최초 설치·카메라 변경 때만 실행 |
| `tts/setup_tts.sh` | Jetson 호스트 | 아니요 | 최초 설치와 장애 진단용 |
| `set_env.sh` | 각 실행 셸에서 `source` | 실행 파일 아님 | 실제 값이 든 파일은 Git에 올리지 않음 |

## Autopilot 핵심이 아닌 호환 파일

아래 파일은 통합/구버전 운용을 위해 번들에 남겨 두지만, 현재 Autopilot 분산 구성에서는
Jetson에서 실행하지 않습니다.

| 경로 | 현재 실행 위치 | 이유 |
|---|---|---|
| `pipeline/jetson_patrol_pipeline.py` | 데이터 PC | UDP :9998을 받아 PostgreSQL에 적재 |
| `pipeline/patrol_pipeline_rag.py` | 데이터 PC | DB 적재 + RAG 대응안 + TTS 송신 |
| `common/` | 데이터 PC 파이프라인 의존성 | 위험 판정과 DB 스키마 |
| `tts/Rag_to_Jetson.py` | 데이터 PC | Jetson TTS로 보내는 **송신기** |
| `requirements.txt` | Jetson 로컬 DB 파이프라인 호환 | `psycopg2`만 필요하며 Autopilot 핵심에는 불필요 |
| `run_pipeline.sh` | 구버전 로컬 DB 적재 전용 | Autopilot 전체 시작 스크립트가 아님 |

이 파일들을 물리적으로 삭제하지 않은 이유는 Jetson 한 대에서 DB 적재까지 수행하는
기존 시연 구성을 계속 지원하기 위해서입니다. 최소 배포를 만들 때는 위의 “실제로
사용하는 파일”만 복사해도 됩니다.

## 권장 시작 순서

UDP는 받는 쪽을 먼저 띄워야 초기 패킷을 놓치지 않습니다.

```bash
# 공통 준비
cd ~/smart_factory_project
cp set_env.example.sh set_env.sh       # 최초 1회
nano set_env.sh
source set_env.sh

# 터미널 1: TurtleBot3 하드웨어(외부 패키지)
source set_env.sh
ros2 launch turtlebot3_bringup robot.launch.py

# 터미널 2: ROS2 탐지 브리지
source set_env.sh
python3 vision_inference_node.py

# 터미널 3: 현장 음성 경보를 사용할 때
source set_env.sh
python3 tts/Rx_pipeline.py

# 마지막: NanoOWL 컨테이너에서 추론 송신기
docker run --rm -it --runtime nvidia --network host \
  --device /dev/video0 \
  -e ROS2_BRIDGE_IP -e DASHBOARD_IP -e DAEUN_LAPTOP_IP \
  -e ROS2_UDP_PORT -e DASHBOARD_UDP_PORT -e DETECTION_UDP_PORT \
  -e JETSON_VIDEO_PORT \
  -v "$PWD:/work" -w /work \
  <nanoowl-image> python3 ai_inference_sender.py
```

`source set_env.sh`는 호스트 셸에만 값을 넣습니다. Docker는 환경변수를 자동으로
상속하지 않으므로 위 `-e 변수명`들을 빼면 영상은 보여도 탐지 UDP가 기본 목적지로
전송되어 미니맵 마커와 RAG 대응안이 생성되지 않습니다.

ROS2 관제 PC의 Nav2/순찰 스택, 데이터 PC의 `patrol_pipeline_rag.py`, 대시보드가
먼저 준비된 뒤 마지막으로 비전 송신기를 시작하는 것이 전체 권장 순서입니다.

## 안전모 미착용 판정

NanoOWL은 `person`, `safety helmet`, `fire`, `vehicle`를 탐지합니다. 사람
박스 상단 40%에서 안전모가 4초 연속 보이지 않을 때 최종 출력을
`person with no helmet`으로 전환합니다. 유예 시간 동안은 `person`으로
표시하고, 안전모가 다시 보이면 즉시 `person`으로 복귀합니다. 판정에만
쓰는 `safety helmet` 박스는 화면과 UDP에 따로 출력하지 않습니다.

기본 4초를 바꾸어야 할 때만 Docker에
`-e PPE_NO_HELMET_CONFIRM_SEC=5.0`을 추가하세요.

## 배포 전 확인

```bash
cd ~/smart_factory_project
python3 -m py_compile ai_inference_sender.py vision_inference_node.py \
  stereo_calibrate.py tts/Rx_pipeline.py
bash -n tts/setup_tts.sh

# 비전 생존 확인
curl http://127.0.0.1:8500/health

# TTS 준비 확인
bash tts/setup_tts.sh --check
```

추가 확인 항목:

- `ROS2_BRIDGE_IP`: Jetson 호스트. Docker가 `--network host`면 보통 `127.0.0.1`
- `ROS_DOMAIN_ID=30`, `ROS_LOCALHOST_ONLY=0`: ROS2 관제 PC와 동일하게 설정
- `TURTLEBOT3_MODEL=burger`, `LDS_MODEL=LDS-03`: 현재 실물 구성과 일치
- `DASHBOARD_IP`: 데이터/RAG 파이프라인이 도는 PC
- `DAEUN_LAPTOP_IP`: 미니맵 렌더러가 도는 ROS2 관제 PC
- `TTS_TOKEN`: Jetson 수신기와 데이터 PC 송신기에 동일한 값
- `stereo_calibration.npz`: 현재 장착된 카메라에서 만든 파일
- `/health`: `frame_age_sec`와 `video_age_sec`가 계속 증가하지 않는지

## 정본과 복사본

| 기능 | 개발 원본 | Jetson 배포본 |
|---|---|---|
| 비전/브리지/보정 | `../젯슨 연결용 프로그램(손준영)/` | 이 폴더 루트 |
| TTS | `../TTS Engine and pipeline/` | `tts/` |

`젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/`은 과거 zip 제작용
뼈대입니다. 새 배포는 이 폴더를 사용하고, 구형 뼈대에 다시 파일을 수동 복사하지 마세요.
