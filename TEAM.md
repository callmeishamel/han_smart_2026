# 팀 구성 및 역할 분담

2026 한이음 멘토링 공모전 — 스마트 팩토리 순찰 로봇 프로젝트의 팀원별 담당 범위를
정리한 문서입니다. 각 파트의 실행 방법은 [`README.md`](README.md)와 하위 폴더의
README를 참고하세요.

## 팀원 한눈에 보기

| 이름 | 역할 | 담당 파트 | 코드 위치 |
|---|---|---|---|
| 손준영 (팀장) | 비전 AI | Jetson/Docker 기반 실시간 위험 탐지 (NanoOWL + 스테레오 뎁스) | [`젯슨 연결용 프로그램(손준영)/`](<젯슨 연결용 프로그램(손준영)/>) |
| 이다은 | 자율주행 · 디지털 트윈 | ROS2/Gazebo 순찰 로봇 주행·SLAM, 미니맵 레이캐스팅 | [`ROS2_자율주행_및_연동(이다은)/`](ROS2_자율주행_및_연동(이다은)/) |
| 이상민 | 데이터 플랫폼 · 대시보드 | 이벤트 적재 DB, 관제 대시보드, 팀 통합 어댑터 | [`데이터 플랫폼 및 대시보드(이상민)/`](<데이터 플랫폼 및 대시보드(이상민)/>) |
| 정진철 | RAG · LLM · 음성 경보 | 대응안 생성 에이전트(RAG/vLLM), 현장 음성 경보(TTS) | 아래 "정진철 파트" 참고 |

전체 데이터가 각 파트 사이를 흘러가는 순서는 루트 [`README.md`](README.md)의
"전체 데이터 흐름" 절 다이어그램을 참고하세요.

---

## 손준영 (팀장) — 비전 AI 탐지

- **담당**: 산업현장 작업자 안전상태 인식 비전 AI 탐지 모듈
- **핵심 기술**: NanoOWL(오픈보캐블러리 VLM) + StereoSGBM 뎁스 추정으로 화재 /
  안전모 미착용 / 차량 근접 탐지, Jetson·Docker 환경 구성
- **코드 위치**: [`젯슨 연결용 프로그램(손준영)/`](<젯슨 연결용 프로그램(손준영)/>)
  (`ai_inference_sender.py`, `vision_inference_node.py`)
- **출력**: MJPEG 영상 스트림(`:8500`) + UDP JSON 탐지 결과를 ROS2 브릿지 /
  대시보드·DB / 미니맵 세 곳에 동시 전송

## 이다은 — 자율주행 · 디지털 트윈

- **담당**: 위험구역 디지털 트윈 모델링 및 순찰 커버리지 시각화
- **핵심 기술**: ROS2 + Gazebo 시뮬레이션(Nav2, Cartographer SLAM), 실물
  TurtleBot3 자율 순찰, 미니맵 레이캐스팅 렌더러
- **코드 위치**: [`ROS2_자율주행_및_연동(이다은)/`](ROS2_자율주행_및_연동(이다은)/)
  (`smart_factory_sim/`, `launch/`, `dashboard_link/minimap_renderer.py`)
- **출력**: `/safety_status`·`/event_markers` ROS2 토픽, 미니맵 HTTP 서버(`:8091`)

## 이상민 — 데이터 플랫폼 · 관제 대시보드

- **담당**: 이벤트 로그 기반 데이터 플랫폼 설계 및 관리자 대시보드, 팀원 간
  인터페이스(계약) 정의 및 통합
- **핵심 기술**: PostgreSQL 스키마 설계, UDP 탐지 수신 파이프라인, Streamlit
  관제 대시보드, [`integration/team_integration_adapter.py`](<데이터 플랫폼 및 대시보드(이상민)/integration/team_integration_adapter.py>)로 각 팀원
  모듈을 어댑터 패턴으로 연동
- **코드 위치**: [`데이터 플랫폼 및 대시보드(이상민)/`](<데이터 플랫폼 및 대시보드(이상민)/>)
  (`pipeline/`, `dashboard/`, `common/`, `integration/`, `admin/`, `edge_video/`)

## 정진철 — RAG · LLM 대응안 생성 · 음성 경보

- **담당**: 상황 맥락 기반 대응안 생성 LLM 에이전트(RAG), 현장 음성 경보(TTS)
- **핵심 기술**: PostgreSQL 지식베이스 + 임베딩 검색으로 산업안전 지침을
  찾아 vLLM/Ollama 경량 LLM으로 대응안 문장 생성, Piper TTS 기반 현장
  스피커 방송 파이프라인
- **코드 위치**:
  - RAG/LLM: [`데이터 플랫폼 및 대시보드(이상민)/rag/`](<데이터 플랫폼 및 대시보드(이상민)/rag/>)
    (`setup_knowledge_base.py`, `embed_chunks.py`, `vllm_client.py`),
    [`integration/response_agent.py`](<데이터 플랫폼 및 대시보드(이상민)/integration/response_agent.py>)의 `RagResponseAgent`
  - 음성 경보: [`TTS Engine and pipeline/`](TTS%20Engine%20and%20pipeline/)
    (`Rag_to_Jetson.py`, `Rx_pipeline.py`)

> **왜 RAG/vLLM 코드가 "이상민" 폴더 안에 있나요?**
> RAG 대응안 생성기는 정진철님이 별도로 개발한 뒤, 대시보드·DB 파이프라인과
> 같은 인터페이스(`team_integration_adapter.py`의 `ResponseAgentAdapter`)로
> 연동하는 과정에서 이상민 폴더(`데이터 플랫폼 및 대시보드(이상민)/rag/`,
> `integration/response_agent.py`)에 합쳐졌습니다. 통합 시점이 다른 파트보다
> 늦어 초기 문서에는 한동안 "코드 미도착"으로 남아 있었지만, 실제 설계·구현은
> 정진철님의 RAG/vLLM 파트입니다. 폴더 경로와 실제 담당자가 다르므로 이 문서로
> 구분해 둡니다.

---

## 참고

- 팀 구성: 손준영(팀장) · 이다은 · 이상민 · 정진철, 2026 한이음 멘토링 공모전
- 역할 분담 원본: [`데이터 플랫폼 및 대시보드(이상민)/integration/team_integration_adapter.py`](<데이터 플랫폼 및 대시보드(이상민)/integration/team_integration_adapter.py>) 상단 주석("역할 분담 (PPT 기준)")
