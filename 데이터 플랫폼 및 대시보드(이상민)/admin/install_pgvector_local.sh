#!/usr/bin/env bash
# PostgreSQL 14가 이 PC에서 돌고 있을 때 pgvector를 설치·활성화합니다.
# 시스템 파일 설치와 DB 확장 활성화에만 sudo를 사용합니다.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
PGVECTOR_SRC="$WORKSPACE_DIR/.runtime/pgvector-src"

cd "$PROJECT_DIR"

if [ ! -f set_env.sh ]; then
    echo "set_env.sh가 없습니다. DB 설정을 먼저 준비하세요."
    exit 1
fi
# shellcheck disable=SC1091
source ./set_env.sh >/dev/null

if [ ! -f /usr/include/postgresql/14/server/postgres.h ]; then
    echo "[1/5] PostgreSQL 14 개발 헤더 설치"
    sudo apt-get install -y postgresql-server-dev-14
else
    echo "[1/5] PostgreSQL 14 개발 헤더 이미 설치됨"
fi

if [ ! -d "$PGVECTOR_SRC/.git" ]; then
    echo "[2/5] pgvector 소스 다운로드"
    mkdir -p "$(dirname "$PGVECTOR_SRC")"
    git clone --depth 1 https://github.com/pgvector/pgvector.git "$PGVECTOR_SRC"
else
    echo "[2/5] 받아둔 pgvector 소스 사용"
fi

echo "[3/5] PostgreSQL 14용 pgvector 빌드·시스템 설치"
make -C "$PGVECTOR_SRC" PG_CONFIG=/usr/bin/pg_config
sudo make -C "$PGVECTOR_SRC" PG_CONFIG=/usr/bin/pg_config install

echo "[4/5] $SFP_DB_NAME DB에 vector 확장 활성화"
sudo -u postgres psql -d "$SFP_DB_NAME" \
    -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "[5/5] 벡터 컬럼·인덱스 구성 및 검증"
.venv/bin/python rag/check_pgvector.py
.venv/bin/python rag/check_pgvector.py --check

echo "pgvector 설치·활성화 완료"
