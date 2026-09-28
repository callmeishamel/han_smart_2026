# 다은님 Ubuntu 노트북 중심 통합 관제·Jetson 원격 실행 구축안

이 문서는 현재 여러 PC와 터미널에서 개별 실행하는 스마트팩토리 구성요소를
다은님 Ubuntu 노트북에 모으고, Jetson은 SSH로 원격 제어하기 위한 구현 명세입니다.
해당 노트북에서 저장소와 이 문서를 연 뒤 Codex에 전달하여 실제 서비스 파일과
통합 실행기를 만들 때 사용합니다.

## 1. 목표

물리 장비는 Ubuntu 관제 노트북과 Jetson 두 대를 유지하되, 사용자는 Ubuntu
노트북에서만 조작합니다.

```text
Ubuntu 관제 노트북
 ├─ PostgreSQL + pgvector
 ├─ Ollama(GEMMA 2B)
 ├─ ROS2/Nav2/Gazebo 또는 실물 주행 launch
 ├─ Jetson 비전 → ROS2 브릿지(:9999/UDP)
 ├─ 미니맵 렌더러(:9091/UDP, :8091/HTTP)
 ├─ RAG·DB 파이프라인(:9998/UDP)
 ├─ detection event_logger
 ├─ Streamlit 대시보드
 └─ SSH로 Jetson 서비스 시작·중지·상태·로그 관리

Jetson
 ├─ 카메라·NanoOWL·스테레오 추론·MJPEG(:8500)
 └─ Piper TTS 수신·재생(:9997/TCP)
```

최종 사용자 명령은 다음 형태를 목표로 합니다.

```bash
./factoryctl start --mode real
./factoryctl start --mode simulation
./factoryctl status
./factoryctl logs rag
./factoryctl stop
```

같은 PC에 배치하더라도 각 기능은 독립 프로세스와 서비스로 유지합니다. 한 기능의
재시작이 전체 시스템을 종료시키지 않게 하고, 로그와 장애 원인을 서비스별로 분리하기
위해서입니다.

## 2. 통합 배치 기준

### Ubuntu 노트북으로 이동할 항목

| 기능 | 현재 파일 또는 명령 | 비고 |
|---|---|---|
| PostgreSQL | 시스템 서비스 | 운영 DB와 RAG 지식베이스를 같은 Linux DB로 통합 가능 |
| pgvector | PostgreSQL 서버 확장 | DB와 같은 Ubuntu 노트북에 설치 |
| Ollama | `ollama serve`, `gemma2:2b` | GPU가 없어도 CPU 실행 가능 |
| ROS2·Nav2 | `ROS2_자율주행_및_연동(이다은)` | simulation/real 모드는 상호 배타적 |
| 비전 ROS2 브릿지 | `젯슨 연결용 프로그램(손준영)/vision_inference_node.py` | `ROS2_BRIDGE_BIND_IP=0.0.0.0` 필요 |
| 미니맵 | `ROS2_자율주행_및_연동(이다은)/dashboard_link/run_minimap.sh` | ROS2 `/map`과 TF 준비 후 시작 |
| RAG 파이프라인 | `데이터 플랫폼 및 대시보드(이상민)/run_pipeline_rag.sh` | `--use-llm --async-llm` 권장 |
| 이벤트 로거 | `데이터 플랫폼 및 대시보드(이상민)/edge_video/event_logger.py` | 미니맵 `/detections` 폴링 |
| 대시보드 | `dashboard/smart_factory_dashboard_v3.py` | 네트워크의 다른 PC는 브라우저로 접속 |
| TTS 송신 | RAG 파이프라인 및 대시보드에 이미 연결됨 | 수신·실제 재생만 Jetson |

### Jetson에 남길 항목

| 기능 | 현재 파일 또는 명령 | 이유 |
|---|---|---|
| 비전 추론·영상 송출 | `젯슨 연결용 프로그램(손준영)/ai_inference_sender.py` | 카메라와 Jetson 전용 NanoOWL 환경 의존 |
| TTS 수신·재생 | `TTS Engine and pipeline/Rx_pipeline.py` | 스피커, Piper, ALSA 장치 의존 |

`데이터 플랫폼 및 대시보드(이상민)/edge_video/camera_stream_server.py`는 참고용이며,
실제 영상 송출은 `ai_inference_sender.py`가 이미 담당하므로 별도 서비스로 만들지
않습니다.

## 3. 네트워크와 환경변수

아래 예시에서 실제 주소는 코드나 서비스 파일에 하드코딩하지 않고 별도 환경 파일에
넣습니다.

```text
UBUNTU_CONTROL_IP=<다은님 Ubuntu 노트북의 고정 IP>
JETSON_IP=<Jetson의 고정 IP>
JETSON_SSH_USER=<Jetson SSH 사용자>
```

### Jetson 환경

세 UDP 목적지를 모두 Ubuntu 노트북으로 지정합니다.

```bash
export DASHBOARD_IP="$UBUNTU_CONTROL_IP"       # UDP 9998
export ROS2_BRIDGE_IP="$UBUNTU_CONTROL_IP"     # UDP 9999
export DAEUN_LAPTOP_IP="$UBUNTU_CONTROL_IP"    # UDP 9091
export DASHBOARD_UDP_PORT=9998
export ROS2_UDP_PORT=9999
export DETECTION_UDP_PORT=9091
export TTS_TCP_PORT=9997
```

`HOST_IP` 하나에 의존하지 말고 위 세 목적지를 명시적으로 설정합니다.

### Ubuntu 관제 노트북 환경

```bash
export SFP_DB_HOST=localhost
export SFP_DB_PORT=5432
export SFP_DB_NAME=smart_factory_db
export SFP_DB_USER=postgres
export SFP_DB_PASSWORD='<별도 비밀 환경 파일에서 설정>'

export PIPELINE_UDP_BIND=0.0.0.0
export DASHBOARD_UDP_PORT=9998
export ROS2_BRIDGE_BIND_IP=0.0.0.0
export ROS2_UDP_PORT=9999

export DAEUN_LAPTOP_IP=127.0.0.1
export MINIMAP_PORT=8091
export MINIMAP_BIND="$UBUNTU_CONTROL_IP"

export JETSON_IP="$JETSON_IP"
export JETSON_VIDEO_PORT=8500
export TTS_TCP_PORT=9997

export LLM_BASE_URL=http://127.0.0.1:11434/v1
export LLM_MODEL=gemma2:2b
export LLM_TIMEOUT=180
```

Ubuntu에서 PostgreSQL과 pgvector를 함께 사용하면 `RAG_KB_*`를 지정하지 않아도
운영 DB와 지식베이스가 같은 DB를 사용합니다. DB를 일부러 분리할 때만
`RAG_KB_HOST`, `RAG_KB_NAME` 등을 설정합니다.

### 포트 표

| 포트 | 프로토콜 | 송신 → 수신 | 수신 위치 |
|---:|---|---|---|
| 9999 | UDP | Jetson 비전 → `vision_inference_node.py` | Ubuntu |
| 9998 | UDP | Jetson 비전 → `patrol_pipeline_rag.py` | Ubuntu |
| 9091 | UDP | Jetson 각도 → `minimap_renderer.py` | Ubuntu |
| 8091 | HTTP | 대시보드·event_logger → 미니맵 | Ubuntu |
| 8500 | HTTP/MJPEG | 대시보드 → Jetson 영상 | Jetson |
| 9997 | TCP | RAG·대시보드 → TTS 수신기 | Jetson |
| 5432 | TCP | 로컬 서비스 → PostgreSQL | Ubuntu, 외부 공개 불필요 |
| 11434 | HTTP | RAG → Ollama | Ubuntu, 외부 공개 불필요 |
| 8501 | HTTP | 브라우저 → Streamlit | Ubuntu |

Ubuntu 방화벽에는 필요한 LAN 입력만 허용합니다. PostgreSQL과 Ollama가 전부 로컬에서
사용된다면 5432와 11434는 외부에 열지 않습니다. 미니맵에 `MINIMAP_TOKEN`을 설정한
경우 대시보드와 event_logger에도 같은 값을 넣습니다.

## 4. 실행 모드

ROS2 실행기는 동시에 여러 개를 띄우지 않습니다. 통합 실행기는 모드를 하나만 받도록
구현합니다.

### 시뮬레이션 모드

```bash
ros2 launch smart_factory_sim digital_twin.launch.py patrol:=true \
  mock_udp_host:=127.0.0.1 mock_udp_port:=9998
```

이 모드에서는 실제 Jetson 비전 송신기를 시작하지 않아도 됩니다. 필요하면 TTS만 Jetson에
원격으로 띄울 수 있습니다.

### 실물 모드

저장 지도를 이용한 예시는 다음과 같습니다.

```bash
ros2 launch smart_factory_sim real_navigation.launch.py \
  map:=$HOME/factory_map.yaml initial_x:=0.0 initial_y:=0.0 initial_yaw:=0.0
```

실물 모드에서는 Ubuntu의 UDP 리스너가 준비된 후 Jetson 비전 송신기를 가장 마지막에
시작합니다.

### 매핑 모드

```bash
ros2 launch smart_factory_sim real_cartographer.launch.py
```

매핑과 Nav2 실물 주행은 동시에 시작하지 않습니다. 기존 자동 매핑 실행기가 있다면 해당
실행기를 서비스 명령으로 감싸되 내부 동작은 재작성하지 않습니다.

## 5. 시작·종료 순서

UDP는 재전송이 없으므로 수신기를 먼저 시작합니다.

### 시작 순서

1. Ubuntu PostgreSQL 상태 확인
2. Ubuntu Ollama 상태 확인
3. 선택한 ROS2 모드 시작
4. Ubuntu 비전 ROS2 브릿지 시작 (`0.0.0.0:9999`)
5. Ubuntu 미니맵 시작 (`:9091`, `:8091`)
6. Ubuntu event_logger 시작
7. Ubuntu RAG 파이프라인 시작 (`0.0.0.0:9998`)
8. Ubuntu Streamlit 대시보드 시작
9. SSH로 Jetson TTS 수신기 시작 (`:9997`)
10. 실물 모드라면 SSH로 Jetson 비전 송신기 시작 — 항상 마지막

RAG 기본 실행 명령은 다음을 기준으로 합니다.

```bash
cd '데이터 플랫폼 및 대시보드(이상민)'
./run_pipeline_rag.sh --use-llm --async-llm --tts
```

중복 억제는 기존 기본값 30초를 유지합니다. `jetson_patrol_pipeline.py`와
`patrol_pipeline_rag.py`는 같은 9998 포트를 사용하므로 절대 동시에 시작하지 않습니다.

### 종료 순서

1. Jetson 비전 송신기 중지
2. Ubuntu RAG 파이프라인 중지 및 워커 드레인
3. Ubuntu event_logger와 대시보드 중지
4. 미니맵과 ROS2 노드 중지
5. 필요하면 Jetson TTS 수신기 중지
6. PostgreSQL과 Ollama는 상시 서비스로 유지하거나 명시적 옵션으로만 중지

## 6. systemd와 SSH 설계

### 서비스 단위

Ubuntu 쪽에는 다음과 같은 이름을 권장합니다.

```text
smart-factory-ros2.service
smart-factory-vision-bridge.service
smart-factory-minimap.service
smart-factory-event-logger.service
smart-factory-rag.service
smart-factory-dashboard.service
```

ROS2의 simulation/real/mapping 명령이 다르므로 실제 구현에서는 하나의 고정 서비스에
모든 명령을 넣기보다, 모드별 템플릿 서비스 또는 모드 래퍼를 사용해 동시에 하나만
활성화되도록 합니다.

Jetson 쪽에는 다음 두 서비스를 권장합니다.

```text
smart-factory-vision.service
smart-factory-tts.service
```

Jetson 비전이 Docker 안에서 실행된다면 `smart-factory-vision.service`의 `ExecStart`는
추측해서 작성하지 않습니다. Jetson에서 현재 실제로 쓰는 `docker run`, `docker compose`,
`docker exec` 또는 Python 실행 절차를 먼저 확인한 뒤 래퍼 스크립트로 감쌉니다.

### systemd 기본 정책

- `WorkingDirectory`는 저장소의 실제 절대 경로를 사용합니다.
- 환경변수와 비밀번호는 `EnvironmentFile`로 분리하고 권한을 `600`으로 제한합니다.
- Python 가상환경이 있다면 `ExecStart`에 해당 Python 절대 경로를 사용합니다.
- 장기 실행 서비스는 `Restart=on-failure`와 적절한 `RestartSec`를 사용합니다.
- 종료 시 Python이 `SIGTERM`을 받고 정리할 시간을 확보합니다.
- 로그는 우선 journald에 남기고 `journalctl -u <서비스>`로 확인합니다.
- 설치 스크립트는 여러 번 실행해도 같은 결과가 되도록 멱등성을 지킵니다.
- 기존 수동 실행 스크립트는 삭제하지 않고 서비스가 이를 재사용하거나 병행 가능하게 둡니다.

### SSH 설정

Ubuntu에서 전용 SSH 키를 만들고 Jetson에 공개키를 등록합니다.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/smart_factory_jetson
ssh-copy-id -i ~/.ssh/smart_factory_jetson.pub "$JETSON_SSH_USER@$JETSON_IP"
```

`~/.ssh/config` 예시:

```sshconfig
Host smart-factory-jetson
    HostName <JETSON_IP>
    User <JETSON_SSH_USER>
    IdentityFile ~/.ssh/smart_factory_jetson
    ServerAliveInterval 15
    ServerAliveCountMax 3
```

통합 실행기는 `ssh smart-factory-jetson ...` 형태를 사용합니다. 원격에서 system 서비스
제어에 sudo가 필요하면 모든 sudo 권한을 열지 말고 위 두 서비스의
`start/stop/restart/status`에 필요한 명령만 비밀번호 없이 허용하는 방식을 검토합니다.
권한 변경은 자동으로 강행하지 말고 생성된 sudoers 파일을 사용자가 검토한 후 설치합니다.

SSH 세션이 끊겨도 systemd가 시작한 Jetson 프로세스는 계속 실행되어야 합니다.

## 7. 통합 실행기 `factoryctl` 요구사항

`factoryctl`은 단순히 여러 명령을 백그라운드로 던지는 스크립트가 아니라, 단계별 상태를
확인하고 실패 지점을 알려주는 제어 도구로 구현합니다.

### 필수 명령

```text
factoryctl start --mode simulation|real|mapping
factoryctl stop
factoryctl restart --mode ...
factoryctl status
factoryctl logs [all|ros2|minimap|rag|dashboard|jetson-vision|jetson-tts]
factoryctl doctor
```

### 동작 요구사항

- 저장소 위치, 환경 파일, Jetson SSH 별칭을 설정 파일에서 읽습니다.
- 시작 전 필요한 파일과 실행 파일 존재 여부를 확인합니다.
- 이미 실행 중인 서비스를 다시 시작해 중복 프로세스를 만들지 않습니다.
- `start`는 각 단계의 readiness 확인 후 다음 단계로 넘어갑니다.
- 실패하면 어느 PC의 어떤 서비스가 실패했는지 출력하고 0이 아닌 종료 코드를 반환합니다.
- `status`는 로컬 systemd와 Jetson 원격 systemd를 한 화면에 보여줍니다.
- `logs`는 로컬 `journalctl` 또는 SSH 원격 `journalctl`을 사용합니다.
- `stop`은 송신기부터 역순으로 정리합니다.
- `--dry-run`을 제공해 실행될 명령을 상태 변경 없이 확인할 수 있게 합니다.
- 실제 비밀번호, 토큰, 사설키 내용을 출력하지 않습니다.

## 8. readiness와 장애 진단

단순히 프로세스가 존재하는지만 보지 말고 가능한 경우 실제 응답을 확인합니다.

| 기능 | 권장 확인 방법 |
|---|---|
| PostgreSQL | `pg_isready` 및 필요한 DB 접속 |
| RAG 지식베이스 | `python3 rag/setup_knowledge_base.py --check` |
| Ollama | `curl http://127.0.0.1:11434/api/tags` 또는 `/v1/models` |
| ROS2 | 필요한 `/map` 토픽 및 TF 존재 여부 |
| 미니맵 | `curl http://127.0.0.1:8091/state` |
| RAG UDP | systemd 활성 상태와 9998 UDP 바인딩 확인 |
| Streamlit | `curl http://127.0.0.1:8501/_stcore/health` |
| Jetson 비전 | `curl http://$JETSON_IP:8500/health` |
| Jetson TTS | 원격 systemd 활성 상태와 9997 TCP 리스닝 확인 |
| SSH | `ssh -o BatchMode=yes smart-factory-jetson true` |

`doctor`는 포트 충돌도 확인해야 합니다. 특히 9998에 기본 파이프라인과 RAG 파이프라인이
동시에 바인딩되지 않았는지 검사합니다.

## 9. Codex 구현 요청사항

다은님 Ubuntu 노트북에서 Codex는 먼저 저장소의 실제 경로, ROS2 배포판, Python 환경,
Jetson SSH 접속 방식과 현재 비전 실행 명령을 읽기 전용으로 확인합니다. 확인되지 않은
경로나 사용자명·IP·Docker 명령은 임의로 확정하지 말고 예시 설정으로 분리합니다.

권장 산출물 구조:

```text
deploy/
 ├─ README.md
 ├─ bin/
 │   └─ factoryctl
 ├─ env/
 │   ├─ control.env.example
 │   └─ jetson.env.example
 ├─ systemd/
 │   ├─ control/
 │   │   ├─ smart-factory-vision-bridge.service
 │   │   ├─ smart-factory-minimap.service
 │   │   ├─ smart-factory-event-logger.service
 │   │   ├─ smart-factory-rag.service
 │   │   └─ smart-factory-dashboard.service
 │   └─ jetson/
 │       ├─ smart-factory-vision.service
 │       └─ smart-factory-tts.service
 ├─ install_control.sh
 ├─ install_jetson.sh
 └─ uninstall.sh
```

ROS2 모드 서비스나 래퍼는 실제 launch 구조를 확인한 뒤 위 구조에 추가합니다.

### 구현 단계

1. 현재 Ubuntu에서 각 프로그램을 수동으로 한 번씩 실행해 실제 명령과 의존성을 확인
2. Jetson에서 현재 비전·TTS 수동 실행 명령과 Docker 여부 확인
3. 환경 파일 예시와 `factoryctl --dry-run`부터 구현
4. Ubuntu 서비스 파일 구현 및 `systemd-analyze verify` 수행
5. Jetson 서비스 파일 구현 및 Jetson에서 개별 검증
6. SSH 원격 제어 연결
7. `start/status/logs/stop` 전체 흐름 검증
8. simulation 모드 검증 후 real 모드 검증
9. 재부팅 후 자동 시작 여부는 사용자 선택 옵션으로 제공

### 구현 중 지켜야 할 사항

- 실제 `set_env.sh`, DB 비밀번호, TTS/MINIMAP 토큰, SSH 사설키는 커밋하지 않습니다.
- 기존 사용자 변경과 기존 수동 실행 경로를 보존합니다.
- 서비스 설치 전에 생성될 파일과 실행될 sudo 명령을 보여줍니다.
- PostgreSQL 데이터 삭제나 DB 재생성은 자동화 범위에 포함하지 않습니다.
- 방화벽 규칙 변경과 부팅 자동 시작은 명시적 옵션 또는 사용자 승인 후 수행합니다.
- ROS2 simulation, real navigation, mapping이 동시에 실행되지 않게 합니다.
- Jetson 비전 송신기는 Ubuntu 수신기 준비가 확인된 뒤 시작합니다.
- RAG는 `--use-llm --async-llm`을 사용하고 기존 TTS·중복 억제·자동 구역 기능을 유지합니다.

## 10. 완료 기준

아래 조건을 모두 만족하면 구축 완료로 봅니다.

- Ubuntu에서 명령 하나로 선택한 모드의 전체 서비스가 시작됨
- Jetson 터미널에 직접 접속하지 않아도 비전과 TTS를 제어할 수 있음
- SSH 연결이 끊겨도 Jetson 서비스가 유지됨
- 수신기 준비 후 송신기가 시작되어 초기 UDP 패킷을 놓치지 않음
- `factoryctl status`에서 로컬·원격 서비스 상태를 함께 확인 가능
- `factoryctl logs`에서 서비스별 로그 확인 가능
- `factoryctl stop`이 송신기부터 역순으로 안전하게 종료함
- simulation과 real/mapping 모드가 동시에 실행되지 않음
- DB·LLM 장애 시 원인이 `doctor`와 서비스 로그에 명확히 표시됨
- 실제 비밀번호·토큰·SSH 키가 Git 변경 목록에 포함되지 않음
- 기존 수동 실행 명령도 계속 사용할 수 있음

## 11. Codex에 전달할 요청문

아래 문장을 이 문서와 함께 Codex에 전달하면 됩니다.

> 이 저장소의 `다은님_Ubuntu_통합관제_원격실행_구축안.md`를 끝까지 읽고,
> 현재 Ubuntu 노트북과 저장소의 실제 실행 환경을 먼저 점검해 주세요. 문서의 목표대로
> Ubuntu 로컬 서비스와 Jetson SSH 원격 서비스를 한 명령으로 관리하는 `deploy/` 구성을
> 구현해 주세요. 기존 수동 실행 경로와 사용자 변경은 보존하고, 비밀번호·토큰·SSH 키는
> 커밋하지 마세요. IP, 사용자명, Docker 실행 방식처럼 로컬에서 확인할 수 없는 값은
> 하드코딩하지 말고 예시 환경 파일이나 명시적 질문으로 남겨 주세요. 각 서비스는 개별
> 검증하고, 마지막에 dry-run, 서비스 파일 검증, simulation 모드 통합 검증 결과를 정리해
> 주세요. 실제 로봇 구동과 방화벽·부팅 자동 시작처럼 외부 상태를 바꾸는 단계는 실행 전에
> 사용자에게 확인받아 주세요.

