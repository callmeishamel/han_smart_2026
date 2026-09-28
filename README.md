# 한이음 2026 — 스마트 팩토리 순찰 로봇

2026 한이음 멘토링 프로젝트. TurtleBot3 기반 순찰 로봇이 공장 내 위험 상황(화재,
안전모 미착용, 차량 근접 등)을 실시간으로 탐지하고, 위치를 지도에 표시하며,
위험 등급에 따라 산업안전 지침 기반 대응안을 자동 생성해 관제 대시보드에
띄워주는 시스템입니다.

이 문서는 4개 파트로 나뉜 코드를 **어떤 순서로 실행해야 전체가 연동되는지**
정리한 통합 실행 안내서입니다. 각 파트의 상세 사용법은 하위 폴더의 README/가이드
문서를 따로 참고하세요.

| 파트 | 담당 | 위치 |
|---|---|---|
| ROS2 자율주행 · 디지털 트윈 · 미니맵 | 이다은 | [`ROS2_자율주행_및_연동(이다은)/`](ROS2_자율주행_및_연동(이다은)/) |
| 비전 추론 (NanoOWL, Jetson/Docker) | 손준영 | [`젯슨 연결용 프로그램(손준영)/`](<젯슨 연결용 프로그램(손준영)/>) |
| 데이터 플랫폼 · 관제 대시보드 · RAG 대응안 생성 | 이상민 / 정진철 | [`데이터 플랫폼 및 대시보드(이상민)/`](<데이터 플랫폼 및 대시보드(이상민)/>) |
| 현장 음성 경보 (Piper TTS) | 정진철 | [`TTS Engine and pipeline/`](TTS%20Engine%20and%20pipeline/) |

팀원별 상세 역할과 코드 위치는 [`TEAM.md`](TEAM.md)를 참고하세요.

---

## 1. 전체 데이터 흐름

```
┌─────────────────────────── Jetson / Docker (손준영) ───────────────────────────┐
│  ai_inference_sender.py                                                        │
│    NanoOWL(VLM) + StereoSGBM(Depth) 로 fire / person-with-no-helmet / vehicle  │
│    탐지 → MJPEG 스트림(:8500) 송출 + UDP JSON 삼중 전송                         │
└───────────┬───────────────────────┬───────────────────────┬────────────────────┘
            │ UDP :9999 (ROS2용)     │ UDP :9998 (대시보드/DB용) │ UDP :9091 (미니맵 각도용)
            ▼                       ▼                         ▼
┌───────────────────────┐ ┌──────────────────────────────┐ ┌──────────────────────────┐
│ vision_inference_node │ │ 데이터 플랫폼(이상민)/pipeline/│ │ dashboard_link/           │
│ .py (ROS2 브릿지, 루트) │ │   jetson_patrol_pipeline.py   │ │  minimap_renderer.py      │
│ → /safety_status 발행  │ │   또는 patrol_pipeline_rag.py │ │  (이다은 노트북, :8091)     │
└───────────┬───────────┘ └───────────────┬───────────────┘ └───────────┬───────────────┘
            │                             │ INSERT                     │ 레이캐스팅으로
            ▼                             ▼                            │ 지도 위 점 계산
┌───────────────────────┐   ┌──────────────────────────────┐           ▼
│ event_detector.py     │   │ PostgreSQL                    │  ┌──────────────────────────┐
│ (이다은, /safety_status│   │   patrol_logs / response_plans│  │ (/map, tf로 로봇 위치 결합)│
│  구독 → /event_markers,│   │   patrol_zones / detection_events│└───────────┬───────────────┘
│  /detected_events 발행)│   └───────────────┬───────────────┘              │ 영상(:8091)+미니맵
└───────────────────────┘                   │ 조회                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ dashboard/smart_factory_dashboard_v3.py  (Streamlit, 이상민 PC)                │
│   DB 조회 + 젯슨 영상(:8500) + 미니맵(:8091) 표시 + 위험 등급 강조             │
└──────────────────────────────────────────────────────────────────────────────┘
```

위험 등급 이벤트는 대응안을 만든 뒤 **현장 스피커로도 나갑니다** (`--tts`로 켬).

```
┌──────────────────────────────────┐   TCP :9997      ┌────────────────────────────┐
│ pipeline/patrol_pipeline_rag.py  │ ─{id,text,우선순위}▶│ TTS Engine and pipeline/   │
│   --tts (bounded 송신 worker)      │ ◀─ queued ACK ── │   Rx_pipeline.py (젯슨)     │
└──────────────────────────────────┘                  │ 우선순위 큐→Piper→aplay→스피커│
                                                      └────────────────────────────┘
```

(`edge_video/event_logger.py`는 3번 미니맵 렌더러의 `/detections`를 폴링해 `detection_events`에
따로 적재합니다. `event_detector.py`의 `/event_markers`는 RViz, `/detected_events`는
`smart_factory_sim/web_dashboard.py`가 구독합니다 — 위 다이어그램에서는 일부 생략.)

### Gazebo mock 시연 경로

Jetson/NanoOWL 없이도 화재·안전모 미착용 시연을 끝까지 할 수 있습니다. 시뮬레이션
launch가 Gazebo 표적 모델을 감지해 `/detected_events`로 발행하고, `mock_event_bridge`가
동일 이벤트를 기존 NanoOWL UDP 계약(`:9998`)으로 변환합니다. 따라서 아래 경로가 실제
카메라 경로와 같은 DB·RAG·대시보드를 사용합니다.

```
Gazebo 표적 → event_detector → /detected_events → RViz · ROS2 웹 대시보드 · 미니맵
                                           └→ mock_event_bridge → UDP :9998
                                                                      → DB · RAG 대응안 · 관제 대시보드
```

미니맵은 `/detected_events`의 이미 투영된 지도 좌표를 직접 구독하므로 mock 시연에서는
Jetson 각도 UDP(`:9091`)가 필요 없습니다. `event_logger.py`를 함께 실행하면 미니맵의
`/detections`를 통해 `detection_events` 테이블에도 적재됩니다.

**핵심 규칙 — UDP 포트를 헷갈리지 마세요.**
[`ai_inference_sender.py`](<젯슨 연결용 프로그램(손준영)/ai_inference_sender.py>)는 같은 탐지 결과를 **세 포트로 동시에** 보냅니다.

| 포트 | 용도 | 받는 쪽 |
|---|---|---|
| `9999` | ROS2 브릿지용 | [`vision_inference_node.py`](<젯슨 연결용 프로그램(손준영)/vision_inference_node.py>) |
| `9998` | 대시보드/DB용 | `jetson_patrol_pipeline.py`, `patrol_pipeline_rag.py`, `NanoOwlUdpVisionAdapter` |
| `9091` | 미니맵 각도용 (`{"label", "angle_offset"}`, 다은님 노트북) | `minimap_renderer.py` |

---

## 2. 실행 순서

전체를 다 켤 필요는 없습니다 — 지금 확인하려는 범위만 골라서 실행하면 됩니다.
아래는 **전체 파이프라인을 처음부터 끝까지 다 돌릴 때**의 권장 순서입니다.
(UDP는 리스너가 먼저 떠 있어야 패킷을 놓치지 않으므로, **받는 쪽 → 보내는 쪽** 순서를 지키세요.)

### 0) 사전 준비 (최초 1회, 각 PC마다)

1. PostgreSQL 설치 및 실행, DB 생성 (`smart_factory_db`)
2. 파이썬 패키지 설치
   ```bash
   # 데이터 플랫폼/대시보드/RAG (이상민 PC)
   pip install -r "데이터 플랫폼 및 대시보드(이상민)/requirements.txt"

   # Jetson (손준영, 파이프라인만 돌릴 때)
   pip3 install -r "젯슨 연결용 프로그램(손준영)/requirements.txt"

   # Jetson/Docker 비전 추론 (손준영)
   pip3 install -r "젯슨 연결용 프로그램(손준영)/requirements-vision.txt"   # torch/nanoowl은 Jetson 전용 빌드 별도 필요
   ```
   ROS2 패키지(`smart_factory_sim`)는 pip이 아니라 `colcon build`로 빌드합니다 (3단계 참고).
3. 환경변수/시크릿 파일 준비 — 비밀번호가 커밋되지 않도록 예시 파일에서 복사해서 씁니다.
   - 이상민 PC(Windows): `copy set_env.example.ps1 set_env.ps1` → 값 수정 → 새 터미널마다 `. .\set_env.ps1`
   - Jetson(Linux): `cp set_env.example.sh set_env.sh` → 값 수정 → `source set_env.sh`
   - 대시보드: `copy .streamlit\secrets.toml.example .streamlit\secrets.toml` → 값 수정
   - 자세한 내용: [`데이터 플랫폼 및 대시보드(이상민)/README.md`](<데이터 플랫폼 및 대시보드(이상민)/README.md>) "실행 전 준비" 절

### 1) DB

테이블은 각 스크립트가 처음 연결할 때 자동 생성됩니다(`common/schema.py`의
`init_all_tables`). RAG 대응안까지 쓰려면 최초 1회만 지식베이스를 구성하세요.
```bash
cd "데이터 플랫폼 및 대시보드(이상민)"
python rag/setup_knowledge_base.py
```

### 2) (선택) ROS2 시뮬레이션 / 실물 SLAM — 이다은 파트

디지털 트윈으로 테스트할지, 실물 로봇으로 맵핑할지에 따라 launch 파일을 고릅니다.
```bash
# 순찰 데모 (랙 6개 공장 + Gazebo + Nav2 + RViz + 웨이포인트 순찰)
ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true

# 넓은 장애물 월드에서 주행 시험
ros2 launch smart_factory_sim simulation.launch.py patrol:=true

# 실물 로봇 SLAM (Cartographer) — 시뮬레이션에는 쓸 수 없음
ros2 launch smart_factory_sim real_cartographer.launch.py
```

실물 지도를 저장한 뒤에는 Cartographer를 종료하고, 같은 로봇 브링업을 유지한 채
저장 지도를 다시 쓰는 AMCL/Nav2를 실행합니다. 이 조합이 `/map`과
`map → base_footprint`를 제공하므로 미니맵/관제 대시보드에 저장 지도와 현재 위치가
표시됩니다.

```bash
# --free를 생략하면 미탐사 회색이 자유 공간으로 다시 읽혀 맵 전체가 사각형이 될 수 있습니다.
ros2 run nav2_map_server map_saver_cli -f factory_map --free 0.196
ros2 launch smart_factory_sim real_navigation.launch.py \
  map:=$HOME/factory_map.yaml initial_x:=0.0 initial_y:=0.0 initial_yaw:=0.0
```

이미 저장한 지도라면 `factory_map.yaml`의 `free_thresh`를 `0.196`으로 바꾸고
`map_server`/Nav2를 다시 시작하세요. `/map`이 발행된 뒤 값을 고쳐도 기존 프로세스에는
반영되지 않습니다.

**Gazebo mock 화재·안전모 위반 전체 시연**에서는 Jetson 4번·7번을 띄우지 않고,
먼저 5번의 `patrol_pipeline_rag.py`를 실행한 뒤 아래처럼 시뮬레이터를 띄우면 됩니다.
파이프라인이 다른 PC에 있으면 해당 PC IP를 `mock_udp_host`에 넣으세요.

```bash
ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true \
  mock_udp_host:=127.0.0.1 mock_udp_port:=9998
```

공통 인자: `patrol`(기본 false), `use_nav2`(기본 true), `use_rviz`(기본 true),
`gui`(기본 true).

> **월드 · 지도 · 웨이포인트는 한 묶음입니다.** launch마다 짝이 정해져 있고,
> 하나라도 어긋나면 순찰 목표가 전부 실패합니다. 월드 파일을 수정했다면 반드시
> `python3 maps/generate_map.py`로 지도를 다시 만든 뒤 `colcon build` 하세요.
> 자세한 내용은 [`ROS2_자율주행_및_연동(이다은)/launch/README.md`](ROS2_자율주행_및_연동(이다은)/launch/README.md).

### 3) (선택) 미니맵 렌더러 — 이다은 노트북

2번의 `/map` 토픽과 `map → base_footprint` TF가 먼저 떠 있어야 합니다.
```bash
cd "ROS2_자율주행_및_연동(이다은)/dashboard_link"
./run_minimap.sh
```
UDP `:9091`(젯슨 각도 데이터 수신), HTTP `:8091`을 씁니다. `:8091`은 미니맵
페이지(`/`), 지도(`/map_data`), 로봇 좌표(`/state`), 탐지 목록(`/detections`),
그리고 구버전 호환용 MJPEG(`/minimap_feed`)를 제공합니다. 지도 위에는 로봇 위치와
AI 탐지 마커만 표시합니다 — LiDAR 장애물 빨간 점은 벽을 물체로 오인하는 일이 잦아
제거했습니다.

> **접근 제한 권장.** 이 서버는 공장 도면, 로봇 실시간 위치, 화재 이벤트,
> 작업자 위치와 헬멧 미착용 이력을 내보냅니다. 기본값은 `0.0.0.0`에 인증 없이
> 열리므로, 운영 시에는 아래처럼 좁히세요.
> ```bash
> export MINIMAP_BIND=203.0.113.20
> export MINIMAP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
> ```
> **토큰을 켰다면 미니맵을 부르는 쪽 두 곳 모두에 같은 값을 넣어야 합니다.**
> 대시보드(8번)와 이벤트 로거(6번)입니다. 한쪽만 넣으면 그쪽만 403을 받고
> 조용히 멈추는데, 화면은 멀쩡해 보여서 원인을 찾기 어렵습니다.
> `self_test\test_integration_wiring.py`가 이 짝이 맞는지 검사합니다.

### 4) ROS2 브릿지 노드 — 손준영/이다은

```bash
cd "젯슨 연결용 프로그램(손준영)"
python3 vision_inference_node.py
```
UDP `127.0.0.1:9999`를 리스닝해 ROS2 `/safety_status` 토픽으로 재발행합니다.
`ai_inference_sender.py`와 같은 호스트에서 실행해야 합니다.

### 4-1) (선택) TTS 음성 경보 수신기 — 젯슨

대응안을 현장 스피커로 방송하려면, 파이프라인(5번)보다 **먼저** 젯슨에서 띄웁니다.
```bash
cd "TTS Engine and pipeline"
./setup_tts.sh          # 최초 1회 - 모델 내려받기 + 스피커 확인 + 소리 테스트
python3 Rx_pipeline.py
```
TCP `:9997`을 리스닝합니다. `piper` CLI와 `aplay`(alsa-utils), 음성 모델이 필요한데
`setup_tts.sh`가 셋 다 한 번에 점검합니다.

> `PIPER_MODEL_PATH`를 직접 지정한다면 **`setup_tts.sh`가 받는 파일과 이름이 같아야
> 합니다**(`ko_KR-kss-medium.onnx`). 지정하지 않으면 스크립트 옆에서 알아서 찾습니다.
자세한 내용: [`TTS Engine and pipeline/README.md`](TTS%20Engine%20and%20pipeline/README.md)

### 5) 데이터 파이프라인 — 이상민 파트

대응안 생성이 필요 없으면 `jetson_patrol_pipeline.py`, 필요하면
`patrol_pipeline_rag.py`(둘 중 하나만, 같은 포트 9998을 씀).
```bash
cd "데이터 플랫폼 및 대시보드(이상민)"

# 기본 (감지만 적재)
python3 pipeline/jetson_patrol_pipeline.py --zone A --udp-port 9998

# 구역을 로봇 위치에서 자동으로 판정 (미니맵 서버 필요, requests 패키지 필요)
python3 pipeline/jetson_patrol_pipeline.py --zone auto --zone-fallback A

# 대응안 생성 포함 (권장) — Linux/Jetson
./run_pipeline_rag.sh

# 대응안 + 현장 음성 방송 (4-1번을 먼저 띄운 경우)
./run_pipeline_rag.sh --tts
```

`--tts`는 선택입니다. TTS 폴더를 못 찾으면 경고만 남기고 나머지는 그대로 동작합니다.

### 6) (선택) SLAM 탐지 이벤트 로거 — 이상민 PC

3번 미니맵 렌더러의 `/detections`를 폴링해 `detection_events`에 적재합니다.
```bash
python3 "데이터 플랫폼 및 대시보드(이상민)/edge_video/event_logger.py"
```

### 7) Jetson 비전 추론 — 손준영 파트 (가장 마지막에 켜세요)

받는 쪽(4, 5번)이 먼저 떠 있어야 패킷을 놓치지 않습니다.
```bash
cd "젯슨 연결용 프로그램(손준영)"
python3 ai_inference_sender.py
```
MJPEG 영상은 `:8500`(`/`)으로, 탐지 결과는 UDP로 `:9999`/`:9998`/`:9091`에
동시 전송됩니다. 필요한 pip 의존성은 같은 폴더의 `requirements-vision.txt`를 참고하세요.

Docker로 실행한다면 `source set_env.sh`만으로는 목적지 환경변수가 컨테이너에
전달되지 않습니다. `smart_factory_project/README.md`의 명령처럼
`-e DASHBOARD_IP -e DAEUN_LAPTOP_IP` 등을 반드시 넘기세요. 이 항목이 빠지면
영상은 정상이어도 DB/RAG와 미니맵 탐지 목록은 계속 0건으로 남습니다.

### 7-1) Autopilot 기준 Jetson 배포 파일

최신 Jetson 배포 정본은 루트 [`smart_factory_project/`](smart_factory_project/)입니다.
과거 `jetson용_smart_factory_project_1/` 뼈대에 파일을 다시 수동 복사하지 마세요.

Autopilot 구성에서 Jetson이 상시 실행하는 프로젝트 파일은 다음 세 가지입니다.

| 실행 환경 | 파일 | 역할 |
|---|---|---|
| NanoOWL Docker | `ai_inference_sender.py` | 카메라·추론·영상·UDP 송신 |
| Jetson 호스트 ROS2 | `vision_inference_node.py` | UDP :9999를 `/safety_status`로 변환 |
| Jetson 호스트(선택) | `tts/Rx_pipeline.py` | TCP :9997 음성 경보 수신·재생 |

`stereo_calibrate.py`와 `tts/setup_tts.sh`는 최초 설정/진단용입니다. 반대로
`pipeline/`, `common/`, `tts/Rag_to_Jetson.py`는 현재 분산 구성에서 데이터 PC가
사용하는 호환 파일이며 Jetson Autopilot 핵심 프로세스가 아닙니다. 정확한 배포 트리와
시작 순서는 [`smart_factory_project/README.md`](smart_factory_project/README.md)를
참고하세요.

### 8) 관제 대시보드 — 이상민 PC

```bash
cd "데이터 플랫폼 및 대시보드(이상민)"
streamlit run dashboard\smart_factory_dashboard_v3.py
# 또는 Windows: run_dashboard.bat 더블클릭
```

### 9) (선택) DB 관리 CLI

```bash
python "데이터 플랫폼 및 대시보드(이상민)/admin/manage_smart_factory_db.py"
```

---

## 3. 하드웨어 없이 빠르게 동작 확인하기 (개발용)

Jetson/ROS2 장비 없이 이상민 PC 한 대로 파이프라인~대시보드 연동만 확인하고 싶다면:

```bash
cd "데이터 플랫폼 및 대시보드(이상민)"

# 터미널 A: 파이프라인 실행 (실제 Jetson 대신 가짜 UDP 송신기를 받을 준비)
python pipeline\jetson_patrol_pipeline.py --zone A

# 터미널 B: 가짜 Jetson 패킷 5개 전송
python self_test\fake_jetson_sender.py

# 터미널 A에 "적재 완료 [A] ..." 로그가 뜨면 성공. 이어서 대시보드 실행:
streamlit run dashboard\smart_factory_dashboard_v3.py
```

DB/네트워크 없이 로직만 확인하려면:
```bash
python self_test\test_response_agent.py   # 대응안 생성 로직 50건 검증
python self_test\test_schema_logic.py     # 위험도 판정 로직 검증
python self_test\test_minimap_logic.py    # 미니맵 좌표 변환 로직 검증
```

---

## 4. 환경변수 요약

| 변수 | 기본값 | 의미 | 어디서 |
|---|---|---|---|
| `SFP_DB_HOST` / `SFP_DB_PASSWORD` 등 | — | PostgreSQL 접속 정보 | `set_env.*` |
| `ROS2_BRIDGE_IP` | `HOST_IP` → `127.0.0.1` | 젯슨이 9999(ROS2 브릿지)를 보낼 대상 IP. 브릿지는 **젯슨 호스트**에서 돕니다 | ai_inference_sender.py |
| `DASHBOARD_IP` | `HOST_IP` → `127.0.0.1` | 젯슨이 9998(DB 파이프라인)을 보낼 대상 IP. 파이프라인은 **이상민님 PC**에서 돕니다 | ai_inference_sender.py |
| `HOST_IP` | `127.0.0.1` | 위 두 변수의 공통 기본값(하위 호환). **두 목적지가 다른 기기라면 위 두 개를 따로 지정해야 합니다** | ai_inference_sender.py |
| `PIPELINE_UDP_BIND` | `0.0.0.0` | 파이프라인이 젯슨 패킷을 받을 바인딩 주소. 루프백이면 다른 기기 패킷을 못 받습니다 | jetson_patrol_pipeline.py, patrol_pipeline_rag.py, team_integration_adapter.py |
| `JETSON_IP` / `JETSON_VIDEO_PORT` | `203.0.113.10` / `8500` | 젯슨 주소 — 대시보드가 영상을 가져오고, TTS 송신이 대응안을 보내는 곳 | dashboard_video_minimap_section.py, Rag_to_Jetson.py |
| `DAEUN_LAPTOP_IP` / `MINIMAP_PORT` | `203.0.113.20` / `8091` | 대시보드/이벤트 로거가 미니맵 서버를 찾는 주소 | dashboard_video_minimap_section.py, event_logger.py |
| `MINIMAP_TOKEN` | (없음) | 미니맵 접근 토큰. 켰다면 **서버와 부르는 쪽 두 곳 모두** 같은 값이어야 합니다 | minimap_renderer.py(서버), dashboard_video_minimap_section.py, event_logger.py |
| `DAEUN_LAPTOP_IP` / `DETECTION_UDP_PORT` | `203.0.113.20` / `9091` | `ai_inference_sender.py`가 탐지 각도(angle_offset)를 보낼 주소, 미니맵 렌더러가 그 각도 데이터를 받는 포트 | ai_inference_sender.py, minimap_renderer.py |
| `ROS2_UDP_PORT` | `9999` | 젯슨 → ROS2 브릿지 UDP 포트. **보내는 쪽과 받는 쪽이 같은 변수를 읽습니다** | ai_inference_sender.py, vision_inference_node.py |
| `ROS2_BRIDGE_BIND_IP` | `127.0.0.1` | ROS2 브릿지가 UDP를 수신할 주소 (같은 호스트 전제) | vision_inference_node.py |
| `DASHBOARD_UDP_PORT` | `9998` | 젯슨 → 대시보드/DB 파이프라인 UDP 포트 | ai_inference_sender.py |
| `TTS_TCP_PORT` | `9997` | 대응안 문장 → 젯슨 TTS 포트. **보내는 쪽과 받는 쪽이 같은 변수를 읽습니다** | Rag_to_Jetson.py, Rx_pipeline.py |
| `TTS_BIND_IP` | `0.0.0.0` | TTS 수신기가 바인딩할 주소 | Rx_pipeline.py |
| `TTS_TOKEN` / `TTS_ALLOWED_IPS` | (없음) | TTS 공유 비밀 / 수신 허용 IP 목록. 운영 시 하나 이상 설정 | Rag_to_Jetson.py, Rx_pipeline.py |
| `TTS_MESSAGE_TTL_SEC` | `20` | 이 시간이 지난 경보는 젯슨 재생 큐에 넣지 않음 | Rag_to_Jetson.py, Rx_pipeline.py |
| `TTS_SEND_QUEUE_SIZE` | `16` | PC 쪽 비동기 송신 대기열 크기 | Rag_to_Jetson.py |
| `TTS_STATUS_MAX_ENTRIES` | `4096` | 젯슨의 message ID 중복 방지 상태 캐시 상한 | Rx_pipeline.py |
| `TTS_VOLUME_PERCENT` / `TTS_VOLUME_MAX_PERCENT` | `140` / `400` | Piper 원본 대비 TTS 소프트웨어 음량과 대시보드 상한. 원본 피크는 자동으로 클리핑을 방지합니다 | Rag_to_Jetson.py, Rx_pipeline.py, smart_factory_dashboard_v3.py |
| `PIPER_MODEL_PATH` | 스크립트 옆 `ko_KR-kss-medium.onnx` | Piper 음성 모델 경로. `setup_tts.sh`가 받는 이름과 같아야 합니다 | Rx_pipeline.py |
| `TTS_MODULE_DIR` | 실행 파일 옆 `tts/` → 저장소 루트의 `TTS Engine and pipeline/` | TTS 송신 모듈(`Rag_to_Jetson.py`)을 찾을 위치. 두 후보를 순서대로 보므로 보통 지정할 필요가 없습니다 | patrol_pipeline_rag.py, smart_factory_dashboard_v3.py |

실제 IP는 팀원 PC마다 다르므로, 각자 환경에 맞게 실행 전 `set` (Windows) /
`export` (Linux)로 덮어써야 합니다. (이 저장소의 예시 IP는 실제 팀원 네트워크
주소를 가리지 않도록 문서용 대역인 `203.0.113.0/24`(RFC 5737)로 치환했습니다.
`127.0.0.1`/`0.0.0.0`은 특정 기기를 식별하지 않으므로 그대로 두었습니다.)

### ⚠ UDP 목적지가 세 곳으로 갈린다는 점에 주의하세요

젯슨은 같은 탐지 결과를 세 포트로 뿌리는데, **세 목적지가 서로 다른 기기**입니다.

```
                        ┌─ :9999 ─▶ vision_inference_node.py   (젯슨 호스트)      ROS2_BRIDGE_IP
ai_inference_sender.py ─┼─ :9998 ─▶ patrol_pipeline_rag.py     (이상민님 PC)      DASHBOARD_IP
   (젯슨 Docker 안)       └─ :9091 ─▶ minimap_renderer.py        (이다은님 노트북)   DAEUN_LAPTOP_IP
```

예전에는 9999와 9998이 `HOST_IP` 하나를 함께 썼습니다. 두 기기가 다르므로
**둘 다 만족시키는 값이 없어서, 어느 쪽으로 맞추든 나머지 하나는 아무것도 받지
못했습니다.** UDP는 목적지가 없어도 에러를 주지 않으므로 증상이 "그냥 조용함"으로만
나타납니다. 지금은 목적지마다 변수가 따로 있고, 둘이 같은 IP면 젯슨 시작 로그에
경고가 뜹니다.

받는 쪽도 마찬가지입니다. 파이프라인의 `--udp-ip` 기본값이 `127.0.0.1`이라
**젯슨 패킷을 구조적으로 받을 수 없었습니다**(어떤 실행 스크립트도 이 값을 바꾸지
않았습니다). 지금은 `PIPELINE_UDP_BIND`(기본 `0.0.0.0`)를 쓰고, 루프백에 바인딩되면
경고 로그가 뜹니다.

이 배선이 어긋나지 않는지는 아래 테스트가 확인합니다.

```powershell
python self_test\test_integration_wiring.py
```

**포트 변수는 짝을 이루는 양쪽이 같은 값을 읽습니다.** 예를 들어 `ROS2_UDP_PORT`를
바꾸면 `ai_inference_sender.py`(송신)와 `vision_inference_node.py`(수신)가 함께
따라갑니다. 예전처럼 한쪽 코드에만 하드코딩된 포트를 고쳐서 조용히 데이터가
끊기는 일을 막기 위한 구조입니다. `vision_inference_node.py`는 ROS2 파라미터로도
덮어쓸 수 있습니다:
```bash
python3 vision_inference_node.py --ros-args -p udp_port:=9999 -p bind_ip:=127.0.0.1
```

---

## 5. 이슈 상태 / 제한사항

### 해결 완료

- **위험도 판정 `[해결 완료]`**: `classify_risk`가
  `ALWAYS_DANGER_OBJECTS`/`DISTANCE_BASED_OBJECTS`를 사용하도록 바뀌어 현재 탐지 클래스
  3종을 의도대로 판정합니다. `person with no helmet`과 `fire`는 거리와 무관하게
  `위험`, `vehicle`은 거리에 따라 `위험`/`주의`가 됩니다. 경위는
  [`docs/판정정책_갱신제안.md`](<데이터 플랫폼 및 대시보드(이상민)/docs/판정정책_갱신제안.md>),
  검증은 `python self_test\test_schema_logic.py`입니다.
- **UDP 목적지 충돌 `[해결 완료]`**: ROS2(`ROS2_BRIDGE_IP:9999`),
  DB/대시보드(`DASHBOARD_IP:9998`), 미니맵(`DAEUN_LAPTOP_IP:9091`)의 목적지를 분리했고,
  DB 파이프라인은 기본적으로 `0.0.0.0`에 바인딩합니다.
- **RAG 지식베이스 영구 폴백 `[해결 완료]`**: 최초 DB 연결 실패 후에도
  `KB_RETRY_INTERVAL_SEC` 간격으로 재연결하므로 프로세스를 재시작하지 않아도 복구됩니다.
- **TTS 무인증 상태 `[기능 해결, 운영 설정 필요]`**: `TTS_TOKEN`과
  `TTS_ALLOWED_IPS` 검증이 구현됐습니다. 호환성을 위해 기본값은 제한 없음이므로 실제
  현장에서는 반드시 토큰 또는 허용 IP를 설정해야 합니다.
- **TTS TCP 전달만 성공으로 보던 문제 `[부분 해결]`**: 모든 요청에 `message_id`를 붙이고
  젯슨의 인증·만료 검사와 재생 큐 등록 후 `queued` ACK를 받습니다. 재시도 중복도 같은
  ID로 제거합니다. 실제 Piper·스피커 재생 완료 ACK는 아직 실기 검증 범위입니다.

### 현재 남은 실제 문제

- **DB 장애가 UDP 수신까지 멈춥니다.** 기본 파이프라인의 DB 재연결과 UDP 수신이 같은
  스레드에 있어, DB 장애 동안 재연결 백오프가 진행되면 UDP 수신 버퍼가 넘쳐 탐지가
  유실될 수 있습니다. 수신 큐/DB writer 분리가 필요합니다.
- **대응안 억제 상태가 처리 성공보다 먼저 기록됩니다.** 대응안 생성 실패, 비동기 큐
  포화 또는 DB 저장 실패가 발생해도 같은 `(구역, 객체)` 경보가 기본 30초 동안 억제될
  수 있습니다. 큐 등록·저장 성공 후 확정하거나 실패 시 rollback해야 합니다.
- **형식은 맞지만 타입이 잘못된 JSON이 수신기를 종료시킬 수 있습니다.** 최상위 값이
  객체가 아니거나 `detections`가 객체 목록이 아니면 `AttributeError`/`TypeError`가 날 수
  있습니다. 스키마 타입 검증 후 잘못된 패킷만 버리도록 보완해야 합니다.
- **일부 Linux 셸 스크립트에 실행 권한이 없습니다.** 새 clone에서 문서대로
  `./run_pipeline_rag.sh`, `./setup_tts.sh` 등을 실행하면 `Permission denied`가 날 수
  있습니다. 현재는 `bash 스크립트명.sh` 또는 `chmod +x 스크립트명.sh`로 우회하고,
  저장소에는 실행 비트(`100755`)를 반영해야 합니다.
- **실행 래퍼 종료 코드 `[부분 해결]`**: Jetson 호환용 `run_pipeline.sh`는 Python 종료
  코드를 저장해 그대로 반환하고 비대화형 실행에서는 `read`를 건너뜁니다.
  데이터 PC의 `run_pipeline_rag.sh`는 아직 같은 보완이 필요합니다.

### 검증·운영 제한

- **실기 검증은 아직입니다.** 젯슨 추론 FPS, `/safety_status` → RViz 마커 경로,
  Piper 음성 출력, PostgreSQL 적재는 하드웨어가 있는 환경에서 확인해야 합니다.
  특히 `event_detector.py`의 `jetson_focal_length`(500.0),
  `jetson_frame_width/height`(1280/720)가 실제 카메라와 맞는지,
  `jetson_camera_frame` TF가 실제로 발행되는지 확인이 필요합니다.
- **RAG 시드의 `CHUNKS`는 검증용 문장입니다.** 제출·운영 전 저작권과 사용 조건을
  확인한 실제 산업안전 자료로 교체해야 합니다. 자세한 조건은
  [`rag/README.md`](<데이터 플랫폼 및 대시보드(이상민)/rag/README.md>)를 참고하세요.
- **`worlds/smart_factory.world`와 `maps/factory_map.pgm`의 크기가 다릅니다.**
  월드는 20×20m이고 `factory_map.pgm`은 실제 SLAM 세션에서 저장한 약 5.0×5.3m
  지도입니다. 현재 launch는 `generate_map.py`가 만든 짝 맞는 지도를 사용하므로 문제없지만,
  `factory_map.*`을 수동 지정하면 어긋납니다.

---

## 6. 하위 문서

- [`데이터 플랫폼 및 대시보드(이상민)/README.md`](<데이터 플랫폼 및 대시보드(이상민)/README.md>) — 데이터 플랫폼/대시보드 상세 구조 및 실행법
- [`데이터 플랫폼 및 대시보드(이상민)/docs/RAG파이프라인_사용가이드.md`](<데이터 플랫폼 및 대시보드(이상민)/docs/RAG파이프라인_사용가이드.md>) — 대응안 생성 파이프라인 상세 사용법
- [`데이터 플랫폼 및 대시보드(이상민)/docs/판정정책_갱신제안.md`](<데이터 플랫폼 및 대시보드(이상민)/docs/판정정책_갱신제안.md>) — 위험도 판정 정책 이슈 (5절 참고)
- [`젯슨 연결용 프로그램(손준영)/손준영팀장님_실행안내(new).md`](<젯슨 연결용 프로그램(손준영)/손준영팀장님_실행안내(new).md>) — Jetson에서 파이프라인만 단독 실행하는 법
- [`TTS Engine and pipeline/README.md`](TTS%20Engine%20and%20pipeline/README.md) — 현장 음성 경보 구성, Piper 설치, 파이프라인 연동 방법
