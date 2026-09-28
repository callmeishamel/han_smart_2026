#!/bin/bash
# ------------------------------------------------------------
# [호환용] Jetson 로컬 DB 적재 파이프라인 실행 스크립트
#
# Autopilot 기본 구성은 DB/RAG 파이프라인을 데이터 PC에서 실행합니다. 이 파일은
# Jetson 한 대에서 pipeline/jetson_patrol_pipeline.py까지 실행하는 구버전/단독 시연용이며,
# 비전·ROS2 브리지·TTS를 한 번에 시작하는 스크립트가 아닙니다. README.md를 확인하세요.
#
# 사용법:
#   1. (최초 1회) set_env.example.sh를 set_env.sh로 복사하고, 안의 값들을
#      이상민에게 받은 실제 비밀번호/IP로 채워넣으세요.
#        cp set_env.example.sh set_env.sh
#        nano set_env.sh
#   2. 이후에는 이 파일만 실행하면 됩니다.
#        ./run_pipeline.sh
# ------------------------------------------------------------

cd "$(dirname "$0")"

if [ ! -f "set_env.sh" ]; then
    echo "set_env.sh가 없습니다. 먼저 아래처럼 준비해주세요:"
    echo "  cp set_env.example.sh set_env.sh"
    echo "  nano set_env.sh   (SFP_DB_PASSWORD, SFP_DB_HOST를 이상민에게 받은 값으로 수정)"
    exit 1
fi

required_files=(
    "common/schema.py"
    "common/console.py"
    "pipeline/jetson_patrol_pipeline.py"
)
for required_file in "${required_files[@]}"; do
    if [ ! -f "$required_file" ]; then
        echo "필수 배포 파일이 없습니다: $required_file"
        echo "배포 안내의 원본 복사 단계를 먼저 실행해주세요."
        exit 1
    fi
done

source set_env.sh

ZONE="A"   # 이 Jetson이 담당하는 구역 (A / B / C)

echo "=================================================="
echo " Jetson 순찰 파이프라인을 시작합니다 (구역: $ZONE)"
echo " DB 대상: $SFP_DB_HOST:$SFP_DB_PORT / $SFP_DB_NAME"
echo "=================================================="
echo ""

python3 pipeline/jetson_patrol_pipeline.py --zone "$ZONE"
pipeline_status=$?

echo ""
echo "파이프라인이 종료되었습니다 (종료 코드: $pipeline_status)."
if [ -t 0 ]; then
    echo "아무 키나 누르면 창이 닫힙니다."
    read -n 1 -s
fi
exit "$pipeline_status"
