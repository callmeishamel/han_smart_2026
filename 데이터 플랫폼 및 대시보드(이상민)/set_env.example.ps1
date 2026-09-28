# set_env.example.ps1
#
# 이 파일은 예시(템플릿)입니다. 실제 비밀번호가 없으므로 깃허브에 커밋해도 안전합니다.
#
# 사용법:
#   1. 이 파일을 같은 폴더에 "set_env.ps1" 이라는 이름으로 복사하세요.
#      (복사한 set_env.ps1은 .gitignore에 등록되어 있어 자동으로 커밋 대상에서 제외됩니다.)
#   2. 아래 SFP_DB_PASSWORD 값을 실제 비밀번호로 바꾸세요.
#   3. 프로젝트 폴더에서 새 PowerShell 창을 열 때마다 아래처럼 "실행"이 아니라 "로드"하세요.
#
#      . .\set_env.ps1
#
#      (맨 앞의 마침표와 공백이 중요합니다 - 이래야 이 창의 환경변수로 반영됩니다.
#       그냥 .\set_env.ps1 로 실행하면 별도 프로세스에서 끝나버려서 반영되지 않습니다.)

# --- DB 접속 ---
$env:SFP_DB_NAME = "smart_factory_db"
$env:SFP_DB_USER = "postgres"
$env:SFP_DB_PASSWORD = "여기에_실제_비밀번호_입력"
$env:SFP_DB_HOST = "localhost"
$env:SFP_DB_PORT = "5432"

# --- 젯슨 (대시보드가 영상을 가져올 주소) ---
$env:JETSON_IP = "203.0.113.10"
$env:JETSON_VIDEO_PORT = "8500"
$env:TTS_TCP_PORT = "9997"
$env:TTS_MESSAGE_TTL_SEC = "20"
$env:TTS_SEND_QUEUE_SIZE = "16"
$env:TTS_SEND_DRAIN_TIMEOUT_SEC = "15"
# 젯슨의 Rx_pipeline.py 에서 TTS_TOKEN 을 켰다면 같은 값을 여기에도 넣으세요.
# 이 수신기는 소리를 내는 액추에이터라, 열어두면 같은 망의 누구든 공장 스피커로
# 아무 문장이나 내보낼 수 있습니다.
# $env:TTS_TOKEN = ""

# --- 이다은님 노트북 (미니맵 / event_logger.py 가 폴링하는 대상) ---
# 예전에는 이 값이 템플릿에 없어서 event_logger.py 를 돌릴 때마다
# $env:DAEUN_LAPTOP_IP 를 손으로 넣어야 했습니다.
$env:DAEUN_LAPTOP_IP = "203.0.113.20"
$env:MINIMAP_PORT = "8091"
# 미니맵 서버에서 MINIMAP_TOKEN 을 켰다면 같은 값을 여기에도 넣으세요.
# $env:MINIMAP_TOKEN = ""

# --- 젯슨 탐지 결과를 받을 UDP 바인딩 주소 ---
# 젯슨은 이 PC와 다른 기기이므로 127.0.0.1 로 두면 패킷이 하나도 도착하지 않습니다.
# UDP라 에러도 안 나서, "UDP 수신 대기 시작" 로그만 뜬 채 조용히 멈춰 있는 것처럼
# 보입니다. 특정 랜카드로만 받고 싶으면 그 인터페이스의 IP를 직접 넣으세요.
$env:PIPELINE_UDP_BIND = "0.0.0.0"
$env:DASHBOARD_UDP_PORT = "9998"
# 젯슨 쪽(ai_inference_sender.py)에는 이 PC의 IP를 DASHBOARD_IP 로 넣어야 합니다.
#   export DASHBOARD_IP=<이 PC의 IP>      # 젯슨에서 실행

# --- 지식베이스 DB (운영 DB와 분리할 때만) ---
# 아래 변수를 설정하지 않으면 SFP_DB_*를 그대로 사용합니다.
# $env:RAG_KB_HOST = "localhost"
# $env:RAG_KB_PORT = "5432"
# $env:RAG_KB_NAME = "smart_factory_kb"
# $env:RAG_KB_USER = "postgres"
# $env:RAG_KB_PASSWORD = ""

# --- LLM 서버 (선택, Ollama + GEMMA 2B 기준) ---
$env:LLM_BASE_URL = "http://localhost:11434/v1"
$env:LLM_MODEL = "gemma2:2b"
$env:LLM_TIMEOUT = "180"

Write-Host "환경변수 설정 완료 (DB: $env:SFP_DB_NAME @ $env:SFP_DB_HOST, 젯슨: $env:JETSON_IP, 미니맵: $env:DAEUN_LAPTOP_IP`:$env:MINIMAP_PORT, UDP 수신: $env:PIPELINE_UDP_BIND)"
