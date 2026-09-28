"""
Rx_pipeline.py — 젯슨 TTS 수신기 (현장 음성 경보)

역할
----
이상민/정진철 PC에서 생성한 대응안 문장을 TCP로 받아, 젯슨에 연결된 스피커로
Piper TTS 음성 출력합니다. 전체 파이프라인에서 사람에게 직접 닿는 유일한 출력입니다.

    [RAG 대응안] --TCP :9997/queued ACK--> [우선순위·TTL 큐] --> Piper --> aplay --> 스피커

보내는 쪽은 같은 폴더의 Rag_to_Jetson.py 입니다. 포트는 양쪽이 같은 환경변수
(TTS_TCP_PORT)를 읽습니다 — 한쪽만 바꾸면 조용히 끊기는 사고를 막기 위한
저장소 공통 규칙입니다(루트 README "환경변수 요약" 참고).

실행
----
    export PIPER_MODEL_PATH=/path/to/ko_KR-kss-medium.onnx   # 생략 시 스크립트 옆
    python3 Rx_pipeline.py

사전 조건: piper CLI와 aplay(alsa-utils)가 설치되어 있어야 합니다.
    piper --model <모델> --output_file test.wav <<< "테스트"
    aplay test.wav
"""

from dataclasses import dataclass
try:
    import audioop
except ImportError:  # Python 3.13+ 폴백. Jetson 22.04/Python 3.10에는 있습니다.
    audioop = None
import hmac
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tts_receiver")


# ==========================================
# 설정
# ==========================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 모델 경로를 상대경로로 두면 실행 디렉터리가 바뀌는 순간 못 찾습니다.
# 스크립트 자기 위치를 기준으로 잡고, 환경변수로 덮어쓸 수 있게 합니다.
#
# 기본값이 ko_KR-kss-medium 인 이유: Piper 공식 저장소(rhasspy/piper-voices)에
# **존재하는 한국어 음성이 이것 하나뿐**입니다. 예전 기본값이던
# ko_KR-hyeri-medium 은 그 저장소에 없어서, 그대로 두면 setup_tts.sh 로 받아도
# 파일명이 안 맞아 무음이 됩니다.
#
# 라이선스 주의: kss 는 CC BY-NC-SA 4.0(비상업적)입니다. 경진대회·연구·시연은
# 출처를 밝히면 되지만, 상용화 시에는 다른 음성을 학습하거나 구해야 합니다.
MODEL_PATH = os.environ.get(
    "PIPER_MODEL_PATH", os.path.join(_SCRIPT_DIR, "ko_KR-kss-medium.onnx"))

# 절대 경로로 가상환경 Python을 실행하면 그 환경의 bin 디렉터리가 PATH에
# 포함되지 않을 수 있습니다. 이 경우 같은 환경에 설치된 piper를 먼저 찾고,
# 운영자가 PIPER_BIN으로 지정한 경로를 최우선으로 사용합니다.
_venv_piper = os.path.join(os.path.dirname(sys.executable), "piper")
_user_piper = os.path.expanduser("~/.local/bin/piper")
PIPER_BIN = os.environ.get("PIPER_BIN", "").strip() or \
    shutil.which("piper") or \
    (_user_piper if os.access(_user_piper, os.X_OK) else "") or \
    (_venv_piper if os.access(_venv_piper, os.X_OK) else "piper")

# 보내는 쪽(Rag_to_Jetson.py)과 같은 환경변수를 읽습니다.
TTS_PORT = int(os.environ.get("TTS_TCP_PORT", "9997"))
TTS_BIND_IP = os.environ.get("TTS_BIND_IP", "0.0.0.0")

# ALSA 출력 장치.
#
# **비우면 USB 스피커를 자동으로 찾습니다.** 젯슨은 기본 출력이 HDMI 로 잡히는
# 경우가 많아, USB 스피커를 꽂아도 소리가 안 나면서 aplay 는 성공으로 끝납니다
# (모니터 스피커로 나가거나, HDMI 에 아무것도 안 붙어 있으면 그냥 사라집니다).
# 그래서 지정하지 않으면 detect_playback_device() 가 aplay -l 을 읽어 USB 카드를
# 고릅니다. 자세한 사정은 그 함수 주석 참고.
#
# 직접 지정하고 싶으면 `aplay -l` 로 확인 후 넣으세요. 이때 **카드 번호(plughw:1,0)
# 보다 카드 이름(plughw:CARD=Device,DEV=0)을 권합니다** — USB 는 재부팅이나
# 재연결로 번호가 바뀝니다.
APLAY_DEVICE = os.environ.get("APLAY_DEVICE", "").strip()

# 대시보드의 "스피커 출력" 슬라이더가 제어할 ALSA mixer 설정입니다.
# 비워두면 현재 재생 장치(APLAY_DEVICE)에서 Master → Speaker → PCM 순으로
# 실제 볼륨 컨트롤을 찾아 씁니다. USB DAC마다 control 이름이 달라 운영자가
# 확정하고 싶으면 예: TTS_MIXER_CONTROL=Speaker 로 지정할 수 있습니다.
TTS_MIXER_DEVICE = os.environ.get("TTS_MIXER_DEVICE", "").strip()
TTS_MIXER_CONTROL = os.environ.get("TTS_MIXER_CONTROL", "").strip()

# 자동 탐색을 끄고 싶을 때 (기본 장치를 그대로 쓰고 싶은 경우).
APLAY_AUTODETECT = os.environ.get("APLAY_AUTODETECT", "1").strip() not in ("0", "false", "False")

# recommended_action은 VARCHAR(500). 한글 1자가 UTF-8로 3바이트이므로
# 500자 = 최대 1500바이트. JSON 래핑까지 감안해 넉넉히 잡습니다.
MAX_TEXT_CHARS = 500
MAX_PAYLOAD_BYTES = 8192

RECV_TIMEOUT_SECONDS = 5.0      # 요청 하나를 다 받는 데 허용할 "전체" 시간
try:
    # 긴급 방송 5회가 수신 직후 잘리지 않도록 최소 5건을 보장합니다.
    SPEAK_QUEUE_SIZE = max(5, int(os.environ.get("TTS_SPEAK_QUEUE_SIZE", "8")))
except ValueError:
    SPEAK_QUEUE_SIZE = 8
PIPER_TIMEOUT_SECONDS = 30
APLAY_TIMEOUT_SECONDS = 60

# 동시에 처리할 연결 수. 예전에는 accept 루프가 연결을 하나씩 직렬로 처리해서,
# 느린 클라이언트 하나가 붙어 있으면 그동안 도착한 **진짜 경보를 아예 받지
# 못했습니다**(송신 측은 TCP backlog 에 들어갔으므로 "송신 성공"을 찍습니다).
MAX_CONCURRENT_CONNECTIONS = int(os.environ.get("TTS_MAX_CONNECTIONS", "8"))

# 공유 비밀. 설정하면 이 값이 실린 요청만 재생합니다.
# 보내는 쪽(Rag_to_Jetson.py)이 같은 환경변수를 읽습니다.
TTS_TOKEN = os.environ.get("TTS_TOKEN", "").strip()

# 쉼표로 구분한 허용 IP 목록. 비우면 제한하지 않습니다.
TTS_ALLOWED_IPS = {
    ip.strip() for ip in os.environ.get("TTS_ALLOWED_IPS", "").split(",") if ip.strip()
}

MESSAGE_TTL_SECONDS = float(os.environ.get("TTS_MESSAGE_TTL_SEC", "20"))
MESSAGE_STATUS_RETENTION_SECONDS = float(
    os.environ.get("TTS_STATUS_RETENTION_SEC", "300"))
MESSAGE_STATUS_MAX_ENTRIES = max(
    1, int(os.environ.get("TTS_STATUS_MAX_ENTRIES", "4096")))
try:
    TTS_VOLUME_MAX_PERCENT = max(
        100, min(400, int(os.environ.get("TTS_VOLUME_MAX_PERCENT", "400"))))
except ValueError:
    TTS_VOLUME_MAX_PERCENT = 400
try:
    DEFAULT_VOLUME_PERCENT = max(
        0, min(TTS_VOLUME_MAX_PERCENT,
               int(os.environ.get("TTS_VOLUME_PERCENT", "140"))))
except ValueError:
    DEFAULT_VOLUME_PERCENT = 140


@dataclass(frozen=True)
class SpeechRequest:
    """송신 ACK·우선순위·만료 판단에 필요한 한 건의 방송 요청."""

    message_id: str
    text: str
    token: str = ""
    priority: int = 50
    created_at: float = 0.0
    expires_at: float = 0.0
    zone: str = ""
    detected_object: str = ""
    volume_percent: int = DEFAULT_VOLUME_PERCENT
    command: str = ""
    speaker_volume_percent: int = 100

    def expired(self, now: float = None) -> bool:
        return bool(self.expires_at) and (
            now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True)
class QueueResult:
    status: str
    dropped_message_id: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == "queued"


class SpeechPriorityQueue:
    """우선순위가 높은 경보를 먼저 꺼내고, 같은 등급에서는 오래된 것부터 처리."""

    def __init__(self, maxsize: int):
        self.maxsize = max(1, int(maxsize))
        self._items = []
        self._condition = threading.Condition()

    def _purge_expired(self, now: float) -> None:
        expired = [item for item in self._items if item.expired(now)]
        self._items[:] = [item for item in self._items if not item.expired(now)]
        for item in expired:
            _remember_message_status(item.message_id, "expired")

    def put_request(self, request: SpeechRequest) -> QueueResult:
        now = time.time()
        if request.expired(now):
            return QueueResult("expired")
        with self._condition:
            self._purge_expired(now)
            dropped_id = ""
            if len(self._items) >= self.maxsize:
                # 가장 낮은 우선순위, 같은 등급이면 가장 오래된 요청이 교체 후보입니다.
                worst = min(
                    self._items, key=lambda item: (item.priority, item.created_at))
                if (request.priority, request.created_at) <= (
                        worst.priority, worst.created_at):
                    return QueueResult("dropped")
                self._items.remove(worst)
                dropped_id = worst.message_id
            self._items.append(request)
            self._condition.notify()
            return QueueResult("queued", dropped_id)

    def get(self, timeout: float = None) -> SpeechRequest:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                self._purge_expired(time.time())
                if self._items:
                    # 우선순위가 높은 것부터, 같은 등급에서는 먼저 온 것부터.
                    request = max(
                        self._items,
                        key=lambda item: (item.priority, -item.created_at))
                    self._items.remove(request)
                    return request
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)

    def get_nowait(self) -> SpeechRequest:
        return self.get(timeout=0)

    def empty(self) -> bool:
        return self.qsize() == 0

    def qsize(self) -> int:
        with self._condition:
            self._purge_expired(time.time())
            return len(self._items)

    def task_done(self) -> None:
        # 현재 워커는 join()을 사용하지 않으므로 Queue 호환 인터페이스만 제공합니다.
        return None


# ==========================================
# 출력 장치 찾기 — USB 스피커
# ==========================================
# 젯슨에서 USB 스피커를 쓸 때 걸리는 것이 세 가지 있습니다.
#
# 1. **기본 출력이 HDMI 입니다.** USB 스피커를 꽂아도 aplay 는 HDMI 로 내보내고
#    성공(코드 0)으로 끝납니다. 소리만 안 납니다 — 가장 찾기 어려운 증상입니다.
# 2. **카드 번호가 고정이 아닙니다.** plughw:1,0 으로 박아두면 재부팅이나 USB
#    재연결로 번호가 바뀌는 순간 엉뚱한 장치로 나가거나 실패합니다. 그래서
#    번호 대신 **카드 이름**(plughw:CARD=Device,DEV=0)을 씁니다.
# 3. **하드웨어 파라미터가 안 맞습니다.** Piper 는 22050Hz 모노를 내놓는데 값싼
#    USB 스피커는 44100/48000Hz 만 받는 것이 많습니다. `hw:` 로 직접 열면
#    "Sample format non available" 로 실패하므로, 리샘플링을 해 주는
#    **`plughw:`** 를 씁니다.
_APLAY_LINE = re.compile(
    r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+): (.*?) \[([^\]]*)\]")

# 카드 설명에 이 말이 들어 있으면 USB 오디오로 봅니다.
_USB_HINTS = ("usb", "headset", "speaker", "audio device")
# 이 말이 들어 있으면 마지막 순위로 밀어냅니다 (젯슨 온보드 HDMI).
_LAST_RESORT_HINTS = ("hdmi", "hda", "tegra")
_PULSE_SINK_PREFIX = "pulse://"


def parse_aplay_devices(text: str):
    """`aplay -l` 출력을 [{card, card_id, card_desc, device, device_desc}] 로.

    파싱만 하는 순수 함수라 aplay 없이 테스트할 수 있습니다.
    """
    devices = []
    for line in text.splitlines():
        match = _APLAY_LINE.match(line.strip())
        if not match:
            continue
        devices.append({
            "card": int(match.group(1)),
            "card_id": match.group(2),
            "card_desc": match.group(3),
            "device": int(match.group(4)),
            "device_desc": match.group(6),
        })
    return devices


def _device_rank(entry) -> int:
    """작을수록 우선. USB 로 보이면 0, 보통은 1, HDMI/온보드는 2."""
    blob = f"{entry['card_id']} {entry['card_desc']} {entry['device_desc']}".lower()
    if any(hint in blob for hint in _USB_HINTS):
        return 0
    if any(hint in blob for hint in _LAST_RESORT_HINTS):
        return 2
    return 1


def pick_playback_device(devices):
    """USB 스피커로 보이는 것을 우선해서 하나 고릅니다. 없으면 None."""
    if not devices:
        return None
    # 같은 순위면 카드 번호가 작은 것(먼저 잡힌 것)을 씁니다.
    return sorted(devices, key=lambda e: (_device_rank(e), e["card"], e["device"]))[0]


def format_alsa_device(entry) -> str:
    """ALSA 장치 문자열. **번호가 아니라 이름**으로 만듭니다.

    plughw:CARD=Device,DEV=0 형태라 USB 를 다시 꽂아 카드 번호가 바뀌어도
    같은 스피커를 가리킵니다.
    """
    return f"plughw:CARD={entry['card_id']},DEV={entry['device']}"


def pulse_server_available() -> bool:
    """현재 사용자 PulseAudio 서버 소켓이 있으면 True.

    데스크톱에서는 PulseAudio가 실제 ALSA 장치를 이미 점유하는 것이 정상입니다.
    이때 plughw를 직접 열면 ``Device or resource busy``가 되므로 공유 가능한
    ``pulse`` ALSA 플러그인을 사용합니다. 헤드리스 Jetson처럼 서버가 없으면
    기존 USB 하드웨어 자동 선택으로 내려갑니다.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime_dir and hasattr(os, "getuid"):
        runtime_dir = f"/run/user/{os.getuid()}"
    return bool(runtime_dir) and os.path.exists(
        os.path.join(runtime_dir, "pulse", "native"))


def find_pulse_usb_sink():
    """PulseAudio의 USB 스피커 sink 이름. 없거나 조회 실패하면 None.

    ``aplay -D pulse``만 쓰면 PulseAudio *기본* sink로 갑니다. Jetson에서는
    USB 스피커가 꽂혀 있어도 기본 sink가 내장 아날로그/HDMI인 경우가 있어,
    TTS가 작게 들리거나 엉뚱한 출력으로 가는 원인이 됩니다.
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "sinks", "short"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("pactl sink 조회 실패 (%s). PulseAudio 기본 출력을 씁니다.", exc)
        return None

    candidates = []
    for line in result.stdout.decode("utf-8", "ignore").splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        sink_name = fields[1].strip()
        if not sink_name:
            continue
        blob = " ".join(fields[1:]).lower()
        if any(hint in blob for hint in _USB_HINTS):
            candidates.append(sink_name)
    return sorted(candidates)[0] if candidates else None


def detect_playback_device():
    """공유 오디오 서버 또는 쓸 만한 ALSA 출력 장치를 돌려줍니다."""
    if pulse_server_available():
        pulse_sink = find_pulse_usb_sink()
        if pulse_sink:
            logger.info("출력 장치: pulse USB sink %s", pulse_sink)
            return _PULSE_SINK_PREFIX + pulse_sink
        logger.info("출력 장치: pulse (공유 PulseAudio 기본 sink)")
        return "pulse"

    try:
        result = subprocess.run(["aplay", "-l"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("aplay -l 실행 실패 (%s). 기본 출력 장치를 씁니다.", exc)
        return None

    devices = parse_aplay_devices(result.stdout.decode("utf-8", "ignore"))
    if not devices:
        logger.warning("재생 장치를 하나도 찾지 못했습니다. "
                       "스피커 연결과 `aplay -l` 출력을 확인하세요.")
        return None

    chosen = pick_playback_device(devices)
    device = format_alsa_device(chosen)
    if _device_rank(chosen) >= 2:
        logger.warning("USB 스피커를 못 찾아 %s (%s) 로 내보냅니다. "
                       "이 장치는 보통 HDMI 라, 스피커를 꽂았는데도 소리가 안 나면 "
                       "USB 연결을 확인하세요.", device, chosen["card_desc"])
    else:
        logger.info("출력 장치: %s (%s)", device, chosen["card_desc"])
    return device


# 탐색 결과 캐시. 매 문장마다 aplay -l 을 부르지 않기 위한 것이고,
# 재생이 실패하면 비워서 다시 찾습니다(USB 를 뽑았다 꽂은 경우).
_resolved_device = None
_device_lock = threading.Lock()


def resolve_output_device():
    """이번 재생에 쓸 장치. 환경변수 > PulseAudio > ALSA 자동 탐색 > 기본."""
    if APLAY_DEVICE:
        return APLAY_DEVICE
    if not APLAY_AUTODETECT:
        return None

    global _resolved_device
    with _device_lock:
        if _resolved_device is None:
            _resolved_device = detect_playback_device() or ""
        return _resolved_device or None


def invalidate_output_device():
    """재생이 실패했을 때 다음번에 다시 찾도록 캐시를 비웁니다.

    USB 스피커는 뽑았다 꽂으면 카드 번호가 바뀔 수 있어서, 한 번 실패한 장치를
    계속 붙들고 있으면 영영 소리가 안 납니다.
    """
    global _resolved_device
    with _device_lock:
        _resolved_device = None


def _aplay_command(wav_path: str, device: str = None):
    """장치와 PulseAudio sink를 반영한 aplay 명령/환경을 만든다."""
    aplay_cmd = ["aplay", "-q"]
    playback_env = None
    if device and device.startswith(_PULSE_SINK_PREFIX):
        # ALSA pulse 플러그인은 PULSE_SINK 환경변수를 읽습니다. 이 값은
        # subprocess에만 전달하므로 다른 데스크톱 앱의 기본 출력을 바꾸지 않습니다.
        aplay_cmd += ["-D", "pulse"]
        playback_env = os.environ.copy()
        playback_env["PULSE_SINK"] = device[len(_PULSE_SINK_PREFIX):]
    elif device:
        aplay_cmd += ["-D", device]
    return aplay_cmd + [wav_path], playback_env


def _mixer_device_for_playback(device: str = None) -> str:
    """aplay 장치 표기를 amixer가 이해하는 mixer 장치로 바꿉니다."""
    if TTS_MIXER_DEVICE:
        return TTS_MIXER_DEVICE
    selected = device or resolve_output_device() or APLAY_DEVICE
    if not selected or selected.startswith(_PULSE_SINK_PREFIX):
        return ""
    # plughw:CARD=UACDemoV10,DEV=0 -> hw:CARD=UACDemoV10
    card_match = re.search(r"CARD=([^,]+)", selected)
    if card_match:
        return f"hw:CARD={card_match.group(1)}"
    if selected.startswith("plughw:"):
        return "hw:" + selected[len("plughw:"):].split(",", 1)[0]
    return selected


def _mixer_controls(device: str):
    """ALSA simple mixer control 이름 목록. 실패해도 음성 재생에는 영향 없습니다."""
    if not device:
        return []
    try:
        result = subprocess.run(
            ["amixer", "-D", device, "scontrols"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("스피커 출력 컨트롤 조회 실패 (%s): %s", device, exc)
        return []
    if result.returncode != 0:
        logger.warning("스피커 출력 컨트롤 조회 실패 (%s): %s", device,
                       result.stderr.decode("utf-8", "ignore").strip()[:200])
        return []
    return re.findall(r"Simple mixer control '([^']+)'", result.stdout.decode("utf-8", "ignore"))


def set_speaker_output_volume(percent: int):
    """Jetson USB 스피커의 ALSA 출력 볼륨을 0~100%로 설정합니다.

    반환값은 (성공 여부, 화면에 보여줄 설명)입니다. mixer가 없는 USB 스피커는
    시스템 출력 조절을 지원하지 않으므로 명확히 실패를 돌려, 기존 TTS 음성
    자체가 조용히 영향을 받지 않게 합니다.
    """
    try:
        value = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        return False, "출력값은 0~100 사이의 숫자여야 합니다"
    device = _mixer_device_for_playback()
    controls = _mixer_controls(device)
    control = TTS_MIXER_CONTROL
    if not control:
        # 장치마다 이름이 다르므로 일반적인 이름을 우선순위로 찾습니다.
        preferred = ("Master", "Speaker", "PCM", "Playback")
        by_lower = {name.lower(): name for name in controls}
        control = next((by_lower[name.lower()] for name in preferred
                        if name.lower() in by_lower), "")
    if not device or not control:
        found = ", ".join(controls) if controls else "없음"
        return False, ("Jetson USB 스피커의 출력 컨트롤을 찾지 못했습니다 "
                       f"(장치: {device or '미확인'}, 컨트롤: {found}). "
                       "Jetson에서 amixer -D hw:CARD=UACDemoV10 scontrols 로 확인하세요.")
    try:
        result = subprocess.run(
            ["amixer", "-M", "-D", device, "sset", control, f"{value}%"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"스피커 출력 설정을 실행하지 못했습니다: {exc}"
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "ignore").strip()[:200]
        return False, f"스피커 출력 설정 실패 ({device} / {control}): {detail}"
    logger.info("🔊 스피커 출력 설정: %s / %s → %d%%", device, control, value)
    return True, f"{control} {value}%"


# ==========================================
# 음성 합성 + 재생
# ==========================================
def _apply_wav_volume(wav_path: str, volume_percent: int) -> bool:
    """Piper PCM WAV에 0~400% 소프트웨어 게인을 적용합니다.

    Jetson의 USB 스피커가 mixer 컨트롤을 제공하지 않아도 동작하고,
    피크가 넘치는 경우에는 클리핑 직전으로만 제한합니다. 따라서 낮은
    Piper 원본은 크게 키우되, 높은 원본을 무작정 찌그러뜨리지는 않습니다.
    실패하면 원본 WAV를 그대로 재생하여 TTS 자체를 유실하지 않습니다.
    """
    try:
        percent = max(0, min(TTS_VOLUME_MAX_PERCENT, int(volume_percent)))
    except (TypeError, ValueError):
        percent = DEFAULT_VOLUME_PERCENT
    if percent == 100:
        return True
    if audioop is None:
        logger.warning("음량 증폭 모듈(audioop)이 없어 원본 음량으로 재생합니다.")
        return False

    temp_fd, temp_path = tempfile.mkstemp(suffix=".wav", prefix="tts_volume_")
    os.close(temp_fd)
    try:
        with wave.open(wav_path, "rb") as source:
            params = source.getparams()
            frames = source.readframes(source.getnframes())
        peak = audioop.max(frames, params.sampwidth) if frames else 0
        requested_gain = percent / 100.0
        if peak:
            # audioop.mul은 범위를 넘은 PCM을 포화시킵니다. 피크 기준으로
            # 이득을 제한하면 큰 소리에서 생기는 거친 왜곡을 막을 수 있습니다.
            max_sample = (1 << (params.sampwidth * 8 - 1)) - 1
            applied_gain = min(requested_gain, max_sample / peak)
        else:
            applied_gain = requested_gain
        if applied_gain < requested_gain:
            logger.info("TTS 음량 %d%% 요청 → 클리핑 방지를 위해 %.0f%% 적용",
                        percent, applied_gain * 100)
        adjusted = audioop.mul(frames, params.sampwidth, applied_gain)
        with wave.open(temp_path, "wb") as target:
            target.setparams(params)
            target.writeframes(adjusted)
        os.replace(temp_path, wav_path)
        return True
    except Exception as exc:  # 손상·비 PCM 파일이어도 원본 재생으로 폴백
        logger.warning("음량 %d%% 적용 실패 — 원본으로 재생합니다 (%s)", percent, exc)
        return False
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def speak_with_piper(text: str, volume_percent: int = None) -> bool:
    """텍스트를 Piper TTS로 합성해 재생합니다. 성공하면 True.

    셸을 거치지 않는 이유
    ---------------------
    text는 네트워크에서 그대로 들어온 값입니다. 예전처럼

        os.system(f'echo "{text}" | piper ...')

    로 만들면 text에 큰따옴표 하나만 들어가도 셸 명령이 탈출됩니다
    (`"; rm -rf ~ #` 같은 문자열이면 젯슨에서 임의 명령이 실행됨).
    악의가 없어도 대응안 문장에 " 나 $ 가 섞이면 그냥 깨집니다.

    subprocess에 리스트로 인자를 넘기면 셸이 개입하지 않으므로 이 문제가
    구조적으로 사라집니다. piper CLI는 표준입력에서 텍스트를 읽으므로
    input= 으로 넘깁니다.
    """
    # 고정 파일명(alert.wav)을 쓰면 이전 경보를 재생하는 도중 다음 경보가
    # 같은 파일을 덮어써서 소리가 섞입니다. 매번 다른 임시 파일을 씁니다.
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="tts_alert_")
    os.close(fd)

    try:
        piper = subprocess.run(
            [PIPER_BIN, "--model", MODEL_PATH, "--output_file", wav_path],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PIPER_TIMEOUT_SECONDS,
        )
        if piper.returncode != 0:
            # 예전에는 실패해도 그대로 aplay를 호출해서, 모델이 없거나 piper가
            # 안 깔린 경우에도 아무 로그 없이 조용히 무음이었습니다.
            logger.error("Piper 합성 실패 (코드 %d): %s",
                         piper.returncode,
                         piper.stderr.decode("utf-8", "ignore").strip()[:300])
            return False

        if os.path.getsize(wav_path) == 0:
            logger.error("Piper가 빈 wav를 만들었습니다 — 모델 경로 확인: %s", MODEL_PATH)
            return False

        selected_volume = (
            DEFAULT_VOLUME_PERCENT if volume_percent is None else volume_percent)
        _apply_wav_volume(wav_path, selected_volume)

        # 출력 장치는 resolve_output_device() 가 정합니다 (환경변수 > USB 자동
        # 탐색 > 기본 장치). 젯슨 기본 출력이 HDMI 라 USB 스피커를 꽂아도
        # 소리가 안 나면서 aplay 는 성공으로 끝나는 것을 막기 위한 것입니다.
        device = resolve_output_device()
        aplay_cmd, playback_env = _aplay_command(wav_path, device)
        aplay = subprocess.run(
            aplay_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=APLAY_TIMEOUT_SECONDS,
            env=playback_env,
        )
        if aplay.returncode != 0:
            stderr_text = aplay.stderr.decode("utf-8", "ignore").strip()[:300]
            logger.error("재생 실패 (코드 %d, 장치 %s): %s",
                         aplay.returncode, device or "기본", stderr_text)
            # USB 를 뽑았다 꽂으면 카드 번호가 바뀝니다. 실패한 장치를 계속
            # 붙들고 있으면 영영 소리가 안 나므로, 다음 문장에서 다시 찾습니다.
            if not APLAY_DEVICE:
                invalidate_output_device()
            return False
        return True

    except FileNotFoundError as exc:
        # piper 또는 aplay 자체가 없는 경우. 매 경보마다 같은 에러가 쌓이므로
        # 원인을 바로 알 수 있게 명령 이름을 남깁니다.
        logger.error("실행 파일을 찾을 수 없습니다 (%s). piper / alsa-utils 설치 확인", exc)
        return False
    except subprocess.TimeoutExpired:
        logger.error("TTS 처리 시간 초과 — 문장이 너무 길거나 오디오 장치가 응답하지 않음")
        return False
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


# ==========================================
# 재생 큐 — 수신 루프를 막지 않기 위한 분리
# ==========================================
_speak_queue = SpeechPriorityQueue(maxsize=SPEAK_QUEUE_SIZE)


def _coerce_request(value, priority: int = 50,
                    expires_at: float = None) -> SpeechRequest:
    if isinstance(value, SpeechRequest):
        return value
    now = time.time()
    return SpeechRequest(
        message_id=uuid.uuid4().hex,
        text=" ".join(str(value).split())[:MAX_TEXT_CHARS],
        priority=max(0, min(100, int(priority))),
        created_at=now,
        expires_at=float(
            expires_at if expires_at is not None else now + MESSAGE_TTL_SECONDS),
        volume_percent=DEFAULT_VOLUME_PERCENT,
    )


def _speak_worker():
    """큐에 쌓인 문장을 순서대로 읽어 재생하는 백그라운드 워커."""
    while True:
        request = _coerce_request(_speak_queue.get())
        try:
            if request.expired():
                logger.warning("만료된 경보를 재생하지 않습니다: %s", request.message_id)
                _remember_message_status(request.message_id, "expired")
                continue
            _remember_message_status(request.message_id, "playing")
            ok = speak_with_piper(request.text, request.volume_percent)
            _remember_message_status(
                request.message_id, "played" if ok else "failed")
        except Exception:
            logger.exception("재생 중 예외 발생 (워커는 계속 동작합니다)")
            _remember_message_status(request.message_id, "failed")
        finally:
            _speak_queue.task_done()


_speak_worker_thread = None
_speak_worker_lock = threading.Lock()


def _ensure_speak_worker():
    """재생 워커가 없으면 띄웁니다. 여러 번 불러도 하나만 돕니다.

    daemon=True: 메인이 끝나면 워커도 같이 정리됩니다.
    """
    global _speak_worker_thread
    with _speak_worker_lock:
        if _speak_worker_thread is None or not _speak_worker_thread.is_alive():
            _speak_worker_thread = threading.Thread(target=_speak_worker, daemon=True)
            _speak_worker_thread.start()
    return _speak_worker_thread


def enqueue_speech(value, priority: int = 50,
                   expires_at: float = None) -> QueueResult:
    """재생 큐에 넣습니다. 우선순위와 만료시각을 함께 반영합니다.

    예전에는 accept 루프 안에서 바로 재생해서, 한 문장을 읽는 동안(5~15초)
    다른 구역 경보를 아예 받지 못했습니다.

    큐가 차면 낮은 우선순위를 먼저 버리고, 같은 등급에서는 오래된 경보를
    교체합니다. 이미 만료된 요청은 큐에 넣지 않습니다.
    """
    request = _coerce_request(value, priority, expires_at)

    # 내부 테스트나 외부 호출부가 표준 queue.Queue를 주입한 경우도 계속 지원합니다.
    if not hasattr(_speak_queue, "put_request"):
        try:
            _speak_queue.put_nowait(request.text)
            return QueueResult("queued")
        except queue.Full:
            pass
        try:
            dropped = _speak_queue.get_nowait()
            _speak_queue.task_done()
            logger.warning("재생 큐 포화 — 오래된 경보를 버립니다: %.40s...", dropped)
        except queue.Empty:
            pass
        try:
            _speak_queue.put_nowait(request.text)
            return QueueResult("queued")
        except queue.Full:
            logger.warning("재생 큐 포화 — 이번 경보를 버립니다: %.40s...",
                           request.text)
            return QueueResult("dropped")

    result = _speak_queue.put_request(request)
    if result.dropped_message_id:
        _remember_message_status(result.dropped_message_id, "dropped")
        logger.warning("재생 큐 포화 — 낮은 우선순위/오래된 경보를 버립니다: %s",
                       result.dropped_message_id)
    if result.status == "dropped":
        logger.warning("재생 큐 포화 — 이번 경보를 버립니다: %s", request.message_id)
    elif result.status == "expired":
        logger.warning("이미 만료된 경보를 버립니다: %s", request.message_id)
    return result


# ==========================================
# 수신
# ==========================================
def _recv_until_close(conn, max_bytes: int, deadline: float = None) -> bytes:
    """송신 측이 연결을 닫을 때까지 읽습니다.

    예전의 recv(1024) 한 번 호출에는 두 가지 문제가 있었습니다.
      1) TCP는 스트림이라 한 번의 recv가 보낸 만큼을 다 준다는 보장이 없습니다.
      2) 한글 500자는 UTF-8로 최대 1500바이트라 1024에서 반드시 잘리고,
         멀티바이트 문자 중간에서 잘리면 디코딩이 터집니다.

    Rag_to_Jetson.py가 sendall() 후 소켓을 닫으므로, EOF까지 읽으면 됩니다.

    deadline 은 **요청 전체**의 마감 시각(time.monotonic 기준)입니다.
    socket.settimeout() 은 recv 한 번마다 다시 세어지기 때문에, 그것만으로는
    1바이트씩 천천히 흘리는 클라이언트를 막지 못합니다. 그런 연결이 하나만
    붙어 있어도 그동안 도착한 진짜 경보가 처리되지 못했습니다.
    """
    chunks = []
    total = 0
    while total < max_bytes:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("요청 전체 수신 제한 시간 초과")
            conn.settimeout(remaining)
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[:max_bytes]


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_priority(value, default: int = 50) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_volume_percent(value, default: int = DEFAULT_VOLUME_PERCENT) -> int:
    try:
        return max(0, min(TTS_VOLUME_MAX_PERCENT, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_speaker_volume_percent(value, default: int = 100) -> int:
    """실제 스피커 mixer 값은 증폭이 아니라 0~100%만 허용합니다."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def parse_request(raw: bytes) -> SpeechRequest:
    """수신 바이트를 구조화된 방송 요청으로 변환합니다.

    v2 JSON이 정상 경로이며, 이전 평문과 {"text": ...} 요청도 호환합니다.
    """
    decoded = raw.decode("utf-8", errors="ignore").strip()
    if not decoded:
        return SpeechRequest("", "")

    now = time.time()
    message_id = uuid.uuid4().hex
    token = ""
    priority = 50
    created_at = now
    expires_at = now + MESSAGE_TTL_SECONDS
    zone = ""
    detected_object = ""
    volume_percent = DEFAULT_VOLUME_PERCENT
    command = ""
    speaker_volume_percent = 100
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        # 평문 호환은 유지하되, 깨진 JSON을 그대로 스피커로 읽지는 않습니다.
        text = "" if decoded[:1] in {"{", "["} else decoded
    else:
        if isinstance(payload, dict):
            text = payload.get("text", "")
            token = str(payload.get("token", "") or "")
            message_id = str(payload.get("message_id", "") or message_id)
            priority = _safe_priority(payload.get("priority"), 50)
            created_at = _safe_float(payload.get("created_at"), now)
            expires_at = _safe_float(
                payload.get("expires_at"), created_at + MESSAGE_TTL_SECONDS)
            zone = str(payload.get("zone", "") or "")
            detected_object = str(payload.get("detected_object", "") or "")
            volume_percent = _safe_volume_percent(
                payload.get("volume_percent"), DEFAULT_VOLUME_PERCENT)
            command = str(payload.get("command", "") or "")
            speaker_volume_percent = _safe_speaker_volume_percent(
                payload.get("speaker_volume_percent"), 100)
        elif isinstance(payload, str):
            text = str(payload)
        else:
            text = ""

    text = " ".join(str(text).split())      # 개행/중복 공백 정리
    return SpeechRequest(
        message_id=message_id,
        text=text[:MAX_TEXT_CHARS],
        token=token,
        priority=priority,
        created_at=created_at,
        expires_at=expires_at,
        zone=zone,
        detected_object=detected_object,
        volume_percent=volume_percent,
        command=command,
        speaker_volume_percent=speaker_volume_percent,
    )


def parse_message(raw: bytes):
    """기존 호출부 호환용 (text, token) 반환."""
    request = parse_request(raw)
    return request.text, request.token


def parse_payload(raw: bytes) -> str:
    """parse_message 의 문장 부분만. 예전 호출부/테스트 호환용입니다."""
    return parse_message(raw)[0]


_message_status = {}
_message_status_lock = threading.Lock()


def _remember_message_status(message_id: str, status: str) -> None:
    if not message_id:
        return
    now = time.time()
    with _message_status_lock:
        expired_keys = [
            key for key, (_value, saved_at) in _message_status.items()
            if now - saved_at > MESSAGE_STATUS_RETENTION_SECONDS
        ]
        for key in expired_keys:
            _message_status.pop(key, None)
        # 높은 요청률이나 비정상적인 message_id 입력에도 메모리가 계속 자라지 않게
        # 가장 오래 기억한 항목부터 제거합니다. Python dict는 삽입 순서를 보존합니다.
        if (message_id not in _message_status
                and len(_message_status) >= MESSAGE_STATUS_MAX_ENTRIES):
            remove_count = len(_message_status) - MESSAGE_STATUS_MAX_ENTRIES + 1
            oldest = sorted(
                _message_status.items(), key=lambda item: item[1][1])[:remove_count]
            for key, _value in oldest:
                _message_status.pop(key, None)
        _message_status[message_id] = (status, now)


def get_message_status(message_id: str) -> str:
    """최근 message_id 상태. 이후 health/status API에서도 재사용할 수 있습니다."""
    with _message_status_lock:
        value = _message_status.get(message_id)
        if value and time.time() - value[1] > MESSAGE_STATUS_RETENTION_SECONDS:
            _message_status.pop(message_id, None)
            value = None
    return value[0] if value else ""


def _send_ack(conn, request: SpeechRequest, status: str,
              detail: str = "", dropped_message_id: str = "") -> None:
    payload = {
        "message_id": request.message_id,
        "status": status,
    }
    if detail:
        payload["detail"] = detail
    if dropped_message_id:
        payload["dropped_message_id"] = dropped_message_id
    conn.sendall(
        (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _safe_send_ack(conn, request: SpeechRequest, status: str,
                   detail: str = "", dropped_message_id: str = "") -> bool:
    """ACK 유실은 이미 완료된 큐 처리를 실패로 되돌리지 않습니다."""
    try:
        _send_ack(conn, request, status, detail, dropped_message_id)
        return True
    except OSError as exc:
        logger.warning("ACK 전송 실패 (%s, %s): %s",
                       request.message_id or "no-message-id", status, exc)
        return False


def is_authorized(addr_ip: str, token: str) -> bool:
    """이 요청을 스피커로 내보내도 되는지 판단합니다.

    이 수신기는 **데이터를 읽어가는 서버가 아니라 소리를 내는 액추에이터**입니다.
    같은 망에 있는 누구든 TCP 로 문장을 던지면 공장 스피커가 그대로 읽습니다.
    "지금 즉시 대피하십시오" 같은 문장이 장난으로 나가면 그 자체가 안전 사고입니다.

    TTS_TOKEN / TTS_ALLOWED_IPS 를 비워 두면 예전 동작 그대로(누구나 가능)이고,
    그 경우 시작할 때 경고를 출력합니다.
    """
    if TTS_ALLOWED_IPS and addr_ip not in TTS_ALLOWED_IPS:
        logger.warning("허용되지 않은 주소의 요청 거부: %s", addr_ip)
        return False
    if TTS_TOKEN:
        # 타이밍 공격 방지를 위해 길이/내용 비교를 상수 시간으로 합니다.
        if not hmac.compare_digest(token, TTS_TOKEN):
            logger.warning("토큰이 일치하지 않는 요청 거부: %s", addr_ip)
            return False
    return True


def start_tts_receiver(on_text=None, bind_ip: str = None, port: int = None,
                       ready_event: threading.Event = None) -> None:
    """대응안 문장을 받는 TCP 서버.

    on_text: 문장 1건을 처리할 콜백. 기본은 재생 큐에 넣기.
             (테스트에서 piper/aplay 없이 검증하려고 주입 가능하게 뒀습니다.)
    """
    if on_text is None:
        accept_request = enqueue_speech
        _ensure_speak_worker()
    else:
        # 기존 테스트/외부 호출부는 문자열 콜백을 주입합니다.
        def accept_request(request):
            result = on_text(request.text)
            if isinstance(result, QueueResult):
                return result
            return QueueResult("dropped" if result is False else "queued")
    bind_ip = bind_ip if bind_ip is not None else TTS_BIND_IP
    port = port if port is not None else TTS_PORT

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind((bind_ip, port))
    except OSError as exc:
        logger.error("포트 %d 바인딩 실패: %s", port, exc)
        logger.error("이미 떠 있는 프로세스가 있는지 확인: lsof -i :%d", port)
        server_socket.close()
        raise

    server_socket.listen(max(1, MAX_CONCURRENT_CONNECTIONS))
    logger.info("🔊 [Jetson TTS Receiver] 대응안 수신 대기 (%s:%d)", bind_ip, port)
    logger.info("   모델: %s", MODEL_PATH)
    if not os.path.exists(MODEL_PATH):
        logger.warning("   ⚠ 모델 파일이 없습니다. PIPER_MODEL_PATH를 확인하세요.")
        logger.warning("     받는 법: ./setup_tts.sh")
    elif not os.path.exists(MODEL_PATH + ".json"):
        # piper 는 <모델>.onnx 옆의 <모델>.onnx.json 을 함께 읽습니다. 이게 없으면
        # 합성이 통째로 실패하는데, 에러 메시지만 봐서는 원인을 알기 어렵습니다.
        logger.warning("   ⚠ 설정 파일이 없습니다: %s.json", MODEL_PATH)
        logger.warning("     piper 는 .onnx 와 .onnx.json 을 **둘 다** 필요로 합니다.")
        logger.warning("     받는 법: ./setup_tts.sh")

    # 출력 장치를 시작할 때 미리 정해 둡니다. 첫 경보가 울릴 때까지 기다렸다가
    # "소리가 안 난다" 를 발견하면 늦습니다.
    if APLAY_DEVICE:
        logger.info("   출력 장치: %s (APLAY_DEVICE 지정)", APLAY_DEVICE)
    elif APLAY_AUTODETECT:
        resolve_output_device()
    else:
        logger.info("   출력 장치: 시스템 기본 (자동 탐색 꺼짐)")

    if TTS_TOKEN or TTS_ALLOWED_IPS:
        limits = []
        if TTS_TOKEN:
            limits.append("토큰")
        if TTS_ALLOWED_IPS:
            limits.append(f"허용 IP {len(TTS_ALLOWED_IPS)}개")
        logger.info("   접근 제한: %s", " + ".join(limits))
    elif bind_ip == "0.0.0.0":
        logger.warning("")
        logger.warning("   ⚠ 모든 네트워크 인터페이스에 인증 없이 열려 있습니다.")
        logger.warning("     이 포트로 문장을 던지면 공장 스피커가 그대로 읽습니다.")
        logger.warning("     운영 시에는 아래처럼 제한하세요:")
        logger.warning("       export TTS_BIND_IP=203.0.113.10")
        logger.warning("       export TTS_TOKEN=$(python3 -c \"import secrets;print(secrets.token_urlsafe(16))\")")
        logger.warning("       (보내는 쪽에도 같은 TTS_TOKEN 을 넣어야 합니다)")
        logger.warning("")

    if ready_event is not None:
        ready_event.set()

    slots = threading.Semaphore(MAX_CONCURRENT_CONNECTIONS)

    def handle(conn, addr):
        request = None
        try:
            # 요청 하나를 다 받는 데 허용할 전체 시간. settimeout() 은 recv 마다
            # 다시 세어지므로, 마감 시각을 따로 넘겨야 천천히 흘리는 연결을 끊습니다.
            deadline = time.monotonic() + RECV_TIMEOUT_SECONDS
            conn.settimeout(RECV_TIMEOUT_SECONDS)
            request = parse_request(
                _recv_until_close(conn, MAX_PAYLOAD_BYTES, deadline))
            if request.command:
                if request.command != "set_speaker_output_volume":
                    _safe_send_ack(conn, request, "invalid", "unsupported command")
                    return
                if not is_authorized(addr[0], request.token):
                    _safe_send_ack(conn, request, "rejected", "authorization failed")
                    return
                ok, detail = set_speaker_output_volume(request.speaker_volume_percent)
                _safe_send_ack(conn, request, "configured" if ok else "failed", detail)
                return
            if not request.text:
                logger.warning("빈 페이로드 수신 (%s) — 무시", addr[0])
                _safe_send_ack(conn, request, "invalid", "empty text")
                return
            if not is_authorized(addr[0], request.token):
                _safe_send_ack(conn, request, "rejected", "authorization failed")
                return
            if request.expired():
                _remember_message_status(request.message_id, "expired")
                _safe_send_ack(conn, request, "expired", "message TTL exceeded")
                return

            previous = get_message_status(request.message_id)
            if previous in {"receiving", "queued", "playing", "played"}:
                _safe_send_ack(
                    conn, request, "duplicate",
                    f"original status: {previous}")
                return

            _remember_message_status(request.message_id, "receiving")
            logger.info("📢 [수신] %s: %s | 우선순위 %d | %s",
                        addr[0], request.message_id, request.priority, request.text)
            result = accept_request(request)
            _remember_message_status(request.message_id, result.status)
            _safe_send_ack(
                conn, request, result.status,
                dropped_message_id=result.dropped_message_id)
        except socket.timeout:
            logger.warning("수신 타임아웃 (%s) — 연결을 닫습니다", addr[0])
        except Exception:
            logger.exception("수신 처리 실패 (%s)", addr[0])
            if request is not None:
                _remember_message_status(request.message_id, "failed")
                _safe_send_ack(conn, request, "error", "receiver exception")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            slots.release()

    try:
        while True:
            conn, addr = server_socket.accept()
            # 연결을 하나씩 직렬로 처리하면, 느린 클라이언트 하나가 붙어 있는
            # 동안 진짜 경보를 통째로 놓칩니다(송신 측은 backlog 에 들어갔으므로
            # "송신 성공"을 찍어서, 안 울린 줄도 모릅니다).
            if not slots.acquire(blocking=False):
                logger.warning("동시 연결 상한(%d) 초과 — %s 연결을 즉시 닫습니다",
                               MAX_CONCURRENT_CONNECTIONS, addr[0])
                conn.close()
                continue
            threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        logger.info("종료 신호 감지, 수신기를 정리합니다.")
    finally:
        server_socket.close()


def main():
    # 워커는 start_tts_receiver() 가 기본 경로일 때 알아서 띄웁니다.
    start_tts_receiver()


if __name__ == "__main__":
    main()
