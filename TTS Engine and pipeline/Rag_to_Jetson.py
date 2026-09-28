"""
Rag_to_Jetson.py — 대응안 문장을 젯슨 TTS로 송신

역할
----
RAG 대응안의 구역·객체·recommended_action을 방송 문장으로 만들어 보냅니다.
받는 쪽은 같은 폴더의 Rx_pipeline.py 입니다.

    [RagResponseAgent] --> send_plan_async() --TCP :9997/queued ACK--> [Rx_pipeline.py]

포트는 양쪽이 같은 환경변수(TTS_TCP_PORT)를 읽습니다. 젯슨 IP도 대시보드와
같은 변수(JETSON_IP)를 씁니다 — 저장소 공통 규칙입니다.

파이프라인에 붙일 때
--------------------
대응안 생성 직후 bounded 송신 워커를 쓰는 send_plan_async()를 호출합니다.
구역·객체·대응안을 공통 문장으로 만들고 젯슨의 queued ACK까지 확인합니다.

    from Rag_to_Jetson import send_plan_async
    ...
    plan = agent.generate_response(event)
    if writer.write_response_plan(plan):
        send_plan_async(plan)
"""

from dataclasses import dataclass
import json
import logging
import os
import queue
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional
import uuid

logger = logging.getLogger("tts_sender")


# ==========================================
# 설정
# ==========================================
# 대시보드(edge_video/dashboard_video_minimap_section.py)와 같은 변수/기본값을
# 씁니다. 예전에는 이 파일에만 203.0.113.11가 하드코딩되어 있어서, 저장소 안에
# 젯슨 IP 기본값이 두 종류(.25 / .50)로 갈려 있었습니다.
JETSON_IP = os.environ.get("JETSON_IP", "203.0.113.10")

# 받는 쪽(Rx_pipeline.py)과 같은 환경변수.
TTS_PORT = int(os.environ.get("TTS_TCP_PORT", "9997"))

# 받는 쪽에서 TTS_TOKEN 을 켰다면 여기에도 같은 값이 있어야 합니다.
# 이 수신기는 소리를 내는 액추에이터라, 열어두면 같은 망의 누구든 공장 스피커로
# 아무 문장이나 내보낼 수 있습니다.
TTS_TOKEN = os.environ.get("TTS_TOKEN", "").strip()

CONNECT_TIMEOUT_SECONDS = 3.0
MAX_TEXT_CHARS = 500        # response_plans.recommended_action VARCHAR(500)과 동일
DEFAULT_RETRIES = 1         # 순간적인 연결 실패로 경보를 통째로 잃지 않도록
RETRY_DELAY_SECONDS = 0.3   # 곧바로 다시 걸면 같은 이유로 또 실패합니다
MESSAGE_TTL_SECONDS = float(os.environ.get("TTS_MESSAGE_TTL_SEC", "20"))
try:
    # 긴급 방송 5회가 송신 큐에서 잘리지 않도록 최소 크기를 보장합니다.
    SEND_QUEUE_SIZE = max(5, int(os.environ.get("TTS_SEND_QUEUE_SIZE", "16")))
except ValueError:
    SEND_QUEUE_SIZE = 16
SEND_DRAIN_TIMEOUT_SECONDS = float(
    os.environ.get("TTS_SEND_DRAIN_TIMEOUT_SEC", "15"))
TTS_VOLUME_STATE_FILE = os.environ.get(
    "TTS_VOLUME_STATE_FILE", "/tmp/smart_factory_tts_volume")
try:
    TTS_VOLUME_MAX_PERCENT = max(
        100, min(400, int(os.environ.get("TTS_VOLUME_MAX_PERCENT", "400"))))
except ValueError:
    TTS_VOLUME_MAX_PERCENT = 400
MAX_ACK_BYTES = 4096
ACK_SUCCESS_STATUSES = {"queued", "duplicate"}
SPEECH_OBJECT_LABELS = {
    "fire": "화재",
    "hazardous leak": "유해물질 누출",
    "gas leak": "가스 누출",
    "smoke": "연기",
    "person": "작업자",
    "human": "작업자",
    "person with no helmet": "안전모 미착용 작업자",
    "vehicle": "차량",
    "forklift": "지게차",
    "heavy equipment": "중장비",
}

# 현장 방송은 관리자가 화면에서 읽는 상세 대응안과 목적이 다릅니다. 이 목록에
# 있고 '위험' 등급으로 확정된 감지만 방송합니다. 일반 작업자,
# 미분류 객체, 정상·주의 상태에서는 문장 자체를 만들지 않습니다.
# LLM 응답을 기다리지 않으므로 LLM을 꺼도, 느려도 동작합니다.
EMERGENCY_TTS_ACTIONS = {
    "fire": "화재 발생. 즉시 대피하십시오.",
    "화재": "화재 발생. 즉시 대피하십시오.",
    "hazardous leak": "유해물질 누출 발생. 즉시 대피하십시오.",
    "유해물질 누출": "유해물질 누출 발생. 즉시 대피하십시오.",
    "gas leak": "가스 누출 발생. 즉시 대피하십시오.",
    "가스 누출": "가스 누출 발생. 즉시 대피하십시오.",
    "smoke": "연기 감지. 즉시 대피하십시오.",
    "연기": "연기 감지. 즉시 대피하십시오.",
}
STANDARD_TTS_ACTIONS = {
    "person with no helmet": "안전모 미착용. 작업을 중지하십시오.",
    "안전모 미착용": "안전모 미착용. 작업을 중지하십시오.",
    "vehicle": "차량 위험. 안전거리를 확보하십시오.",
    "차량·중장비": "차량 위험. 안전거리를 확보하십시오.",
}
EMERGENCY_TTS_REPEAT_COUNT = 5


def _clamp_volume(value, default: int = 140) -> int:
    """0~400% 범위의 음량으로 정규화합니다."""
    try:
        return max(0, min(TTS_VOLUME_MAX_PERCENT, int(value)))
    except (TypeError, ValueError):
        return default


def get_tts_volume_percent() -> int:
    """방송 WAV는 원본(100%)으로 보냅니다.

    실제 소리 크기는 대시보드의 ``스피커 출력``이 Jetson ALSA mixer에서
    조절한다. 예전 /tmp 상태 파일을 계속 읽으면 기존의 0% TTS 음량 값이
    남아 새 스피커 출력 UI에서도 방송을 무음으로 만들 수 있어 사용하지 않는다.
    """
    return 100


def _field(value: Any, name: str, default: Any = "") -> Any:
    """dict와 dataclass/일반 객체에서 같은 방식으로 값을 읽습니다."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _plan_alert_fields(plan: Any):
    """dict/dataclass 이벤트·대응안에서 방송 판정 필드를 꺼낸다."""
    source_event = _field(plan, "source_event", None)
    zone = str(
        _field(plan, "zone", "")
        or _field(source_event, "zone", "")
        or ""
    ).strip()
    detected_object = str(
        _field(plan, "detected_object", "")
        or _field(source_event, "detected_object", "")
        or ""
    ).strip()
    risk_level = str(
        _field(plan, "risk_level", "")
        or _field(source_event, "risk_level", "")
        or ""
    ).strip()
    return zone, detected_object, risk_level


def is_tts_alert_event(event: Any) -> bool:
    """위험 등급으로 확정된 지원 문제 이벤트인지 판단합니다."""
    _zone, detected_object, risk_level = _plan_alert_fields(event)
    label = detected_object.lower()
    return (risk_level == "위험"
            and (label in EMERGENCY_TTS_ACTIONS
                 or label in STANDARD_TTS_ACTIONS))


def build_speech_text(plan: Any) -> str:
    """현장용 짧은 방송 문장. 관리자용 recommended_action은 읽지 않는다.

    ``recommended_action``은 근거·순서가 포함된 화면용 상세 대응안입니다.
    이를 그대로 읽으면 길고 느려져 긴급 대피 안내로 부적절하므로, TTS는
    감지 객체와 구역만으로 결정된 고정 문장을 사용합니다.
    """
    if not is_tts_alert_event(plan):
        return ""
    zone, detected_object, _risk_level = _plan_alert_fields(plan)
    label = detected_object.lower()
    action = (EMERGENCY_TTS_ACTIONS.get(label)
              or STANDARD_TTS_ACTIONS.get(label))
    head = f"{zone} 구역" if zone else "현장"
    return f"{head} {action}"[:MAX_TEXT_CHARS]


def is_emergency_tts_event(event: Any) -> bool:
    """감지 즉시 5회 반복 방송할 긴급 이벤트인지 판단한다."""
    _zone, detected_object, _risk_level = _plan_alert_fields(event)
    return (is_tts_alert_event(event)
            and detected_object.lower() in EMERGENCY_TTS_ACTIONS)


def send_detection_alert_async(
        event: Any,
        on_complete: Optional[Callable[["DeliveryResult"], None]] = None):
    """확정된 문제만 방송 큐에 넣습니다.

    화재·누출·연기는 5회, 안전모 미착용·근접 차량 위험은 1회
    안내합니다. 정상, 주의, 지원하지 않는 객체는 빈 목록입니다.
    """
    if not is_tts_alert_event(event):
        return []
    zone, detected_object, _risk_level = _plan_alert_fields(event)
    text = build_speech_text(event)
    repeat_count = (EMERGENCY_TTS_REPEAT_COUNT
                    if is_emergency_tts_event(event) else 1)
    return [
        send_alert_async(
            text, zone=zone,
            detected_object=detected_object,
            priority=priority_for_object(detected_object),
            on_complete=on_complete)
        for _ in range(repeat_count)
    ]


def send_emergency_alert_async(event: Any,
                               on_complete: Optional[Callable[["DeliveryResult"], None]] = None):
    """긴급 방송을 LLM·대응안 저장과 무관하게 즉시 5회 큐에 넣는다."""
    if not is_emergency_tts_event(event):
        return []
    return send_detection_alert_async(event, on_complete=on_complete)


def priority_for_object(detected_object: str) -> int:
    """큰 값일수록 먼저 방송합니다."""
    label = str(detected_object or "").strip().lower()
    if label in {"fire", "hazardous leak", "gas leak", "smoke"}:
        return 100
    if label in {"person", "human", "person with no helmet"}:
        return 80
    if label in {"vehicle", "forklift", "heavy equipment"}:
        return 60
    return 50


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    message_id: str
    status: str
    detail: str = ""


class DeliveryHandle:
    """비동기 송신 결과. 기존 Thread처럼 join()할 수 있습니다."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self._event = threading.Event()
        self._result: Optional[DeliveryResult] = None

    def _finish(self, result: DeliveryResult) -> None:
        if not self._event.is_set():
            self._result = result
            self._event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        self._event.wait(timeout)

    def is_alive(self) -> bool:
        return not self._event.is_set()

    def result(self, timeout: Optional[float] = None) -> Optional[DeliveryResult]:
        self._event.wait(timeout)
        return self._result


def _read_ack(sock: socket.socket) -> Dict[str, Any]:
    chunks = []
    total = 0
    while total < MAX_ACK_BYTES:
        chunk = sock.recv(min(1024, MAX_ACK_BYTES - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise OSError("젯슨이 수신 ACK를 보내지 않았습니다")
    try:
        ack = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("젯슨 ACK 형식이 올바르지 않습니다") from exc
    if not isinstance(ack, dict):
        raise OSError("젯슨 ACK가 JSON 객체가 아닙니다")
    return ack


def send_alert_with_result(jetson_ip: Optional[str] = None,
                           alert_text: str = "",
                           port: Optional[int] = None,
                           timeout: float = CONNECT_TIMEOUT_SECONDS,
                           retries: int = DEFAULT_RETRIES,
                           *,
                           message_id: Optional[str] = None,
                           priority: int = 50,
                           created_at: Optional[float] = None,
                           expires_at: Optional[float] = None,
                           zone: str = "",
                           detected_object: str = "",
                           volume_percent: Optional[int] = None) -> DeliveryResult:
    """문장 1건을 보내고 젯슨의 대기열 ACK 결과를 반환합니다.

    jetson_ip / port에 None을 주면 환경변수(JETSON_IP / TTS_TCP_PORT)를 씁니다.
    """
    ip = jetson_ip if jetson_ip is not None else JETSON_IP
    dest_port = port if port is not None else TTS_PORT

    text = " ".join(str(alert_text).split())[:MAX_TEXT_CHARS]
    msg_id = str(message_id or uuid.uuid4().hex)
    if not text:
        logger.warning("[TTS 송신] 빈 문장이라 보내지 않았습니다")
        return DeliveryResult(False, msg_id, "invalid", "empty text")

    created = float(created_at if created_at is not None else time.time())
    expires = float(
        expires_at if expires_at is not None else created + MESSAGE_TTL_SECONDS)
    message = {
        "version": 2,
        "message_id": msg_id,
        "text": text,
        "priority": max(0, min(100, int(priority))),
        "created_at": created,
        "expires_at": expires,
        "zone": str(zone or ""),
        "detected_object": str(detected_object or ""),
        "volume_percent": _clamp_volume(
            get_tts_volume_percent() if volume_percent is None else volume_percent),
    }
    if TTS_TOKEN:
        message["token"] = TTS_TOKEN
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")

    last_error = None
    for attempt in range(retries + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((ip, dest_port))
                sock.sendall(payload)
                # 받는 쪽은 EOF까지 읽으므로, 다 보냈다는 신호로 쓰기 방향을
                # 닫아줍니다. 이게 없으면 수신 측이 recv 타임아웃까지 기다립니다.
                sock.shutdown(socket.SHUT_WR)
                ack = _read_ack(sock)

            ack_id = str(ack.get("message_id") or "")
            status = str(ack.get("status") or "invalid_ack")
            detail = str(ack.get("detail") or "")
            if ack_id != msg_id:
                raise OSError(
                    f"ACK message_id 불일치: {ack_id or '(missing)'}")
            result = DeliveryResult(
                status in ACK_SUCCESS_STATUSES, msg_id, status, detail)
            if result.ok:
                logger.info("📤 [TTS 대기열 등록] -> %s:%d | %s | %s",
                            ip, dest_port, msg_id, text)
            else:
                logger.error("❌ [TTS 수신 거부] %s | %s (%s)",
                             msg_id, status, detail)
            return result
        except OSError as exc:
            last_error = exc
            if attempt < retries:
                # 곧바로 다시 걸면 같은 이유(연결 거부 등)로 또 실패합니다.
                time.sleep(RETRY_DELAY_SECONDS)
                continue

    logger.error("❌ [TTS 송신 실패] 젯슨 연결/ACK 확인 필요 (%s:%d) - %s",
                 ip, dest_port, last_error)
    return DeliveryResult(
        False, msg_id, "transport_error", str(last_error or ""))


def send_alert_to_jetson(jetson_ip: Optional[str] = None,
                         alert_text: str = "",
                         port: Optional[int] = None,
                         timeout: float = CONNECT_TIMEOUT_SECONDS,
                         retries: int = DEFAULT_RETRIES,
                         **metadata) -> bool:
    """기존 bool API. 이제 TCP 전달이 아니라 젯슨 대기열 ACK를 성공으로 봅니다."""
    return send_alert_with_result(
        jetson_ip, alert_text, port, timeout, retries, **metadata).ok


def set_speaker_output_volume(volume_percent: int,
                              jetson_ip: Optional[str] = None,
                              port: Optional[int] = None,
                              timeout: float = CONNECT_TIMEOUT_SECONDS) -> DeliveryResult:
    """Jetson ALSA 스피커 출력값을 인증된 제어 요청으로 설정합니다.

    음성 메시지와 달리 큐에 넣지 않고, 수신기가 mixer 변경을 완료한 ACK를
    기다립니다. 그래서 대시보드는 실제 적용 실패를 즉시 보여줄 수 있습니다.
    """
    try:
        volume = max(0, min(100, int(volume_percent)))
    except (TypeError, ValueError):
        return DeliveryResult(False, "", "invalid", "출력값은 0~100 사이여야 합니다")
    ip = jetson_ip if jetson_ip is not None else JETSON_IP
    dest_port = port if port is not None else TTS_PORT
    msg_id = uuid.uuid4().hex
    message = {
        "version": 2,
        "message_id": msg_id,
        "command": "set_speaker_output_volume",
        "speaker_volume_percent": volume,
    }
    if TTS_TOKEN:
        message["token"] = TTS_TOKEN
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, dest_port))
            sock.sendall(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            ack = _read_ack(sock)
        ack_id = str(ack.get("message_id") or "")
        status = str(ack.get("status") or "invalid_ack")
        detail = str(ack.get("detail") or "")
        if ack_id != msg_id:
            raise OSError(f"ACK message_id 불일치: {ack_id or '(missing)'}")
        result = DeliveryResult(status == "configured", msg_id, status, detail)
        if result.ok:
            logger.info("🔊 [스피커 출력 설정] %s:%d → %d%% (%s)",
                        ip, dest_port, volume, detail)
        else:
            logger.error("❌ [스피커 출력 설정 거부] %s (%s)", status, detail)
        return result
    except OSError as exc:
        logger.error("❌ [스피커 출력 설정 실패] %s:%d - %s", ip, dest_port, exc)
        return DeliveryResult(False, msg_id, "transport_error", str(exc))


def _plan_metadata(plan: Any) -> Dict[str, Any]:
    source_event = _field(plan, "source_event", None)
    zone = str(_field(plan, "zone", "") or "").strip()
    detected_object = str(
        _field(plan, "detected_object", "")
        or _field(source_event, "detected_object", "")
        or ""
    ).strip()
    return {
        "alert_text": build_speech_text(plan),
        "zone": zone,
        "detected_object": detected_object,
        "priority": priority_for_object(detected_object),
        "volume_percent": get_tts_volume_percent(),
    }


def send_plan_to_jetson(plan: Any, **transport) -> bool:
    """대응안 객체/dict를 공통 방송 문장으로 만들어 동기 전송합니다."""
    metadata = _plan_metadata(plan)
    metadata.update(transport)
    return send_alert_to_jetson(**metadata)


@dataclass
class _SendJob:
    kwargs: Dict[str, Any]
    handle: DeliveryHandle
    callback: Optional[Callable[[DeliveryResult], None]] = None


class TtsSenderWorker:
    """경보마다 스레드를 만들지 않는 bounded 단일 송신 워커."""

    def __init__(self, queue_size: int = SEND_QUEUE_SIZE):
        self._queue = queue.Queue(maxsize=max(1, queue_size))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._start_lock = threading.Lock()
        self._lock = threading.Lock()
        self._submitted = 0
        self._queued = 0
        self._failed = 0
        self._dropped = 0

    def start(self) -> None:
        # 여러 탐지 처리 스레드가 처음 경보를 동시에 제출해도 worker는 하나만 띄웁니다.
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="tts-sender-worker", daemon=True)
            self._thread.start()

    @staticmethod
    def _finish_job(job: _SendJob, result: DeliveryResult) -> None:
        job.handle._finish(result)
        if job.callback:
            try:
                job.callback(result)
            except Exception:
                logger.exception("TTS 송신 완료 콜백 실패")

    def submit(self, job: _SendJob) -> DeliveryHandle:
        self.start()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            # 오래된 미전송 경보를 버리고 최신 상황을 남깁니다.
            try:
                old = self._queue.get_nowait()
                self._queue.task_done()
                dropped = DeliveryResult(
                    False, old.handle.message_id, "send_queue_dropped",
                    "newer alert replaced this request")
                self._finish_job(old, dropped)
                with self._lock:
                    self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                dropped = DeliveryResult(
                    False, job.handle.message_id, "send_queue_dropped",
                    "sender queue is full")
                self._finish_job(job, dropped)
                with self._lock:
                    self._dropped += 1
                return job.handle
        with self._lock:
            self._submitted += 1
        return job.handle

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "submitted": self._submitted,
                "queued": self._queued,
                "failed": self._failed,
                "dropped": self._dropped,
                "pending": self._queue.qsize(),
            }

    def close(self, timeout: float = SEND_DRAIN_TIMEOUT_SECONDS) -> Dict[str, int]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout))
        return self.stats()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = send_alert_with_result(**job.kwargs)
                self._finish_job(job, result)
                with self._lock:
                    if result.ok:
                        self._queued += 1
                    else:
                        self._failed += 1
            except Exception as exc:
                result = DeliveryResult(
                    False, job.handle.message_id, "worker_error", str(exc))
                self._finish_job(job, result)
                with self._lock:
                    self._failed += 1
                logger.exception("TTS 송신 워커 오류")
            finally:
                self._queue.task_done()


_sender_worker: Optional[TtsSenderWorker] = None
_sender_worker_lock = threading.Lock()


def _get_sender_worker() -> TtsSenderWorker:
    global _sender_worker
    with _sender_worker_lock:
        if _sender_worker is None:
            _sender_worker = TtsSenderWorker()
        return _sender_worker


def send_alert_async(alert_text: str,
                     jetson_ip: Optional[str] = None,
                     port: Optional[int] = None,
                     timeout: float = CONNECT_TIMEOUT_SECONDS,
                     on_complete: Optional[Callable[[DeliveryResult], None]] = None,
                     **metadata) -> DeliveryHandle:
    """제한된 전용 워커에 전송을 등록하고 즉시 반환합니다."""
    message_id = str(metadata.pop("message_id", None) or uuid.uuid4().hex)
    handle = DeliveryHandle(message_id)
    kwargs = dict(
        jetson_ip=jetson_ip,
        alert_text=alert_text,
        port=port,
        timeout=timeout,
        message_id=message_id,
        **metadata,
    )
    return _get_sender_worker().submit(_SendJob(kwargs, handle, on_complete))


def send_plan_async(plan: Any,
                    on_complete: Optional[Callable[[DeliveryResult], None]] = None,
                    **transport) -> DeliveryHandle:
    """대응안을 표준 방송 문장으로 만들어 bounded worker에 등록합니다."""
    metadata = _plan_metadata(plan)
    metadata.update(transport)
    return send_alert_async(on_complete=on_complete, **metadata)


def get_tts_sender_stats() -> Dict[str, int]:
    with _sender_worker_lock:
        worker = _sender_worker
    if worker is None:
        return {
            "submitted": 0, "queued": 0, "failed": 0,
            "dropped": 0, "pending": 0,
        }
    return worker.stats()


def close_tts_sender(timeout: float = SEND_DRAIN_TIMEOUT_SECONDS) -> Dict[str, int]:
    with _sender_worker_lock:
        worker = _sender_worker
    if worker is None:
        return get_tts_sender_stats()
    return worker.close(timeout)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    # 단독 실행 시 동작 확인용. 실제 젯슨 IP는 환경변수로 주세요.
    #   JETSON_IP=203.0.113.10 python3 Rag_to_Jetson.py
    #   JETSON_IP=203.0.113.10 python3 Rag_to_Jetson.py "임의의 문장"
    #
    # 인자로 문장을 받는 이유: 스피커가 실제로 우는지 확인할 때 매번 같은 문장만
    # 나오면 "방금 그게 새 방송인지 아까 것인지" 를 구분할 수 없습니다.
    import sys

    demo_text = " ".join(sys.argv[1:]).strip() or (
        "A구역 작업자님, 위험 구역입니다. 즉시 안전모를 착용해 주세요.")
    ok = send_alert_to_jetson(alert_text=demo_text)
    raise SystemExit(0 if ok else 1)
