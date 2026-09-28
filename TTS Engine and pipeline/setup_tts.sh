#!/usr/bin/env bash
# 젯슨 TTS 준비 — 모델 내려받기 · USB 스피커 확인 · 실제 소리까지 검증.
#
# 왜 이 스크립트가 필요한가
# -------------------------
# TTS 는 "안 들린다" 는 증상 하나에 원인이 다섯 개쯤 됩니다. 모델이 없거나,
# .onnx 만 있고 .onnx.json 이 없거나, piper 가 안 깔렸거나, aplay 가 HDMI 로
# 내보내거나, USB 스피커 볼륨이 0 이거나. 각각을 따로 확인하지 않으면
# 어디가 문제인지 알 수 없습니다. 여기서 순서대로 하나씩 짚습니다.
#
# 사용법 (젯슨에서):
#     ./setup_tts.sh              # 전부 (모델 + 장치 + 소리 테스트)
#     ./setup_tts.sh --check      # 내려받지 않고 상태만 점검
#     ./setup_tts.sh --no-play    # 소리는 내지 않고 준비만

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Piper 공식 저장소(rhasspy/piper-voices)에 **존재하는 한국어 음성은 이것 하나**입니다.
# 예전 코드가 쓰던 ko_KR-hyeri-medium 은 그 저장소에 없습니다.
VOICE="${PIPER_VOICE:-ko_KR-kss-medium}"
VOICE_URL_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/ko/ko_KR/kss/medium"

MODEL_PATH="${PIPER_MODEL_PATH:-$SCRIPT_DIR/$VOICE.onnx}"
CONFIG_PATH="$MODEL_PATH.json"

DO_DOWNLOAD=1
DO_PLAY=1
for arg in "$@"; do
  case "$arg" in
    --check)   DO_DOWNLOAD=0; DO_PLAY=0 ;;
    --no-play) DO_PLAY=0 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "알 수 없는 인자: $arg" >&2; exit 2 ;;
  esac
done

problems=0
step() { echo; echo "── $* ─────────────────────────────"; }
ok()   { echo "  ✅ $*"; }
warn() { echo "  ⚠  $*"; }
bad()  { echo "  ❌ $*"; problems=$((problems + 1)); }

echo "=================================================="
echo " 젯슨 TTS 준비"
echo " 음성 모델: $VOICE"
echo " 모델 경로: $MODEL_PATH"
echo "=================================================="

# ──────────────────────────────────────────────────────────────
step "1. 필요한 명령"
# ──────────────────────────────────────────────────────────────
for cmd in piper aplay; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd 있음"
  else
    bad "$cmd 없음"
    case "$cmd" in
      piper) echo "       설치: pip3 install piper-tts" ;;
      aplay) echo "       설치: sudo apt install -y alsa-utils" ;;
    esac
  fi
done

DOWNLOADER=""
if command -v curl >/dev/null 2>&1; then DOWNLOADER="curl"
elif command -v wget >/dev/null 2>&1; then DOWNLOADER="wget"
fi
if [[ -z "$DOWNLOADER" && $DO_DOWNLOAD -eq 1 ]]; then
  bad "curl 도 wget 도 없어 모델을 받을 수 없습니다"
fi

# ──────────────────────────────────────────────────────────────
step "2. 음성 모델"
# ──────────────────────────────────────────────────────────────
# piper 는 <모델>.onnx 와 그 옆의 <모델>.onnx.json 을 **둘 다** 읽습니다.
# .onnx 만 받아 두면 합성이 실패하는데, 에러만 봐서는 원인을 알기 어렵습니다.
download() {   # $1=URL  $2=저장경로
  echo "  내려받는 중: $(basename "$2")"
  if [[ "$DOWNLOADER" == "curl" ]]; then
    curl -fL --progress-bar -o "$2.part" "$1" || return 1
  else
    wget -q --show-progress -O "$2.part" "$1" || return 1
  fi
  mv -f "$2.part" "$2"
}

need_model=0
[[ -s "$MODEL_PATH"  ]] || need_model=1
[[ -s "$CONFIG_PATH" ]] || need_model=1

if [[ $need_model -eq 0 ]]; then
  ok "모델과 설정이 이미 있습니다 ($(du -h "$MODEL_PATH" | cut -f1))"
elif [[ $DO_DOWNLOAD -eq 0 ]]; then
  bad "모델이 없습니다 (--check 라 받지 않았습니다)"
  echo "       받으려면: ./setup_tts.sh"
elif [[ -n "$DOWNLOADER" ]]; then
  # 약 63MB. 네트워크가 느리면 몇 분 걸립니다.
  if download "$VOICE_URL_BASE/$VOICE.onnx" "$MODEL_PATH" \
     && download "$VOICE_URL_BASE/$VOICE.onnx.json" "$CONFIG_PATH"; then
    ok "모델 준비 완료 ($(du -h "$MODEL_PATH" | cut -f1))"
  else
    bad "모델을 받지 못했습니다 (네트워크 또는 URL 확인)"
    rm -f "$MODEL_PATH.part" "$CONFIG_PATH.part"
  fi
fi

if [[ -s "$MODEL_PATH" && -s "$CONFIG_PATH" ]]; then
  echo
  echo "  ※ 이 음성(kss)은 CC BY-NC-SA 4.0 — **비상업적 사용만** 허용됩니다."
  echo "    경진대회·연구·시연은 출처를 밝히면 되지만, 상용화 시에는 다른 음성이"
  echo "    필요합니다. https://huggingface.co/rhasspy/piper-voices"
fi

# ──────────────────────────────────────────────────────────────
step "3. 출력 장치 (USB 스피커)"
# ──────────────────────────────────────────────────────────────
if command -v aplay >/dev/null 2>&1; then
  echo "  aplay -l 결과:"
  aplay -l 2>/dev/null | sed 's/^/    /' || true
  echo

  # 장치 선택 로직은 Rx_pipeline.py 안에 있습니다. 여기서 그걸 그대로 불러
  # 씁니다 — 두 곳에 같은 규칙을 따로 쓰면 조용히 갈라집니다.
  DEVICE="$(SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PY' 2>/dev/null
import os, subprocess, sys
# 장치 선택 규칙은 Rx_pipeline.py 한 곳에만 둡니다.
sys.path.insert(0, os.environ["SCRIPT_DIR"])
try:
    import Rx_pipeline as rx
except Exception:
    sys.exit(1)
device = rx.detect_playback_device()
if not device:
    sys.exit(1)
print(device)
PY
)"

  if [[ -n "$DEVICE" ]]; then
    ok "고른 장치: $DEVICE"
    if [[ "$DEVICE" == *HDMI* || "$DEVICE" == *hdmi* ]]; then
      warn "HDMI 로 보입니다. USB 스피커가 꽂혀 있는지 확인하세요."
    fi
    echo
    echo "    이 값을 고정하고 싶으면:"
    echo "      export APLAY_DEVICE=$DEVICE"
    echo "    (카드 **번호**(plughw:1,0) 대신 이름을 쓰는 이유: USB 는 재부팅이나"
    echo "     재연결로 번호가 바뀝니다. 이름은 그대로입니다.)"
  else
    bad "쓸 만한 출력 장치를 못 찾았습니다"
    echo "       USB 스피커를 꽂고 다시 실행하세요. lsusb 로도 확인됩니다."
  fi
else
  bad "aplay 가 없어 장치를 확인할 수 없습니다"
fi

# ──────────────────────────────────────────────────────────────
if [[ $DO_PLAY -eq 1 ]]; then
step "4. 실제로 소리가 나는지"
# ──────────────────────────────────────────────────────────────
  if [[ $problems -gt 0 ]]; then
    warn "앞 단계에 문제가 있어 소리 테스트를 건너뜁니다."
  else
    TEST_WAV="$(mktemp -t tts_setup_XXXXXX.wav)"
    TEST_TEXT="테스트입니다. 순찰 로봇 음성 안내가 정상 동작합니다."

    if echo "$TEST_TEXT" | piper --model "$MODEL_PATH" --output_file "$TEST_WAV" 2>/dev/null \
       && [[ -s "$TEST_WAV" ]]; then
      ok "합성 성공 ($(du -h "$TEST_WAV" | cut -f1))"
      echo "  재생합니다 — 스피커에서 들리는지 확인하세요."
      if [[ -n "${DEVICE:-}" ]]; then
        aplay -q -D "$DEVICE" "$TEST_WAV" && ok "재생 명령 정상 종료" \
          || bad "재생 실패 — 볼륨(alsamixer)과 연결을 확인하세요"
      else
        aplay -q "$TEST_WAV" && ok "재생 명령 정상 종료(기본 장치)" || bad "재생 실패"
      fi
      echo
      echo "  소리가 안 들렸다면:"
      echo "    - alsamixer 로 볼륨 확인 (M 키로 음소거 해제)"
      echo "    - 다른 장치로: aplay -D plughw:CARD=<다른카드>,DEV=0 $TEST_WAV"
    else
      bad "Piper 합성 실패 — 모델(.onnx + .onnx.json)과 piper 설치를 확인하세요"
    fi
    rm -f "$TEST_WAV"
  fi
fi

# ──────────────────────────────────────────────────────────────
echo
echo "=================================================="
if [[ $problems -eq 0 ]]; then
  echo " ✅ 준비 완료. 수신기를 띄우세요:"
  echo "      python3 Rx_pipeline.py"
  echo
  echo " 보내는 쪽(상민님 PC)에서 확인:"
  echo "      JETSON_IP=<젯슨IP> python3 Rag_to_Jetson.py \"테스트 방송입니다\""
  exit 0
else
  echo " ❌ 문제 $problems 건 — 위 ❌ 항목을 먼저 해결하세요."
  exit 1
fi
