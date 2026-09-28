r"""
test_tts_pipeline.py — TTS 송수신 자체 테스트

piper / aplay / 젯슨 하드웨어 없이, 실제 TCP 소켓 왕복으로 검증합니다.
수신기의 재생 콜백(on_text)을 가로채서 "무엇이 재생될 뻔했는지"만 확인하므로
Windows에서도 그대로 돌아갑니다.

실행:
    python test_tts_pipeline.py

모든 줄에 OK가 뜨면 통과입니다.
"""

import os
import socket
import sys
import tempfile
import threading
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 기본 콘솔은 cp949 라서 이 파일의 em대시(—)와 Rx_pipeline 의 로그 이모지
# (⚠ 📢 🔊)를 인코딩하지 못하고, print 한 줄에서 UnicodeEncodeError 로 테스트가
# 통째로 죽습니다(로직은 전부 통과하는데도). 이상민 폴더에는 같은 일을 하는
# common/console.py 가 있지만, 이 폴더는 그 트리에 의존하지 않으므로 여기서 직접 합니다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

import Rag_to_Jetson as tx
import Rx_pipeline as rx


def check(desc, condition, detail=""):
    """detail 은 실패했을 때만 보여줍니다 — 통과 목록이 지저분해지지 않게."""
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


class Receiver:
    """테스트용 수신기. 받은 문장을 리스트에 모읍니다."""

    def __init__(self):
        self.received = []
        self.port = self._free_port()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=rx.start_tts_receiver,
            kwargs=dict(on_text=self.received.append, bind_ip="127.0.0.1",
                        port=self.port, ready_event=self._ready),
            daemon=True,
        )

    @staticmethod
    def _free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def __enter__(self):
        self._thread.start()
        self._ready.wait(timeout=5)
        return self

    def __exit__(self, *exc):
        return False

    def wait_for(self, n, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.received) < n:
            time.sleep(0.02)
        return len(self.received) >= n


_SETUP_SH = ""
_setup_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_tts.sh")
if os.path.exists(_setup_path):
    with open(_setup_path, encoding="utf-8") as _f:
        _SETUP_SH = _f.read()


# 저장소 루트. 이 테스트는 "TTS Engine and pipeline/" 에서도, 젯슨 배포본의
# "smart_factory_project/tts/" 에서도 실행될 수 있으므로 둘 다 견디게 찾습니다.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = next(
    (p for p in (os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE)))
     if os.path.isfile(os.path.join(p, "README.md"))),
    os.path.dirname(_HERE))


def _read(*parts):
    """저장소 안의 파일을 문자열로. 없으면 빈 문자열(배포본에는 없을 수 있음)."""
    path = os.path.join(_REPO_ROOT, *parts)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    print("=== parse_payload() — 순수 파싱 ===")
    check("JSON에서 text 추출",
          rx.parse_payload(b'{"text": "\xed\x99\x94\xec\x9e\xac"}') == "화재")
    check("평문도 그대로 받음", rx.parse_payload("그냥 문장".encode()) == "그냥 문장")
    check("빈 입력은 빈 문자열", rx.parse_payload(b"") == "")
    check("개행/중복공백 정리",
          rx.parse_payload('{"text": "A  구역\\n위험"}'.encode()) == "A 구역 위험")
    volume_request = rx.parse_request(
        b'{"text":"alert","volume_percent":180}')
    check("송신한 TTS 음량을 수신 요청에 보존",
          volume_request.volume_percent == 180)
    loud_request = rx.parse_request(
        b'{"text":"alert","volume_percent":999}')
    check("TTS 음량을 400%로 제한", loud_request.volume_percent == 400)
    speaker_request = rx.parse_request(
        b'{"command":"set_speaker_output_volume","speaker_volume_percent":37}')
    check("스피커 출력 제어 명령 파싱",
          speaker_request.command == "set_speaker_output_volume"
          and speaker_request.speaker_volume_percent == 37)
    speaker_limit_request = rx.parse_request(
        b'{"command":"set_speaker_output_volume","speaker_volume_percent":999}')
    check("스피커 출력은 100%로 제한", speaker_limit_request.speaker_volume_percent == 100)

    # 실제 PCM 샘플이 증폭되는지 확인합니다. 프로토콜에 숫자만 실리고
    # Jetson 재생 직전 파일은 그대로인 반쪽짜리 구현을 막습니다.
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="tts_gain_test_")
    os.close(wav_fd)
    try:
        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes((1000).to_bytes(2, "little", signed=True) * 8)
        applied = rx._apply_wav_volume(wav_path, 400)
        with wave.open(wav_path, "rb") as wav_file:
            first_sample = int.from_bytes(
                wav_file.readframes(1), "little", signed=True)
        check("Jetson WAV 재생 직전 400% 실제 증폭",
              applied and first_sample == 4000, str(first_sample))

        # 4배로 곱한 뒤 포화시키면 약한 구간까지 모두 최대치가 되어 목소리가
        # 찌그러집니다. 새 구현은 전체 피크 기준으로 이득을 낮춰 파형 비율을
        # 유지합니다(20000:10000 = 약 2:1).
        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(
                (20000).to_bytes(2, "little", signed=True)
                + (10000).to_bytes(2, "little", signed=True))
        applied = rx._apply_wav_volume(wav_path, 400)
        with wave.open(wav_path, "rb") as wav_file:
            first, second = (
                int.from_bytes(wav_file.readframes(1), "little", signed=True),
                int.from_bytes(wav_file.readframes(1), "little", signed=True))
        check("큰 원본은 포화 왜곡 없이 피크 기준으로 증폭",
              applied and first == 32767 and 16380 <= second <= 16385,
              f"{first}, {second}")
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    long_text = "가" * 900
    check("MAX_TEXT_CHARS로 절삭",
          len(rx.parse_payload(('{"text": "%s"}' % long_text).encode())) == rx.MAX_TEXT_CHARS)

    # 바이트 상한에서 멀티바이트 문자가 잘려도 예외 없이 처리되는지
    truncated = '{"text": "안녕하세요'.encode("utf-8")[:-1]
    try:
        parsed_truncated = rx.parse_payload(truncated)
        ok = parsed_truncated == ""
    except Exception:
        ok = False
    check("깨진 UTF-8이 들어와도 예외 없음", ok)
    check("객체/문자열이 아닌 JSON은 방송하지 않음",
          rx.parse_payload(b"[]") == "")

    print("\n=== 명령어 주입 방어 ===")
    # 예전 구현은 이 문자열을 f-string으로 셸 명령에 끼워넣어 실행했습니다.
    # 이제 파싱 단계에서 그냥 평범한 텍스트로만 다뤄지는지 확인합니다.
    evil = '"; touch /tmp/pwned; echo "'
    parsed = rx.parse_payload(('{"text": %s}' % __import__("json").dumps(evil)).encode())
    check("셸 메타문자가 텍스트로만 남음", parsed == evil.strip())
    # 문자열 검색은 주석/독스트링까지 잡아 오탐이 나므로, AST로 실제 호출만 봅니다.
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(rx.speak_with_piper)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def call_name(node):
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            return f"{base}.{f.attr}" if base else f.attr
        if isinstance(f, ast.Name):
            return f.id
        return ""

    names = {call_name(c) for c in calls}
    check("os.system 호출 없음", "os.system" not in names)
    check("os.popen 호출 없음", "os.popen" not in names)

    shell_true = any(
        kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for c in calls for kw in c.keywords
    )
    check("shell=True 없음", not shell_true)

    run_calls = [c for c in calls if call_name(c) == "subprocess.run"]
    check("외부 명령이 안전한 subprocess.run만 사용 (piper, aplay, amixer)",
          len(run_calls) >= 2)
    # 첫 인자가 문자열이면 셸 문법으로 조립했다는 뜻입니다. 리스트든
    # 리스트끼리의 연결(aplay_cmd + [path])이든, 문자열만 아니면 안전합니다.
    check("명령을 문자열로 조립하지 않음 — 셸이 개입할 여지 없음",
          all(c.args
              and not isinstance(c.args[0], ast.JoinedStr)          # f-string
              and not (isinstance(c.args[0], ast.Constant)
                       and isinstance(c.args[0].value, str))
              for c in run_calls))
    check("  인자가 리스트에서 출발함",
          all(isinstance(c.args[0], (ast.List, ast.Name, ast.BinOp)) for c in run_calls))
    check("모든 외부 호출에 timeout 지정",
          all(any(kw.arg == "timeout" for kw in c.keywords) for c in run_calls))

    print("\n=== 실제 TCP 왕복 ===")
    with Receiver() as r:
        ok = tx.send_alert_to_jetson("127.0.0.1", "테스트 경보입니다", port=r.port)
        check("송신이 True 반환", ok is True)
        check("수신 성공", r.wait_for(1))
        check("내용 일치", r.received[0] == "테스트 경보입니다")

        # 예전 recv(1024)로는 반드시 잘리던 길이 (한글 500자 = 1500바이트)
        long_ko = "안" * 500
        tx.send_alert_to_jetson("127.0.0.1", long_ko, port=r.port)
        check("한글 500자 수신", r.wait_for(2))
        check("잘리지 않고 500자 그대로 도착",
              len(r.received[1]) == 500 and r.received[1] == long_ko)

        # 연속 전송이 순서대로 다 들어오는지
        for i in range(5):
            tx.send_alert_to_jetson("127.0.0.1", f"연속 {i}", port=r.port)
        check("연속 5건 모두 수신", r.wait_for(7))
        check("순서 유지", [t for t in r.received[2:7]] == [f"연속 {i}" for i in range(5)])

    print("\n=== 관리자용 대응안과 현장 방송 분리 ===")
    plan = {
        "zone": "A",
        "detected_object": "fire",
        "risk_level": "위험",
        "recommended_action": "관리자용 상세 대응안입니다. 전원을 차단하고 소방서에 신고한 뒤 "
                              "대피 유도와 초기 소화 가능 여부를 확인하십시오.",
    }
    speech = tx.build_speech_text(plan)
    check("TTS는 관리자용 상세 대응안을 읽지 않는 짧은 화재 문장",
          speech == "A 구역 화재 발생. 즉시 대피하십시오.", speech)
    check("화재는 긴급 즉시 방송 트리거", tx.is_emergency_tts_event(plan))
    check("안전모 미착용은 긴급 5회 대피 방송 트리거가 아님",
          not tx.is_emergency_tts_event({
              "zone": "A", "detected_object": "안전모 미착용", "risk_level": "위험"}))
    check("정상 fire 라벨은 TTS 문장을 만들지 않음",
          tx.build_speech_text({
              "zone": "A", "detected_object": "fire", "risk_level": "정상"}) == "")
    check("위험이어도 일반 작업자는 TTS 문장을 만들지 않음",
          tx.build_speech_text({
              "zone": "A", "detected_object": "person", "risk_level": "위험"}) == "")
    helmet = {"zone": "B", "detected_object": "안전모 미착용", "risk_level": "위험"}
    check("안전모 미착용 위험일 때만 짧은 TTS 문장 생성",
          tx.build_speech_text(helmet) == "B 구역 안전모 미착용. 작업을 중지하십시오.")
    sent = []
    original_send_async = tx.send_alert_async
    try:
        def fake_send_async(text, **kwargs):
            sent.append((text, kwargs))
            return object()

        tx.send_alert_async = fake_send_async
        handles = tx.send_emergency_alert_async(plan)
        check("긴급 TTS는 정확히 5회 등록", len(handles) == len(sent) == 5)
        check("5회 모두 짧고 같은 화재 대피 문장",
              all(text == speech and kwargs.get("priority") == 100 for text, kwargs in sent))
        sent.clear()
        handles = tx.send_detection_alert_async(helmet)
        check("안전모 미착용 경고는 1회만 등록",
              len(handles) == len(sent) == 1)
        sent.clear()
        handles = tx.send_detection_alert_async({
            "zone": "B", "detected_object": "안전모 미착용", "risk_level": "주의"})
        check("안전모 미착용이 위험으로 확정되지 않으면 송신 없음",
              handles == [] and sent == [])
    finally:
        tx.send_alert_async = original_send_async
    check("화재가 작업자·차량보다 높은 우선순위",
          tx.priority_for_object("fire") >
          tx.priority_for_object("person with no helmet") >
          tx.priority_for_object("vehicle"))

    print("\n=== message_id ACK + 재시도 중복 방지 + 만료 ===")
    with Receiver() as r:
        first = tx.send_alert_with_result(
            "127.0.0.1", "중복되면 안 됩니다", port=r.port,
            retries=0, message_id="same-message")
        second = tx.send_alert_with_result(
            "127.0.0.1", "중복되면 안 됩니다", port=r.port,
            retries=0, message_id="same-message")
        check("첫 요청은 queued ACK", first.ok and first.status == "queued")
        check("같은 message_id 재전송은 duplicate ACK",
              second.ok and second.status == "duplicate")
        check("중복 요청은 한 번만 콜백 실행", r.received == ["중복되면 안 됩니다"])

        expired = tx.send_alert_with_result(
            "127.0.0.1", "이미 늦은 경보", port=r.port, retries=0,
            message_id="expired-message", created_at=time.time() - 20,
            expires_at=time.time() - 1)
        check("만료된 요청은 expired ACK로 거부",
              not expired.ok and expired.status == "expired")
        check("만료된 요청은 재생 콜백에 들어가지 않음",
              r.received == ["중복되면 안 됩니다"])

    print("\n=== 송신 실패 처리 ===")
    dead_port = Receiver._free_port()      # 아무도 안 듣는 포트
    ok = tx.send_alert_to_jetson("127.0.0.1", "아무도 없음",
                                 port=dead_port, timeout=0.3, retries=0)
    check("연결 실패 시 False 반환 (예외 전파 안 함)", ok is False)
    check("빈 문장은 보내지 않고 False",
          tx.send_alert_to_jetson("127.0.0.1", "   ", port=dead_port) is False)

    print("\n=== 비동기 송신이 호출자를 막지 않는지 ===")
    t0 = time.time()
    th = tx.send_alert_async("비동기", jetson_ip="127.0.0.1", port=dead_port, timeout=2.0)
    elapsed = time.time() - t0
    check(f"즉시 반환 ({elapsed*1000:.0f}ms < 200ms)", elapsed < 0.2)
    th.join(timeout=5)

    print("\n=== bounded 송신 워커 ===")
    tx.close_tts_sender(timeout=1)
    tx._sender_worker = tx.TtsSenderWorker(queue_size=2)
    original_send = tx.send_alert_with_result
    gate = threading.Event()
    started = threading.Event()

    def fake_send(**kwargs):
        started.set()
        gate.wait(timeout=3)
        return tx.DeliveryResult(
            True, kwargs["message_id"], "queued")

    try:
        tx.send_alert_with_result = fake_send
        handles = [tx.send_alert_async("워커 0")]
        check("첫 송신이 워커에서 처리 시작", started.wait(timeout=1))
        handles.extend(tx.send_alert_async(f"워커 {i}") for i in range(1, 4))
        dropped = handles[1].result(timeout=1)
        check("큐 포화 시 가장 오래 기다린 전송을 드롭",
              dropped is not None and dropped.status == "send_queue_dropped")
        gate.set()
        stats = tx.close_tts_sender(timeout=3)
        check("한 개 worker가 나머지 요청을 처리",
              stats["queued"] == 3 and stats["dropped"] == 1, str(stats))
    finally:
        gate.set()
        tx.send_alert_with_result = original_send
        tx._sender_worker = None

    print("\n=== 송신 워커 동시 시작 방어 ===")
    worker = tx.TtsSenderWorker(queue_size=1)
    worker_gate = threading.Event()
    starts = []
    starts_lock = threading.Lock()

    def idle_worker():
        with starts_lock:
            starts.append(threading.get_ident())
        worker_gate.wait(timeout=3)

    worker._run = idle_worker
    starters = [threading.Thread(target=worker.start) for _ in range(12)]
    for starter in starters:
        starter.start()
    for starter in starters:
        starter.join(timeout=1)
    time.sleep(0.05)
    check("동시에 start()해도 송신 worker는 하나", len(starts) == 1, str(starts))
    worker_gate.set()
    worker.close(timeout=1)

    print("\n=== ACK 유실·상태 캐시 경계 조건 ===")

    class BrokenAckSocket:
        def sendall(self, _payload):
            raise ConnectionResetError("테스트용 ACK 연결 종료")

    ack_request = rx.SpeechRequest(message_id="ack-lost", text="화재")
    with rx._message_status_lock:
        rx._message_status.clear()
    rx._remember_message_status(ack_request.message_id, "queued")
    check("ACK 전송이 실패하면 False를 반환",
          rx._safe_send_ack(BrokenAckSocket(), ack_request, "queued") is False)
    check("ACK 유실만으로 이미 queued인 요청을 failed로 덮지 않음",
          rx.get_message_status(ack_request.message_id) == "queued")

    # status만 있고 message_id가 없는 ACK를 성공으로 믿으면 다른 요청의 ACK를
    # 잘못 연결할 수 있으므로 송신기는 v2 ID 일치를 강제합니다.
    bad_ack_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bad_ack_listener.bind(("127.0.0.1", 0))
    bad_ack_listener.listen(1)
    bad_ack_port = bad_ack_listener.getsockname()[1]

    def serve_bad_ack():
        conn, _addr = bad_ack_listener.accept()
        with conn:
            while conn.recv(4096):
                pass
            conn.sendall(b'{"status":"queued"}\n')
        bad_ack_listener.close()

    bad_ack_thread = threading.Thread(target=serve_bad_ack, daemon=True)
    bad_ack_thread.start()
    bad_ack_result = tx.send_alert_with_result(
        "127.0.0.1", "ACK ID 검사", port=bad_ack_port, retries=0,
        message_id="expected-id")
    bad_ack_thread.join(timeout=1)
    check("message_id가 없거나 다른 ACK는 전송 성공으로 인정하지 않음",
          not bad_ack_result.ok and bad_ack_result.status == "transport_error")

    saved_status_max = rx.MESSAGE_STATUS_MAX_ENTRIES
    try:
        rx.MESSAGE_STATUS_MAX_ENTRIES = 3
        with rx._message_status_lock:
            rx._message_status.clear()
        for index in range(5):
            rx._remember_message_status(f"status-{index}", "queued")
        with rx._message_status_lock:
            remembered_ids = list(rx._message_status)
        check("message_id 상태 캐시는 설정 상한을 넘지 않음",
              len(remembered_ids) == 3, str(remembered_ids))
        check("상태 캐시 포화 시 가장 오래된 ID부터 제거",
              remembered_ids == ["status-2", "status-3", "status-4"],
              str(remembered_ids))
    finally:
        rx.MESSAGE_STATUS_MAX_ENTRIES = saved_status_max
        with rx._message_status_lock:
            rx._message_status.clear()

    print("\n=== 재생 큐 — 오래된 경보를 버리는지 ===")
    import queue
    rx._speak_queue = queue.Queue(maxsize=2)   # 워커 없이 큐만 확인
    for i in range(4):
        rx.enqueue_speech(f"경보 {i}")
    drained = []
    while not rx._speak_queue.empty():
        drained.append(rx._speak_queue.get_nowait())
    check("큐 크기 상한 유지", len(drained) == 2)
    check("최신 경보가 남음 (오래된 것부터 버림)", drained == ["경보 2", "경보 3"])

    print("\n=== 우선순위·유효시간 재생 큐 ===")
    now = time.time()

    def request(mid, text, priority, created_offset=0, expires_offset=60):
        return rx.SpeechRequest(
            message_id=mid, text=text, priority=priority,
            created_at=now + created_offset,
            expires_at=now + expires_offset)

    pq = rx.SpeechPriorityQueue(maxsize=3)
    pq.put_request(request("low", "차량", 60, 0))
    pq.put_request(request("mid", "안전모", 80, 1))
    pq.put_request(request("high", "화재", 100, 2))
    order = [pq.get_nowait().message_id for _ in range(3)]
    check("화재 > 작업자 > 차량 순으로 꺼냄", order == ["high", "mid", "low"])

    pq = rx.SpeechPriorityQueue(maxsize=2)
    pq.put_request(request("vehicle", "차량", 60, 0))
    pq.put_request(request("worker", "작업자", 80, 1))
    replaced = pq.put_request(request("fire", "화재", 100, 2))
    check("포화 시 낮은 우선순위 경보를 교체",
          replaced.status == "queued" and replaced.dropped_message_id == "vehicle")
    check("만료된 경보는 큐에 들어가지 않음",
          pq.put_request(request("old", "과거", 100, 0, -1)).status == "expired")

    print("\n=== 느린 클라이언트가 진짜 경보를 막지 않는지 ===")
    # 예전에는 accept 루프가 연결을 하나씩 직렬로 처리해서, 1바이트씩 천천히
    # 흘리는 연결 하나만 붙어 있어도 그동안 도착한 경보를 통째로 놓쳤습니다.
    # 게다가 송신 측은 TCP backlog 에 들어갔으므로 "송신 성공"을 찍어서,
    # 경보가 안 울린 줄도 몰랐습니다.
    with Receiver() as r:
        def dribble():
            try:
                c = socket.create_connection(("127.0.0.1", r.port))
                for _ in range(4):
                    c.send(b"a")
                    time.sleep(rx.RECV_TIMEOUT_SECONDS * 0.8)
            except OSError:
                pass

        for _ in range(3):
            threading.Thread(target=dribble, daemon=True).start()
        time.sleep(0.5)

        t0 = time.time()
        tx.send_alert_to_jetson("127.0.0.1", "진짜 화재 경보", port=r.port, retries=0)
        arrived = r.wait_for(1, timeout=3.0)
        elapsed = time.time() - t0
        check(f"느린 연결 3개가 있어도 경보가 도착 ({elapsed*1000:.0f}ms)", arrived)
        check("내용 일치", r.received[0] == "진짜 화재 경보")

    print("\n=== 요청 전체 마감시각 (settimeout 만으로는 못 막음) ===")
    check("_recv_until_close 가 deadline 을 받음",
          "deadline" in rx._recv_until_close.__code__.co_varnames)
    with Receiver() as r:
        c = socket.create_connection(("127.0.0.1", r.port))
        c.send(b"a")
        t0 = time.time()
        # 서버가 마감시각에 연결을 끊으면 recv 가 EOF/에러로 풀립니다.
        c.settimeout(rx.RECV_TIMEOUT_SECONDS + 3)
        try:
            while c.recv(64):
                pass
        except OSError:
            pass
        held = time.time() - t0
        c.close()
        check(f"아무것도 더 안 보내면 {rx.RECV_TIMEOUT_SECONDS:g}초쯤에 끊김 ({held:.1f}초)",
              held < rx.RECV_TIMEOUT_SECONDS + 2)

    print("\n=== 접근 제한 (스피커는 액추에이터입니다) ===")
    check("기본값은 제한 없음 — 예전 동작 유지",
          rx.TTS_TOKEN == "" and rx.TTS_ALLOWED_IPS == set())
    check("제한이 없으면 아무 요청이나 통과", rx.is_authorized("203.0.113.60", ""))

    saved_token, saved_ips = rx.TTS_TOKEN, rx.TTS_ALLOWED_IPS
    try:
        rx.TTS_TOKEN = "s3cret"
        check("토큰을 켜면 토큰 없는 요청은 거부", not rx.is_authorized("127.0.0.1", ""))
        check("틀린 토큰도 거부", not rx.is_authorized("127.0.0.1", "wrong"))
        check("맞는 토큰은 통과", rx.is_authorized("127.0.0.1", "s3cret"))

        rx.TTS_ALLOWED_IPS = {"127.0.0.1"}
        check("허용 목록 밖의 IP 는 토큰이 맞아도 거부",
              not rx.is_authorized("203.0.113.60", "s3cret"))
        rx.TTS_ALLOWED_IPS = set()

        # 실제 소켓 왕복으로도 확인
        with Receiver() as r:
            tx_token_saved = tx.TTS_TOKEN
            try:
                tx.TTS_TOKEN = ""
                tx.send_alert_to_jetson("127.0.0.1", "토큰 없이", port=r.port, retries=0)
                time.sleep(0.4)
                check("토큰 없는 문장은 재생 큐에 들어가지 않음", r.received == [])

                tx.TTS_TOKEN = "s3cret"
                tx.send_alert_to_jetson("127.0.0.1", "토큰 있음", port=r.port, retries=0)
                check("토큰이 맞으면 재생됨", r.wait_for(1))
                check("  내용 일치", r.received[0] == "토큰 있음")
            finally:
                tx.TTS_TOKEN = tx_token_saved
    finally:
        rx.TTS_TOKEN, rx.TTS_ALLOWED_IPS = saved_token, saved_ips

    print("\n=== parse_message — 토큰 분리 ===")
    text, token = rx.parse_message(b'{"text": "\xed\x99\x94\xec\x9e\xac", "token": "abc"}')
    check("문장과 토큰을 함께 뽑음", text == "화재" and token == "abc")
    check("평문은 토큰이 빈 문자열", rx.parse_message("그냥 문장".encode())[1] == "")
    check("parse_payload 는 문장만 (기존 호환)",
          rx.parse_payload(b'{"text": "x", "token": "abc"}') == "x")

    print("\n=== USB 스피커 찾기 ===")
    # 젯슨 실물에서 흔한 aplay -l 출력. HDMI(온보드)와 USB 스피커가 함께 잡힌다.
    aplay_both = (
        "**** List of PLAYBACK Hardware Devices ****\n"
        "card 0: tegrahda [tegra-hda], device 3: HDMI 0 [HDMI 0]\n"
        "  Subdevices: 1/1\n"
        "card 1: Device [USB Audio Device], device 0: USB Audio [USB Audio]\n"
        "  Subdevices: 1/1\n")
    devices = rx.parse_aplay_devices(aplay_both)
    check("두 장치를 모두 파싱", len(devices) == 2, str(len(devices)))
    check("  카드 번호/이름을 뽑음",
          devices[0]["card"] == 0 and devices[1]["card_id"] == "Device")
    check("  장치 번호도 뽑음", devices[0]["device"] == 3 and devices[1]["device"] == 0)

    chosen = rx.pick_playback_device(devices)
    check("**HDMI 가 아니라 USB 를 고른다**", chosen["card_id"] == "Device", str(chosen))
    check("  카드 **이름**으로 장치를 지정한다 (번호는 재부팅마다 바뀜)",
          rx.format_alsa_device(chosen) == "plughw:CARD=Device,DEV=0",
          rx.format_alsa_device(chosen))
    check("  hw: 가 아니라 plughw: (piper 22050Hz -> 스피커 48000Hz 리샘플링)",
          rx.format_alsa_device(chosen).startswith("plughw:"))

    # USB 를 다시 꽂아 카드 번호가 뒤바뀐 경우. 이름 기반이라 결과가 같아야 한다.
    aplay_swapped = (
        "card 0: Device [USB Audio Device], device 0: USB Audio [USB Audio]\n"
        "card 1: tegrahda [tegra-hda], device 3: HDMI 0 [HDMI 0]\n")
    swapped = rx.pick_playback_device(rx.parse_aplay_devices(aplay_swapped))
    check("카드 번호가 바뀌어도 같은 스피커를 가리킨다",
          rx.format_alsa_device(swapped) == "plughw:CARD=Device,DEV=0")

    # USB 가 없으면 HDMI 라도 골라야 한다 (무음보다 낫고, 경고는 따로 나간다)
    hdmi_only = "card 0: tegrahda [tegra-hda], device 3: HDMI 0 [HDMI 0]\n"
    hdmi = rx.pick_playback_device(rx.parse_aplay_devices(hdmi_only))
    check("USB 가 없으면 HDMI 라도 고른다", hdmi is not None and hdmi["card"] == 0)

    check("장치가 없으면 None", rx.pick_playback_device([]) is None)
    check("쓰레기 입력에도 안 죽는다", rx.parse_aplay_devices("아무것도 없음") == [])

    print("\n=== PulseAudio USB sink 선택 ===")
    # 실제 Jetson에서는 USB 스피커가 있어도 PulseAudio 기본 sink가 내장
    # 아날로그로 잡혀 있었습니다. aplay -D pulse가 기본 sink를 그대로 쓰지
    # 않고 USB sink를 명시하도록 검사합니다.
    pulse_sinks = (
        "0\talsa_output.usb-Jieli_Technology_UACDemoV1.0-00.analog-stereo\t"
        "module-alsa-card.c\ts16le 2ch 48000Hz\tIDLE\n"
        "1\talsa_output.platform-sound.analog-stereo\t"
        "module-alsa-card.c\ts16le 2ch 44100Hz\tIDLE\n")
    original_run = rx.subprocess.run
    try:
        class PulseResult:
            returncode = 0
            stdout = pulse_sinks.encode()
            stderr = b""

        rx.subprocess.run = lambda *_args, **_kwargs: PulseResult()
        sink = rx.find_pulse_usb_sink()
    finally:
        rx.subprocess.run = original_run
    check("PulseAudio 기본 sink가 달라도 USB sink를 선택",
          sink == "alsa_output.usb-Jieli_Technology_UACDemoV1.0-00.analog-stereo", sink)
    cmd, playback_env = rx._aplay_command("/tmp/test.wav", "pulse://" + sink)
    check("USB Pulse sink를 지정해 aplay 실행",
          cmd == ["aplay", "-q", "-D", "pulse", "/tmp/test.wav"]
          and playback_env.get("PULSE_SINK") == sink,
          str((cmd, playback_env and playback_env.get("PULSE_SINK"))))

    print("\n=== 설정 일관성 ===")
    check("송/수신이 같은 TTS_TCP_PORT 기본값", tx.TTS_PORT == rx.TTS_PORT == 9997)
    check("송/수신이 같은 TTS_TOKEN 을 읽음", tx.TTS_TOKEN == rx.TTS_TOKEN)
    check("젯슨 IP 기본값이 대시보드와 동일(203.0.113.10)",
          tx.JETSON_IP == os.environ.get("JETSON_IP", "203.0.113.10"))
    check("모델 경로가 절대경로", os.path.isabs(rx.MODEL_PATH))
    # Piper 공식 저장소에 있는 한국어 음성은 kss 하나뿐입니다. 예전 기본값이던
    # hyeri 는 거기 없어서, 받아도 파일명이 안 맞아 조용히 무음이 됐습니다.
    check("기본 모델이 실재하는 음성(ko_KR-kss-medium)",
          bool(os.environ.get("PIPER_MODEL_PATH"))
          or "ko_KR-kss-medium" in os.path.basename(rx.MODEL_PATH),
          os.path.basename(rx.MODEL_PATH))
    check("setup_tts.sh 가 .onnx 와 .onnx.json 을 둘 다 받는다",
          _SETUP_SH.count("VOICE.onnx") >= 1 and "VOICE.onnx.json" in _SETUP_SH)
    check("setup_tts.sh 가 장치 선택을 Rx_pipeline 에서 가져온다 (규칙 이중화 방지)",
          "detect_playback_device" in _SETUP_SH)

    print("\n=== 안내 문서가 실재하지 않는 음성을 가리키지 않는가 ===")
    # 08-29 에 기본 음성을 kss 로 바꿨는데 안내 세 곳이 hyeri 로 남아 있었습니다.
    # set_env.example.sh 를 source 하면 PIPER_MODEL_PATH 가 **세션 전체에서** 없는
    # 파일을 가리켜, setup_tts.sh 로 모델을 제대로 받아 놓고도 무음이 됩니다.
    # 코드가 아니라 문서에서 갈라진 것이라 기존 테스트가 하나도 잡지 못했습니다.
    voice = "ko_KR-kss-medium"
    for line in _SETUP_SH.splitlines():
        if line.startswith("VOICE="):
            voice = line.split(":-")[-1].split("}")[0] or voice
            break
    check("setup_tts.sh 가 받는 음성 이름을 읽음 (" + voice + ")", voice.startswith("ko_KR-"))

    for name, parts in (("README.md", ("README.md",)),
                        ("TTS README", ("TTS Engine and pipeline", "README.md")),
                        ("set_env.example.sh",
                         ("smart_factory_project", "set_env.example.sh"))):
        doc = _read(*parts)
        if not doc:
            continue    # 배포본에 그 파일이 없는 경우는 검사 대상이 아님
        stale = [ln.strip() for ln in doc.splitlines()
                 if "PIPER_MODEL_PATH" in ln and ".onnx" in ln and voice not in ln]
        check(name + " 의 PIPER_MODEL_PATH 안내가 " + voice + " 를 가리킴",
              not stale, stale[0] if stale else "")

    print("\n=== TTS 송신 모듈 경로 후보 ===")
    # 젯슨 배포본은 ~/smart_factory_project/{pipeline, tts} 입니다. 예전에는 저장소
    # 레이아웃(상위 폴더의 "TTS Engine and pipeline")만 찾아서 **젯슨에서는 항상**
    # 실패했고, 실패해도 경고 한 줄로 끝나 파이프라인은 정상으로 보였습니다.
    for name, parts in (("파이프라인",
                         ("smart_factory_project", "pipeline", "patrol_pipeline_rag.py")),
                        ("대시보드",
                         ("데이터 플랫폼 및 대시보드(이상민)", "dashboard",
                          "smart_factory_dashboard_v3.py"))):
        src = _read(*parts)
        if not src:
            continue
        check(name + " 이 옆의 tts/ 를 먼저 찾는다", '_PROJECT_ROOT, "tts"' in src)
        check("  " + name + " 이 저장소 레이아웃도 계속 지원한다",
              '"TTS Engine and pipeline"' in src)
        check("  " + name + " 이 TTS_MODULE_DIR 로 덮어쓸 수 있다", "TTS_MODULE_DIR" in src)

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
