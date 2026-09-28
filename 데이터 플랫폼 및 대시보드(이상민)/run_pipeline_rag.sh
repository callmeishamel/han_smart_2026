#!/bin/bash
# ------------------------------------------------------------
# Jetson 순찰 파이프라인 실행 스크립트 (대응안 생성 포함)
#
# 기존 run_pipeline.sh 와의 차이:
#   run_pipeline.sh      UDP 수신 -> patrol_logs 적재
#   run_pipeline_rag.sh  UDP 수신 -> patrol_logs 적재
#                                 -> (위험 등급) 대응안 생성 -> response_plans 적재
#
#   기존 파일은 수정하지 않았습니다. 둘 중 하나만 실행하세요.
#   같은 UDP 포트(9998, 대시보드/DB용)를 쓰기 때문에 동시에 켜면 포트 충돌이 납니다.
#
# 사용법:
#   1. (최초 1회) set_env.sh 준비 — run_pipeline.sh와 동일합니다.
#        cp set_env.example.sh set_env.sh
#        nano set_env.sh
#   2. (최초 1회) 지식베이스 구성
#        python3 rag/setup_knowledge_base.py
#   3. 이후에는 이 파일만 실행하면 됩니다.
#        ./run_pipeline_rag.sh
#
#   벡터 검색까지 쓰려면 (메모리 여유가 있을 때만):
#        ./run_pipeline_rag.sh --use-embedding
#
#   화재·가스/유해물질 누출·연기 긴급 상황을 감지 즉시 5회 방송하려면:
#        ./run_pipeline_rag.sh --tts
#     -> 젯슨에서 "TTS Engine and pipeline/Rx_pipeline.py"가 먼저 떠 있어야 하고,
#        set_env.sh에 JETSON_IP를 실제 젯슨 주소로 넣어둬야 합니다.
#
#   대응안 재생성 간격(기본 30초)을 바꾸려면:
#        ./run_pipeline_rag.sh --suppress-seconds 60
#
#   CPU LLM 생성 중에도 UDP 수신을 계속하려면:
#        ./run_pipeline_rag.sh --use-llm --async-llm
#        ./run_pipeline_rag.sh --use-llm --async-llm --plan-queue-size 64
# ------------------------------------------------------------
cd "$(dirname "$0")"
if [ ! -f "set_env.sh" ]; then
    echo "set_env.sh가 없습니다. 먼저 아래처럼 준비해주세요:"
    echo "  cp set_env.example.sh set_env.sh"
    echo "  nano set_env.sh   (SFP_DB_PASSWORD, SFP_DB_HOST를 실제 값으로 수정)"
    exit 1
fi
source set_env.sh

# 프로젝트 가상환경이 있으면 파이프라인과 설치 점검이 같은 패키지를
# 보게 합니다. 필요하면 SFP_PYTHON으로 명시적으로 덮어쓸 수 있습니다.
if [ -n "${SFP_PYTHON:-}" ]; then
    PYTHON_BIN="$SFP_PYTHON"
elif [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python3"
fi

if ! "$PYTHON_BIN" -c "import psycopg2" >/dev/null 2>&1; then
    echo "psycopg2를 사용할 수 없습니다: $PYTHON_BIN"
    echo "  $PYTHON_BIN -m pip install -r rag/requirements.txt"
    exit 1
fi
# 고정 로봇은 A/B/C를 쓰고, 지도를 돌아다니는 실물 순찰 로봇은
# SFP_PATROL_ZONE=auto로 두면 미니맵의 현재 구역을 DB에 적재한다.
ZONE="${SFP_PATROL_ZONE:-A}"
echo "=================================================="
echo " 순찰 파이프라인을 시작합니다 (구역: $ZONE, 대응안 생성 포함)"
echo " DB 대상: $SFP_DB_HOST:$SFP_DB_PORT / $SFP_DB_NAME"
echo "=================================================="
echo ""
"$PYTHON_BIN" pipeline/patrol_pipeline_rag.py --zone "$ZONE" "$@"
pipeline_status=$?
echo ""
if [ -t 0 ]; then
    echo "파이프라인이 종료되었습니다. 아무 키나 누르면 창이 닫힙니다."
    read -n 1 -s
fi
exit "$pipeline_status"
