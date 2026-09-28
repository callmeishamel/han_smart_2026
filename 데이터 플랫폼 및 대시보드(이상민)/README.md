# 스마트 팩토리 순찰 로봇 — 데이터 플랫폼 & 관제 대시보드

한이음 멘토링 프로젝트 중 이상민 담당 파트(이벤트 로그 기반 데이터 플랫폼 설계 및
관리자 대시보드 구현)의 전체 코드입니다.

## 폴더 구조

```
데이터 플랫폼 및 대시보드(이상민)/
├── common/
│   └── schema.py                 # 공통 스키마: DB 설정, 테이블 DDL, DetectionEvent, 위험도 판정 로직
├── dashboard/
│   └── smart_factory_dashboard_v3.py   # Streamlit 관제 대시보드 (DB 조회 + 영상/미니맵)
├── pipeline/
│   ├── jetson_patrol_pipeline.py       # Jetson(NanoOWL) -> DB 적재 파이프라인
│   └── patrol_pipeline_rag.py          # 위 파이프라인 + 위험 등급 이벤트 대응안 생성(RAG)
├── integration/
│   ├── team_integration_adapter.py     # 팀원 파트(비전/디지털트윈/LLM) 연동 어댑터
│   └── response_agent.py               # 대응안 생성 에이전트 실제 구현체 (정진철)
├── rag/
│   ├── setup_knowledge_base.py         # 산업안전 지식베이스 테이블/데이터 구성 (최초 1회)
│   ├── embed_chunks.py                 # (선택) 임베딩 적재 + 벡터 검색
│   ├── vllm_client.py                  # (선택) vLLM/Ollama 경량 LLM 클라이언트
│   └── sql/                            # 지식베이스 DDL/시드 데이터 (참고용)
├── edge_video/
│   ├── camera_stream_server.py         # (참고용) 젯슨 순수 영상 MJPEG 서버
│   ├── event_logger.py                 # 이상민님 PC: SLAM 탐지를 detection_events에 적재
│   └── dashboard_video_minimap_section.py  # 대시보드에서 영상+미니맵 표시
├── admin/
│   └── manage_smart_factory_db.py      # DB 관리용 CLI (구역 CRUD, 로그 조회)
├── self_test/                          # DB/네트워크 없이 돌려보는 단독 테스트 모음
├── docs/                                # RAG 사용 가이드, 판정정책 갱신 제안 등
├── run_pipeline_rag.sh                 # 대응안 생성 포함 파이프라인 실행 스크립트
└── run_dashboard.bat                   # 대시보드 실행 스크립트 (Windows)
```

## 왜 `common/schema.py`가 따로 있는가

처음에는 대시보드, 파이프라인, 통합 어댑터, 관리 CLI가 각자 테이블 정의와
`DetectionEvent`, 위험도 판정 로직을 따로 갖고 있었습니다. 그 결과:

- 관리 CLI(`manage_smart_factory_db.py` 원본)가 대시보드와 **다른 테이블 스키마**를
  가정하고 있어서, CLI로 넣은 데이터가 대시보드에 전혀 반영되지 않는 버그가 있었습니다.
- 위험도 판정 기준(예: 사람과의 거리 임계값)을 나중에 조정하면 파이프라인과
  통합 어댑터 중 한쪽에만 반영되고 다른 쪽은 옛날 로직으로 남을 위험이 있었습니다.

`common/schema.py`를 모든 컴포넌트가 import해서 쓰도록 통일해, 이 문제를 구조적으로
막았습니다. **위험도 판정 기준을 바꾸고 싶으면 이 파일 하나만 수정하면 됩니다.**

## 데이터 흐름

```
[Jetson: 젯슨 연결용 프로그램(손준영)/ai_inference_sender.py]  (손준영 팀장 코드, 수정하지 않음)
        │  UDP JSON, DASHBOARD_IP:9998 (대시보드/DB용. ROS2는 9999, 미니맵은 9091)
        ▼
[pipeline/jetson_patrol_pipeline.py]  또는  [integration/.../NanoOwlUdpVisionAdapter]
        │  DetectionEvent (common.schema)
        ▼
[PostgreSQL: patrol_logs / patrol_zones / response_plans / detection_events]  (common.schema로 테이블 정의 통일)
        │
        ├─ 조회 ──▶ [dashboard/smart_factory_dashboard_v3.py]  (Streamlit 관제 대시보드)
        └─ 위험 등급일 때 ──▶ [integration/response_agent.py: RagResponseAgent (정진철 파트, 연동 완료)]

[관리자] ──▶ [admin/manage_smart_factory_db.py]  (구역 CRUD, 로그 조회 CLI)

--- 영상 + SLAM 미니맵 (별도 경로) ---

[Jetson: ai_inference_sender.py]
        │  UDP: {"label": "person", "angle_offset": 0.12}  (각도만 전송, depth 불안정 문제 회피)
        ▼
[이다은님 노트북: ROS2_자율주행_및_연동(이다은)/dashboard_link/minimap_renderer.py]
        │  (미니맵 렌더러는 이 파일 하나뿐입니다. rclpy 없이 로직만 확인하려면
        │   self_test/test_minimap_logic.py 가 스텁으로 이 파일을 불러 검증합니다)
        │  SLAM 지도(occupancy grid)에 레이캐스팅 → 실제 위치(x, y) 역산
        │  SpatialObjectTracker로 중복 병합 + 노이즈 제거
        ├─ 미니맵 페이지(/) ──iframe──▶ [dashboard: render_minimap() — 본문 전체 폭]
        │    └ 지도는 /map_data 로 1회, 이후 /state 로 좌표만 (5Hz)
        └─ JSON(/detections) ──▶ [edge_video/event_logger.py, 이상민님 PC] ──▶ [DB: detection_events]
```

> 미니맵 표시 방식이 바뀌었습니다(v4). 예전에는 서버가 매 프레임 지도를 JPEG로
> 구워 보내는 MJPEG(`/minimap_feed`)를 `<img>`로 받았지만, 지금은 브라우저가 직접
> 그리는 페이지를 `iframe`으로 띄웁니다. 확대·이동·로봇 추적·탐지 목록 클릭이
> 되고 대역폭도 훨씬 적게 씁니다. `/minimap_feed`도 호환용으로 남아 있어
> `MINIMAP_LEGACY_MJPEG=1`로 되돌릴 수 있습니다.

- **비전 모듈(손준영 파트)**: 코드 도착 완료. NanoOWL + StereoSGBM으로 fire/person with no
  helmet/vehicle을 탐지해 UDP로 JSON을 보냅니다. 실제 payload에는 위험도가 없어서,
  `common/schema.py`의 `classify_risk()`가 (객체, 거리) 조합으로 위험도를 매깁니다.
- **LLM 에이전트(정진철 파트)**: 코드 도착 완료. `integration/response_agent.py`의
  `RagResponseAgent`가 `team_integration_adapter.py`의 `ResponseAgentAdapter` Protocol을
  만족하는 실제 구현체로 연동되어 있습니다.
- **디지털 트윈(이다은 파트)**: 코드 도착, 연동 완료(`ROS2_자율주행_및_연동(이다은)/`).
  위 다이어그램의 경로(젯슨 각도 UDP → `minimap_renderer.py` 레이캐스팅 → `/detections`
  → `event_logger.py`)를 씁니다. `team_integration_adapter.py`에 있던
  `DigitalTwinAdapter`/`ZoneLayout`은 끝내 구현체도 호출부도 생기지 않았고, 그 자리를
  `/map_data`·`/state`가 이미 채우고 있어서 **삭제했습니다**(경위는
  [`integration/README.md`](integration/README.md) 2번 항목).

## 실행 전 준비

**상민님 PC는 Windows이므로, 아래 안내는 전부 PowerShell 기준입니다.**
(Jetson에서 돌아가는 `pipeline/jetson_patrol_pipeline.py`만 예외적으로 Linux
환경이라 별도로 표시해뒀습니다.)

이 저장소는 깃허브에 올리는 것을 전제로, **비밀번호가 코드/커밋에 절대 남지 않도록**
"예시 파일 복사" 방식을 씁니다. `*.example.*`로 끝나는 파일은 실제 값이 없어서
커밋해도 안전하고, 거기서 복사해서 만든 실제 파일(`secrets.toml`, `set_env.ps1`,
`set_env.sh`)은 `.gitignore`에 등록되어 있어 자동으로 커밋 대상에서 빠집니다.

```powershell
pip install psycopg2-binary streamlit pandas requests
```

**1) pipeline / admin / edge_video 스크립트용 (환경변수)**

Windows(PowerShell) — **상민님 PC에서는 이걸 쓰세요**:
```powershell
copy set_env.example.ps1 set_env.ps1
notepad set_env.ps1        # SFP_DB_PASSWORD 값을 실제 비밀번호로 수정
. .\set_env.ps1            # 새 터미널을 열 때마다 이렇게 "로드" (맨 앞 마침표+공백 필수)
```

리눅스/Jetson(bash) — **Jetson에서 파이프라인을 돌릴 때만 이걸 쓰세요**:
```bash
cp set_env.example.sh set_env.sh
nano set_env.sh            # SFP_DB_PASSWORD 값을 실제 비밀번호로 수정
source set_env.sh          # 새 터미널을 열 때마다 이렇게 "로드"
```

**2) 대시보드용 (Streamlit secrets)**

대시보드는 Streamlit 관례에 따라 `st.secrets`를 따로 사용합니다.

```powershell
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
notepad .streamlit\secrets.toml   # db_password 등을 실제 값으로 수정
```

**확인**: `git status`를 쳤을 때 `secrets.toml`, `set_env.ps1`, `set_env.sh`가
목록에 안 뜨면(추적되지 않으면) 정상입니다. 혹시 뜬다면 `.gitignore`가 이미
적용된 후에 만든 파일이 아니거나, 실수로 `git add -f`를 썼을 가능성이 있으니
`git rm --cached <파일명>`으로 스테이징에서 빼주세요.

> **Windows 팁**: PowerShell에서 `set_env.ps1` 같은 스크립트가 "이 시스템에서
> 스크립트를 실행할 수 없습니다"라는 에러로 막히면, 아래 명령을 딱 한 번만
> 실행하면 풀립니다 (관리자 권한 필요 없음).
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

## 실행 방법

**1) Jetson 쪽 파이프라인 (실제 로봇이 있는 현장 — Jetson은 Linux이므로 `python3`)**

```bash
python3 pipeline/jetson_patrol_pipeline.py --zone A --udp-port 9998
```

`ai_inference_sender.py`는 같은 탐지 결과를 세 포트로 동시에 전송합니다:
**9999**(ROS2 브릿지 `vision_inference_node.py`용) / **9998**(대시보드·DB 파이프라인용) /
**9091**(미니맵 각도용, `minimap_renderer.py`). 이 파이프라인은 반드시 `--udp-port 9998`로
실행해야 데이터를 받습니다.

같은 스크립트를 로봇마다(zone별로) 별도 프로세스로 띄우면 됩니다. (단, UDP 포트를
공유하는 경우 zone별로 다른 포트를 쓰도록 `ai_inference_sender.py` 쪽 송신 포트와
맞춰야 합니다.)

**2) 관제 대시보드 (상민님 PC — Windows PowerShell)**

```powershell
streamlit run dashboard\smart_factory_dashboard_v3.py
```

**3) DB 관리 CLI (상민님 PC — Windows PowerShell)**

```powershell
python admin\manage_smart_factory_db.py
```

> **Windows에서는 `python3`가 아니라 `python`을 쓰세요.** Mac/Linux는 보통
> `python3`가 기본 등록되어 있지만, Windows는 `python`만 등록되는 경우가
> 많아서 `python3 ...`를 치면 "인식할 수 없는 명령어"가 뜹니다.

**4) 통합 어댑터 단독 테스트 (상민님 PC — Windows PowerShell, 팀원 코드 없이도 전체 배선 확인)**

```powershell
python integration\team_integration_adapter.py
```

DB가 아직 없어도 자동으로 콘솔 출력 Mock으로 대체되므로 에러 없이 동작을 확인할 수
있습니다.

## 현재 상태 요약

| 파트 | 담당 | 상태 |
|---|---|---|
| 비전 AI 탐지 (NanoOWL) | 손준영 | ✅ 코드 도착, 실제 어댑터(`NanoOwlUdpVisionAdapter`)로 연동 완료 |
| 디지털 트윈 (ROS2/Gazebo) | 이다은 | ✅ 코드 도착, 미니맵 각도 UDP → `/detections` → `event_logger.py` 경로로 연동 완료 |
| 데이터 플랫폼 & 대시보드 | 이상민 (본인) | ✅ 기능 연동 완료 · ⚠ `pipeline/README.md`의 운영 이슈 보완 필요 |
| LLM 에이전트 (RAG) | 정진철 | ✅ 코드 도착, `integration/response_agent.py`(RagResponseAgent)로 연동 완료 |

네 파트 모두 연동이 끝났습니다. `team_integration_adapter.py`에 남아 있던
`DigitalTwinAdapter` TODO는, 이다은 파트가 이미 다른 경로로 붙어 있고 그 자리를
`/map_data`·`/state`가 채우고 있어 **삭제로 정리했습니다**.

대응안 생성까지 포함해서 실행하려면 기본 파이프라인을 직접 실행하는 대신
`run_pipeline_rag.sh`를
사용하세요 (최초 1회 `python rag/setup_knowledge_base.py` 필요). CPU LLM 환경에서는
`./run_pipeline_rag.sh --use-llm --async-llm`을 권장합니다. 자세한 사용법은
`docs/RAG파이프라인_사용가이드.md`를 참고하세요.

`run_pipeline_rag.sh`는 프로젝트 `.venv`가 있으면 자동으로 사용하고,
없으면 `python3`로 실행합니다. 특정 Python을 쓰려면 `SFP_PYTHON`을
지정하세요. 실행 권한과 종료 코드 전파도 보존됩니다.
