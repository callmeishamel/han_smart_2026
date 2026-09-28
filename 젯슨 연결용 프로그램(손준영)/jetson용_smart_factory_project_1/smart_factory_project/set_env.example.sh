# set_env.example.sh — 젯슨에서 쓰는 환경변수 템플릿
#
# 이 파일은 예시(템플릿)입니다. 실제 비밀번호가 없으므로 깃허브에 커밋해도 안전합니다.
#
# 사용법:
#   1. 이 파일을 같은 폴더에 "set_env.sh" 이라는 이름으로 복사하세요.
#      (복사한 set_env.sh는 .gitignore에 등록되어 있어 자동으로 커밋 대상에서 제외됩니다.)
#   2. 아래 "반드시 채워야 하는 값"을 실제 값으로 바꾸세요.
#   3. 터미널을 열 때마다 아래처럼 "실행"이 아니라 "로드"하세요.
#
#      source set_env.sh
#
#      (source 없이 ./set_env.sh 로 실행하면 별도 프로세스에서 끝나버려서 반영되지 않습니다.)
#
# 젯슨에서 도는 프로그램은 셋입니다. 이 파일 하나로 셋 다 설정합니다.
#
#   ai_inference_sender.py   비전 추론 + 영상 스트리밍 + UDP 송신   (Docker 컨테이너)
#   vision_inference_node.py ROS2 브릿지                          (호스트, ROS2 환경)
#   Rx_pipeline.py           음성 경보 수신 + Piper TTS            (호스트, --tts 쓸 때만)
#
# ============================================================
# 반드시 채워야 하는 값
# ============================================================

# --- DB 접속 (이 젯슨에서 jetson_patrol_pipeline.py 를 직접 돌릴 때만) ---
export SFP_DB_NAME=smart_factory_db
export SFP_DB_USER=postgres
export SFP_DB_PASSWORD='여기에_실제_비밀번호_입력'
export SFP_DB_HOST=192.168.0.xxx   # 이상민님 PC의 실제 IP (같은 기기면 localhost)
export SFP_DB_PORT=5432

# --- 탐지 결과를 보낼 곳 ---
#
# 같은 탐지 결과가 세 곳으로 나가는데, **세 목적지가 서로 다른 기기**입니다.
# UDP는 목적지가 없어도 에러를 주지 않으므로, 틀리면 "그냥 조용함"으로만 나타납니다.
#
#   :9999 -> vision_inference_node.py   이 젯슨의 호스트
#   :9998 -> patrol_pipeline_rag.py     이상민님 PC (또는 이 젯슨)
#   :9091 -> minimap_renderer.py        이다은님 노트북
export ROS2_BRIDGE_IP=127.0.0.1       # 컨테이너면 아래 "Docker" 항목 참고
export DASHBOARD_IP=192.168.0.xxx     # 파이프라인이 도는 기기. 이 젯슨이면 127.0.0.1
export DAEUN_LAPTOP_IP=192.168.0.yyy  # 이다은님 노트북

# ============================================================
# 기본값으로 두어도 되는 값 (양쪽이 같은 변수를 읽습니다)
# ============================================================
export ROS2_UDP_PORT=9999
export DASHBOARD_UDP_PORT=9998
export DETECTION_UDP_PORT=9091
export JETSON_VIDEO_PORT=8500

# 이 젯슨에서 파이프라인을 직접 돌린다면, 루프백이 아닌 주소에 바인딩해야
# 다른 기기에서 오는 패킷도 받습니다.
export PIPELINE_UDP_BIND=0.0.0.0

# ROS2 브릿지가 UDP를 받을 주소. 송신 측이 --network host 가 아닌 컨테이너라면
# 127.0.0.1 로는 받을 수 없으므로 0.0.0.0 으로 바꾸세요.
export ROS2_BRIDGE_BIND_IP=127.0.0.1

# ============================================================
# 음성 경보 (--tts 를 쓸 때만)
# ============================================================
export TTS_TCP_PORT=9997
export TTS_BIND_IP=0.0.0.0            # 운영 시에는 이 젯슨의 내부망 IP로 좁히세요
export TTS_MESSAGE_TTL_SEC=20         # 이 시간이 지난 경보는 재생하지 않음
export TTS_SEND_QUEUE_SIZE=16         # RAG 파이프라인을 함께 돌릴 때의 송신 큐
export TTS_SEND_DRAIN_TIMEOUT_SEC=15  # 종료 시 남은 송신을 기다리는 시간
export TTS_STATUS_RETENTION_SEC=300   # message_id 중복 방지 상태 보존 시간
export TTS_STATUS_MAX_ENTRIES=4096    # 중복 방지 상태 캐시 최대 항목 수

# Piper 음성 모델.
#
# **setup_tts.sh 가 받는 파일과 이름이 같아야 합니다.** 예전 템플릿은
# ko_KR-hyeri-medium 을 가리켰는데, 그 음성은 Piper 공식 저장소에 아예 없어서
# 모델을 제대로 받아 놓고도 이 변수 때문에 없는 파일을 보게 됐습니다. 그러면
# 수신기는 시작할 때 경고 한 번, 이후로는 경보마다 "Piper 합성 실패"만 쌓입니다.
#
# 이 줄을 통째로 지워도 됩니다 — 그러면 Rx_pipeline.py 가 자기 옆의 모델을 찾습니다.
export PIPER_MODEL_PATH=$HOME/smart_factory_project/tts/ko_KR-kss-medium.onnx

# TTS 송신 모듈(Rag_to_Jetson.py)이 있는 폴더.
#
# **지금 이 젯슨에서는 아무 효과가 없습니다.** 이 값을 읽는 것은
# pipeline/patrol_pipeline_rag.py 하나인데, 그 파일은 integration/ 을 import 하고
# 젯슨 배포본에는 그 폴더가 없어서 실행되지 않습니다(원래 상민님 PC에서 도는
# 파이프라인입니다). 젯슨의 run_pipeline.sh 는 대응안 생성이 없는
# pipeline/jetson_patrol_pipeline.py 를 부릅니다.
#
# 그래도 미리 넣어 둡니다 — 나중에 그 파이프라인을 젯슨으로 옮기면 그때 쓰이고,
# 지금 있어도 해가 없습니다. 지정하지 않아도 파이프라인이 옆의 tts/ 를 먼저 찾습니다.
export TTS_MODULE_DIR=$HOME/smart_factory_project/tts

# 이 수신기는 데이터 서버가 아니라 **소리를 내는 액추에이터**입니다.
# 비워두면 같은 망의 누구든 공장 스피커로 아무 문장이나 내보낼 수 있습니다.
# 값을 만들어 넣고, 보내는 쪽(이상민님 PC)의 set_env.sh 에도 같은 값을 넣으세요.
#   python3 -c "import secrets;print(secrets.token_urlsafe(16))"
# export TTS_TOKEN=

# 젯슨은 기본 출력이 HDMI로 잡히는 경우가 많습니다. 그러면 스피커를 꽂아도
# 소리가 안 나면서 aplay 는 성공으로 끝나서, 로그만 봐서는 정상으로 보입니다.
#   aplay -l   로 카드/장치 번호 확인
# export APLAY_DEVICE=plughw:1,0

# ============================================================
# Docker 로 ai_inference_sender.py 를 돌릴 때
# ============================================================
# 컨테이너 안에서 127.0.0.1 은 "호스트"가 아니라 "컨테이너 자기 자신"입니다.
# --network host 로 띄우면 위 설정이 그대로 맞고, 영상 포트 매핑도 필요 없습니다.
#
#   docker run --rm -it --runtime nvidia --network host \
#     --device /dev/video0 \
#     -v $(pwd):/work -w /work \
#     <nanoowl 이미지> python3 ai_inference_sender.py
#
# --network host 를 못 쓰면 호스트 IP를 직접 주고 영상 포트를 매핑해야 합니다.
#
#   docker run --rm -it --runtime nvidia \
#     --device /dev/video0 -p 8500:8500 \
#     -e ROS2_BRIDGE_IP=172.17.0.1 \
#     ...

echo "환경변수 설정 완료"
echo "  DB          : $SFP_DB_NAME @ $SFP_DB_HOST:$SFP_DB_PORT"
echo "  ROS2 브릿지  : $ROS2_BRIDGE_IP:$ROS2_UDP_PORT"
echo "  대시보드/DB  : $DASHBOARD_IP:$DASHBOARD_UDP_PORT"
echo "  미니맵 각도  : $DAEUN_LAPTOP_IP:$DETECTION_UDP_PORT"
echo "  영상        : 0.0.0.0:$JETSON_VIDEO_PORT"
