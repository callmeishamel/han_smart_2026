#!/usr/bin/env bash
# =============================================================================
# setup_linux.sh — RAG 대응안 생성 파트 설치 도우미 (Linux)
#
# 하는 일
#   Python 패키지, Ollama, gemma2:2b, pgvector 를 확인하고 빠진 것만 설치합니다.
#   이미 되어 있는 것은 건너뜁니다. 여러 번 실행해도 안전합니다.
#
# 하지 않는 일
#   DB 비밀번호를 만들거나 고치지 않습니다. set_env.sh 는 직접 작성하십시오.
#   지식베이스 적재와 임베딩 적재는 하지 않습니다. 설치가 끝난 뒤
#   안내되는 명령을 실행하십시오.
#
# 사용법
#   ./setup_linux.sh --check     무엇이 되어 있고 무엇이 빠졌는지만 출력
#   ./setup_linux.sh             빠진 것을 설치 (단계마다 확인)
#   ./setup_linux.sh --yes       확인 없이 진행
#
#   ./setup_linux.sh --skip-pgvector    pgvector 를 건너뜀
#   ./setup_linux.sh --skip-ollama      Ollama 를 건너뜀
#
# 셋 다 없어도 대응안은 생성됩니다. 검색 정확도와 문장 품질을 올리는
# 단계입니다.
# =============================================================================

set -uo pipefail

CHECK_ONLY=0
ASSUME_YES=0
SKIP_PGVECTOR=0
SKIP_OLLAMA=0

for arg in "$@"; do
    case "$arg" in
        --check)          CHECK_ONLY=1 ;;
        --yes|-y)         ASSUME_YES=1 ;;
        --skip-pgvector)  SKIP_PGVECTOR=1 ;;
        --skip-ollama)    SKIP_OLLAMA=1 ;;
        --help|-h)        sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "알 수 없는 옵션: $arg (--help 참조)"; exit 1 ;;
    esac
done

cd "$(dirname "$0")" || exit 1

BAR="======================================================================"
MISSING=()
READY=()

say()  { printf '%s\n' "$*"; }
ok()   { READY+=("$1");   printf '  [완료] %s\n' "$1"; }
miss() { MISSING+=("$1"); printf '  [없음] %s\n' "$1"; }
warn() { printf '  [주의] %s\n' "$1"; }
step() { printf '\n %s\n %s\n' "$1" "----------------------------------------------------------------------"; }

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    printf '  %s [y/N] ' "$1"
    read -r reply </dev/tty 2>/dev/null || return 1
    [[ "$reply" =~ ^[Yy]$ ]]
}

have() { command -v "$1" >/dev/null 2>&1; }

# 실행 스크립트와 설치 점검이 같은 Python 환경을 보게 합니다.
# SFP_PYTHON으로 명시할 수 있고, 기본은 프로젝트 .venv 우선입니다.
if [ -n "${SFP_PYTHON:-}" ]; then
    PYTHON_BIN="$SFP_PYTHON"
elif [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python3"
fi

# --- 패키지 관리자 판별 -------------------------------------------------------
if have dnf;      then PKG=dnf
elif have apt;    then PKG=apt
else                   PKG=""
fi

pkg_install() {
    case "$PKG" in
        dnf) sudo dnf install -y "$@" ;;
        apt) sudo apt install -y "$@" ;;
        *)   warn "패키지 관리자를 찾지 못했습니다. 직접 설치하십시오: $*"
             return 1 ;;
    esac
}

say "$BAR"
say " RAG 대응안 생성 파트 설치 도우미"
say "$BAR"
say " 위치: $(pwd)"
[ "$CHECK_ONLY" -eq 1 ] && say " 모드: 확인만 (아무것도 설치하지 않습니다)"

# =============================================================================
# 0. 기본 환경
# =============================================================================
step "[0] 기본 환경"

if [ -x "$PYTHON_BIN" ] || have "$PYTHON_BIN"; then
    ok "Python: $PYTHON_BIN ($($PYTHON_BIN -V 2>&1 | cut -d' ' -f2))"
else miss "python3 — 먼저 설치하십시오"; fi

if have pip3; then ok "pip3"
else
    miss "pip3"
    if [ "$CHECK_ONLY" -eq 0 ] && confirm "pip3 를 설치할까요?"; then
        case "$PKG" in
            dnf) pkg_install python3-pip ;;
            apt) pkg_install python3-pip ;;
        esac
    fi
fi

if [ -f set_env.sh ]; then
    ok "set_env.sh 있음"
    # shellcheck disable=SC1091
    source ./set_env.sh 2>/dev/null || warn "set_env.sh 를 읽는 중 오류"
else
    miss "set_env.sh 없음"
    say ""
    say "  DB 접속 정보 파일이 없습니다. 먼저 만드십시오."
    say "    cp set_env.example.sh set_env.sh"
    say "    nano set_env.sh        # SFP_DB_PASSWORD, SFP_DB_HOST 입력"
    say ""
    say "  없어도 Python 패키지와 Ollama 설치는 진행됩니다."
fi

# =============================================================================
# 1. Python 패키지
# =============================================================================
step "[1] Python 패키지"

if "$PYTHON_BIN" -c "import psycopg2" 2>/dev/null; then
    ok "psycopg2 (필수)"
else
    miss "psycopg2 (필수)"
fi
"$PYTHON_BIN" -c "import sentence_transformers" 2>/dev/null \
    && ok "sentence-transformers (선택, 벡터 검색용)" \
    || miss "sentence-transformers (선택, 벡터 검색용)"
"$PYTHON_BIN" -c "import pgvector" 2>/dev/null \
    && ok "pgvector 파이썬 패키지 (선택)" \
    || miss "pgvector 파이썬 패키지 (선택)"

if [ "$CHECK_ONLY" -eq 0 ]; then
    if ! "$PYTHON_BIN" -c "import psycopg2" 2>/dev/null \
       || ! "$PYTHON_BIN" -c "import sentence_transformers" 2>/dev/null; then
        say ""
        if confirm "rag/requirements.txt 를 설치할까요? (약 2~3분)"; then
            "$PYTHON_BIN" -m pip install -r rag/requirements.txt \
                || warn "설치 실패 — 가상환경 사용을 검토하십시오"
        fi
    fi
fi

# =============================================================================
# 2. Ollama + gemma2:2b
# =============================================================================
if [ "$SKIP_OLLAMA" -eq 0 ]; then
step "[2] Ollama + gemma2:2b (문장 생성, 선택)"

if have ollama; then
    ok "ollama 설치됨"
    if ollama list 2>/dev/null | grep -q "gemma2:2b"; then
        ok "gemma2:2b 모델 있음"
    else
        miss "gemma2:2b 모델"
        if [ "$CHECK_ONLY" -eq 0 ] && confirm "gemma2:2b 를 내려받을까요? (약 1.6GB)"; then
            ollama pull gemma2:2b || warn "모델 내려받기 실패"
        fi
    fi
else
    miss "ollama"
    if [ "$CHECK_ONLY" -eq 0 ]; then
        say ""
        say "  Ollama 는 CPU 에서도 GPU 에서도 돌아갑니다."
        say "  설치 스크립트가 systemd 서비스까지 등록합니다."
        if confirm "Ollama 를 설치할까요?"; then
            curl -fsSL https://ollama.com/install.sh | sh || warn "Ollama 설치 실패"
            if have ollama && confirm "gemma2:2b 를 내려받을까요? (약 1.6GB)"; then
                ollama pull gemma2:2b || warn "모델 내려받기 실패"
            fi
        fi
    fi
fi

if have ollama; then
    if curl -fsS --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
        ok "Ollama 서버 응답 정상 (:11434)"
    else
        warn "Ollama 서버가 응답하지 않습니다: sudo systemctl start ollama"
    fi
fi
fi

# =============================================================================
# 3. pgvector
# =============================================================================
if [ "$SKIP_PGVECTOR" -eq 0 ]; then
step "[3] pgvector (벡터 검색, 선택)"

say "  pgvector 는 PostgreSQL 서버 측 확장입니다."
say "  DB 가 도는 PC 에서만 설치됩니다. 이 PC 에 DB 가 없으면 건너뛰십시오."
say ""

PGCONFIG=""
if have pg_config; then
    PGCONFIG="$(command -v pg_config)"
else
    for c in /usr/pgsql-*/bin/pg_config /usr/lib/postgresql/*/bin/pg_config; do
        [ -x "$c" ] && PGCONFIG="$c" && break
    done
fi

if have psql; then ok "psql 있음"; else miss "psql — 이 PC 에 DB 가 없을 수 있습니다"; fi

EXT_STATE="unknown"
if have psql && [ -n "${SFP_DB_NAME:-}" ]; then
    Q="SELECT 1 FROM pg_extension WHERE extname='vector'"
    if PGPASSWORD="${SFP_DB_PASSWORD:-}" psql -qtAX \
        -h "${SFP_DB_HOST:-localhost}" -p "${SFP_DB_PORT:-5432}" \
        -U "${SFP_DB_USER:-postgres}" -d "$SFP_DB_NAME" \
        -c "$Q" 2>/dev/null | grep -q 1; then
        ok "vector 확장 활성화됨 ($SFP_DB_NAME)"
        EXT_STATE="enabled"
    else
        miss "vector 확장 ($SFP_DB_NAME 에 없음)"
        EXT_STATE="absent"
    fi
else
    warn "DB 접속 정보가 없어 확장 상태를 확인하지 못했습니다"
fi

if [ "$EXT_STATE" != "enabled" ] && [ "$CHECK_ONLY" -eq 0 ]; then
    if [ -z "$PGCONFIG" ]; then
        miss "pg_config — 개발 헤더가 없습니다"
        say ""
        say "  이걸 빠뜨리면 make 에서 pgxs.mk 오류가 납니다."
        case "$PKG" in
            dnf) say "    sudo dnf install -y postgresql\$(psql -V | grep -oE '[0-9]+' | head -1)-devel" ;;
            apt) say "    sudo apt install -y postgresql-server-dev-\$(psql -V | grep -oE '[0-9]+' | head -1) build-essential" ;;
            *)   say "    개발 헤더 패키지를 설치하십시오" ;;
        esac
        say ""
        say "  설치 후 이 스크립트를 다시 실행하십시오."
    else
        ok "pg_config: $PGCONFIG"
        say ""
        if confirm "pgvector 를 소스에서 빌드해 설치할까요? (sudo 필요)"; then
            TMP="$(mktemp -d)"
            (
                cd "$TMP" || exit 1
                git clone --depth 1 https://github.com/pgvector/pgvector.git \
                    && cd pgvector \
                    && PG_CONFIG="$PGCONFIG" make \
                    && sudo PG_CONFIG="$PGCONFIG" make install
            ) && ok "pgvector 빌드·설치 완료" || warn "pgvector 설치 실패"
            rm -rf "$TMP"

            if [ -n "${SFP_DB_NAME:-}" ] && confirm "$SFP_DB_NAME 에 확장을 활성화할까요?"; then
                PGPASSWORD="${SFP_DB_PASSWORD:-}" psql \
                    -h "${SFP_DB_HOST:-localhost}" -p "${SFP_DB_PORT:-5432}" \
                    -U "${SFP_DB_USER:-postgres}" -d "$SFP_DB_NAME" \
                    -c "CREATE EXTENSION IF NOT EXISTS vector;" \
                    && ok "확장 활성화 완료" \
                    || warn "확장 활성화 실패 — DB 사용자에게 권한이 필요합니다"
            fi
        fi
    fi
fi
fi

# =============================================================================
# 요약
# =============================================================================
say ""
say "$BAR"
say " 결과: 완료 ${#READY[@]}건 / 미설치 ${#MISSING[@]}건"
say "$BAR"

if [ "${#MISSING[@]}" -gt 0 ]; then
    say ""
    say " 아직 없는 것"
    for m in "${MISSING[@]}"; do say "   - $m"; done
fi

cat <<'NEXT'

 다음에 할 일

   1) DB 접속 정보를 아직 안 넣었다면
        cp set_env.example.sh set_env.sh
        nano set_env.sh
        source set_env.sh

   2) 지식베이스 적재 (필수, 한 번만)
        python3 rag/setup_knowledge_base.py

   3) 벡터 검색을 쓸 경우
        python3 rag/check_pgvector.py       컬럼·인덱스 자동 추가
        python3 rag/embed_chunks.py --search   BGE-M3 약 2GB 자동 내려받음

   4) LLM 을 쓸 경우
        python3 rag/check_llm.py

   5) 실행
        ./run_pipeline_rag.sh                            기본
        ./run_pipeline_rag.sh --use-llm --async-llm      LLM 사용

 2번까지만 해도 대응안은 생성됩니다. 3·4번은 품질을 올리는 단계입니다.
 막히면 docs/설치가이드_리눅스.md 의 "자주 막히는 지점" 을 보십시오.
NEXT
