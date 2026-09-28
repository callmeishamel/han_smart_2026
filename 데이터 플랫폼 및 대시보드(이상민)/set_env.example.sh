# set_env.example.sh
#
# 이 파일은 예시(템플릿)입니다. 실제 비밀번호가 없으므로 깃허브에 커밋해도 안전합니다.
#
# 사용법:
#   1. 이 파일을 같은 폴더에 "set_env.sh" 이라는 이름으로 복사하세요.
#      (복사한 set_env.sh는 .gitignore에 등록되어 있어 자동으로 커밋 대상에서 제외됩니다.)
#   2. 아래 SFP_DB_PASSWORD 값을 실제 비밀번호로 바꾸세요.
#   3. 터미널을 열 때마다 아래처럼 "실행"이 아니라 "로드"하세요.
#
#      source set_env.sh
#
#      (source 없이 ./set_env.sh 로 실행하면 별도 프로세스에서 끝나버려서 반영되지 않습니다.)

# --- DB 접속 ---
export SFP_DB_NAME=smart_factory_db
export SFP_DB_USER=postgres
export SFP_DB_PASSWORD='여기에_실제_비밀번호_입력'
export SFP_DB_HOST=localhost   # PC에서 원격으로 붙는 경우 PC의 실제 IP로 변경
export SFP_DB_PORT=5432

# --- 젯슨 (run_pipeline_rag.sh --tts 로 음성 경보를 쓸 때 필요) ---
# 이 값이 없으면 기본값 203.0.113.10 으로 붙으려다 조용히 실패합니다.
export JETSON_IP=203.0.113.10
export JETSON_VIDEO_PORT=8500
export TTS_TCP_PORT=9997
export TTS_MESSAGE_TTL_SEC=20
export TTS_SEND_QUEUE_SIZE=16
export TTS_SEND_DRAIN_TIMEOUT_SEC=15
# 젯슨의 Rx_pipeline.py 에서 TTS_TOKEN 을 켰다면 같은 값을 여기에도 넣으세요.
# 이 수신기는 소리를 내는 액추에이터라, 열어두면 같은 망의 누구든 공장 스피커로
# 아무 문장이나 내보낼 수 있습니다.
# export TTS_TOKEN=

# --- 이다은님 노트북 (미니맵 / event_logger.py 가 폴링하는 대상) ---
export DAEUN_LAPTOP_IP=203.0.113.20
export MINIMAP_PORT=8091
# 미니맵 서버에서 MINIMAP_TOKEN 을 켰다면 같은 값을 여기에도 넣으세요.
# export MINIMAP_TOKEN=

# --- 젯슨 탐지 결과를 받을 UDP 바인딩 주소 ---
# 젯슨은 이 PC와 다른 기기이므로 127.0.0.1 로 두면 패킷이 하나도 도착하지 않습니다.
# UDP라 에러도 안 나서, "UDP 수신 대기 시작" 로그만 뜬 채 조용히 멈춰 있는 것처럼
# 보입니다. 특정 랜카드로만 받고 싶으면 그 인터페이스의 IP를 직접 넣으세요.
export PIPELINE_UDP_BIND=0.0.0.0
export DASHBOARD_UDP_PORT=9998
# 젯슨 쪽(ai_inference_sender.py)에는 이 PC의 IP를 DASHBOARD_IP 로 넣어야 합니다.
#   export DASHBOARD_IP=<이 PC의 IP>      # 젯슨에서 실행

# --- 지식베이스 DB (운영 DB와 분리할 때만) ---
# 설정하지 않으면 위 SFP_DB_* 값을 그대로 사용합니다. pgvector를 리눅스의
# 별도 PostgreSQL에 둘 때 아래 항목의 주석을 해제하세요.
# export RAG_KB_HOST=localhost
# export RAG_KB_PORT=5432
# export RAG_KB_NAME=smart_factory_kb
# export RAG_KB_USER=postgres
# export RAG_KB_PASSWORD=

# --- LLM 서버 (선택) ---
# Ollama + GEMMA 2B 기준. LLM을 사용하지 않으면 주석 처리해도 됩니다.
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_MODEL=gemma2:2b
export LLM_TIMEOUT=180
# 대시보드 사이드바 초기 TTS 음량. 100=원본, 최대 400=증폭.
# 슬라이더를 바꾸면 이 값보다 사이드바 값이 우선됩니다.
export TTS_VOLUME_PERCENT=140
# 100~400. 송신기/수신기와 같은 값으로 두세요.
export TTS_VOLUME_MAX_PERCENT=400
# 화재·가스/유해물질 누출·연기 긴급 방송은 첫 감지 즉시 5회 반복합니다.
# 같은 이벤트가 매 프레임 들어와도 이 시간 안에는 다시 방송하지 않습니다.
export TTS_EMERGENCY_COOLDOWN_SEC=30
# BGE-M3 등 임베딩 모델 캐시. 용량이 크므로 여유 있는 경로로
# 바꿔도 됩니다. 미설정 시 Hugging Face 기본 캐시를 사용합니다.
# export HF_HOME="$HOME/.cache/huggingface"

echo "환경변수 설정 완료 (DB: $SFP_DB_NAME @ $SFP_DB_HOST, 젯슨: $JETSON_IP, 미니맵: $DAEUN_LAPTOP_IP:$MINIMAP_PORT, UDP 수신: $PIPELINE_UDP_BIND)"
