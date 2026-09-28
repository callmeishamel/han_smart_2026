import cv2
import numpy as np
import socket
import json
import math
import os
import threading
import time
from PIL import Image
from nanoowl.owl_predictor import OwlPredictor
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# 스테레오 캘리브레이션은 있으면 쓰고 없으면 건너뜁니다.
#
# import 를 감싸는 이유: 이 파일은 젯슨에 손으로 복사해 붙여넣는 일이 많습니다.
# 그때 stereo_calibrate.py 를 같이 옮기지 않으면 ImportError 로 **탐지 전체가**
# 죽습니다. 거리 정확도를 잃는 것과 아무것도 못 보는 것은 다른 문제입니다.
try:
    from stereo_calibrate import load_rectifier
except ImportError:
    load_rectifier = None

try:
    from stereo_calibrate import CALIBRATION_PATH
except ImportError:
    # 구버전/누락 배포에서도 사용자가 명시한 경로만큼은 로그에 보여줍니다.
    CALIBRATION_PATH = os.getenv("STEREO_CALIBRATION_FILE", "")

# 카메라가 요청한 2560x720을 거부하고 1280x720 모노 프레임을 내놓는 경우를
# 잡습니다. 이 검사가 없으면 모노 영상의 좌우 절반을 서로 다른 카메라로 오인해
# NanoOWL 영상과 StereoSGBM 거리 모두 그럴듯한 모양의 잘못된 값을 냅니다.
# 구버전 stereo_calibrate.py만 복사된 현장 배포도 탐지 자체는 계속 돌 수 있도록
# 별도 import로 분리하고, 아래에서 같은 최소 비율 검사를 폴백으로 수행합니다.
try:
    from stereo_calibrate import check_stereo_frame
except ImportError:
    check_stereo_frame = None

# ==========================================
# 0. 전역 변수 및 화질 제어 파라미터
# ==========================================
# 카메라가 방금 읽은 원본 프레임(박스 없음)과, 추론이 마지막으로 찾아낸 박스를
# **따로** 들고 있습니다.
#
# 예전에는 추론 루프가 박스까지 그린 완성 프레임 하나(output_frame_rgb)만
# 내놓았습니다. 그러면 스트림 FPS 가 추론 FPS 에 그대로 묶입니다 — NanoOWL 추론과
# StereoSGBM 이 프레임당 수백 ms 씩 걸리므로, 카메라가 30fps 로 들어와도 화면은
# 그 몇 분의 1 로 뚝뚝 끊겨 보였습니다.
#
# 이제 캡처 스레드가 최신 프레임을 계속 갱신하고, 스트리밍 쪽이 "가장 최근 영상 +
# 가장 최근 박스"를 합쳐서 내보냅니다. 영상은 카메라 속도로 흐르고, 박스만 추론
# 속도로 갱신됩니다. 대신 움직이는 물체에서는 박스가 실제 위치보다 추론 1회분
# 늦게 따라붙습니다 — 화면이 끊기는 것보다 이쪽이 낫다고 판단했습니다.
latest_video_frame = None      # 좌안 원본 (스트리밍용)
latest_right_frame = None      # 우안 원본 (시차 계산용)
frame_seq = 0                  # 새 프레임이 왔는지 추론 루프가 판별하는 용도
frame_lock = threading.Lock()

latest_overlays = []           # [(xmin, ymin, xmax, ymax, info_text), ...]
overlay_seq = 0                # 박스가 바뀌었는지 인코딩 캐시가 판별하는 용도
overlay_lock = threading.Lock()

# /health 로 내보낼 상태. 대시보드나 사람이 "젯슨이 죽은 건지, 살아 있는데
# 탐지가 없는 건지"를 구분할 수 있어야 합니다. 영상만 봐서는 구분이 안 됩니다
# (카메라가 빠져도 마지막 프레임이 계속 재전송되므로 화면은 멀쩡해 보입니다).
health_state = {
    "started_at": time.time(),
    "frames": 0,             # 추론 횟수 (ok / frame_age_sec 의 기준)
    "last_frame_at": 0.0,
    "video_frames": 0,       # 카메라 캡처 횟수 — 추론과 별개로 돕니다
    "last_video_at": 0.0,
    "camera_errors": 0,
    "last_detection_count": 0,
    "udp_errors": 0,
}
# main()에서 확정한 비민감 네트워크 목적지. /health에서 함께 보여 주어
# Docker -e 누락으로 영상만 뜨고 UDP가 엉뚱한 곳으로 가는 상태를 진단한다.
network_targets = {}
health_lock = threading.Lock()

# 속도 계측.
#
# "영상이 느리다" 는 원인이 여러 겹입니다 — 카메라가 애초에 느리게 붙었을 수도,
# rectify/JPEG 인코딩이 오래 걸릴 수도, 네트워크가 못 따라갈 수도 있습니다.
# 추측으로 만지면 엉뚱한 곳을 고치게 되므로 각 구간을 따로 잽니다.
# 결과는 /health 로 나갑니다.
perf_state = {
    "capture_interval_ms": 0.0,   # 프레임 하나 받는 주기 (= 실제 카메라 FPS)
    "read_ms": 0.0,               # cap.read() 자체가 블로킹된 시간
    "rectify_ms": 0.0,            # 좌우 정렬(remap) 비용
    "encode_ms": 0.0,             # JPEG 인코딩 비용
    "stereo_ms": 0.0,             # StereoSGBM 시차 계산 (추론 루프에서 가장 비쌈)
    "stereo_ran": 0,              # 실제로 계산한 횟수
    "stereo_skipped": 0,          # 탐지가 없어 건너뛴 횟수
    "stream_interval_ms": 0.0,    # 스트림이 실제로 프레임을 내보내는 주기
    "jpeg_bytes": 0,              # 마지막 프레임 크기
    "encode_reuse": 0,            # 캐시 재사용 횟수 (클라이언트가 여럿일 때)
    "encode_miss": 0,             # 실제로 인코딩한 횟수
    "stream_clients": 0,
}
perf_lock = threading.Lock()


def _blend(old: float, new: float, alpha: float = 0.2) -> float:
    """지수 이동 평균. 한 프레임이 튀어도 값이 요동치지 않게."""
    return new if old <= 0.0 else (1.0 - alpha) * old + alpha * new

# ✨ [화질 조절 포인트 1] JPEG 웹 스트리밍 인코딩 화질 (0~100)
#
# **화면이 느리면 여기부터 낮춰 보세요.** 1280x720 에서 q95 는 프레임당 200~400KB
# 라, 30fps 면 50~100Mbps 입니다. 무선이면 이것만으로 못 따라갑니다. q80 이면
# 용량이 절반쯤으로 줄고 인코딩도 빨라지는데, 관제 화면에서 눈에 띄는 차이는
# 거의 없습니다.
#   export JPEG_QUALITY=80
JPEG_QUALITY_SETTING = int(os.getenv("JPEG_QUALITY", "95"))

# 스트리밍 목표 FPS. 실제로는 인코딩 시간에 따라 이보다 낮아질 수 있습니다.
STREAM_TARGET_FPS = float(os.getenv("STREAM_FPS", "30"))
STREAM_FRAME_INTERVAL = 1.0 / max(1.0, STREAM_TARGET_FPS)

# 카메라에 요청할 FPS. 0 이면 요청하지 않고 드라이버 기본값을 씁니다.
#
# 이걸 안 주면 카메라가 2560x720 에서 제멋대로 협상합니다 — 많은 USB 스테레오
# 모듈이 이 해상도에서 15fps 나 그 이하로 붙고, 그러면 아래 파이프라인을 아무리
# 최적화해도 그 위로 못 올라갑니다.
CAMERA_TARGET_FPS = float(os.getenv("CAMERA_FPS", "30"))

# 대기 화면은 내용이 항상 같으므로 매 프레임 새로 그리지 않고 1회만 만들어 캐싱합니다.
_placeholder_cache = {}
_placeholder_lock = threading.Lock()

# 인코딩 결과 캐시.
#
# 예전에는 **클라이언트마다 따로** JPEG 을 만들었습니다. 대시보드 iframe 과 브라우저
# 탭을 같이 열면 같은 프레임을 두 번 인코딩하고, 그 앞에서 draw_overlays 가 매번
# 2.7MB 를 복사했습니다. 보는 사람이 늘수록 젯슨 CPU 가 그만큼 더 먹히고, 결국
# 모두에게 화면이 느려집니다.
#
# 같은 (프레임, 박스) 조합이면 결과가 완전히 같으므로 한 번만 만들어 나눠 씁니다.
_jpeg_cache = {"frame_seq": -1, "overlay_seq": -1, "data": b""}
_jpeg_lock = threading.Lock()


# 새 프레임이 없어도 이 간격마다 한 번은 다시 보냅니다.
# 아무것도 안 보내면 중간 프록시나 브라우저가 연결이 죽은 것으로 보고 끊을 수
# 있습니다. 화면은 어차피 마지막 프레임을 그대로 보여주므로 자주 보낼 필요는 없습니다.
STREAM_KEEPALIVE_SEC = float(os.getenv("STREAM_KEEPALIVE_SEC", "2.0"))


def get_stream_jpeg():
    """(프레임 식별자, JPEG 바이트).

    식별자는 (프레임 번호, 박스 번호) 입니다. 호출부가 **직전에 보낸 것과 같은지**
    판별해 같은 그림을 두 번 보내지 않도록 하기 위한 것입니다. 카메라가 15fps 인데
    스트림 목표가 30fps 면, 이 판별이 없으면 **같은 JPEG 을 두 번 보내** 대역폭만
    두 배로 씁니다(287KB 프레임이면 4.3MB/s -> 8.6MB/s). 무선에서는 그것만으로
    화면이 밀립니다.
    """
    with frame_lock:
        frame = latest_video_frame
        f_seq = frame_seq
    with overlay_lock:
        overlays = latest_overlays
        o_seq = overlay_seq

    if frame is None:
        return (-1, -1), get_placeholder_jpeg()

    key = (f_seq, o_seq)
    with _jpeg_lock:
        if _jpeg_cache["frame_seq"] == f_seq and _jpeg_cache["overlay_seq"] == o_seq:
            with perf_lock:
                perf_state["encode_reuse"] += 1
            return key, _jpeg_cache["data"]

    # 락 밖에서 인코딩합니다. 수~수십 ms 걸리는 무거운 연산이라, 락을 쥔 채로 하면
    # 다른 클라이언트가 그동안 통째로 멈춥니다. 그 사이 같은 프레임을 두 번
    # 인코딩할 수는 있지만(드묾), 서로를 막는 것보다 낫습니다.
    started = time.perf_counter()
    composed = draw_overlays(frame, overlays)
    ok, encoded = cv2.imencode(
        '.jpg', composed, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY_SETTING])
    data = encoded.tobytes() if ok else b''
    encode_ms = (time.perf_counter() - started) * 1000.0

    with _jpeg_lock:
        _jpeg_cache["frame_seq"] = f_seq
        _jpeg_cache["overlay_seq"] = o_seq
        _jpeg_cache["data"] = data
    with perf_lock:
        perf_state["encode_ms"] = _blend(perf_state["encode_ms"], encode_ms)
        perf_state["jpeg_bytes"] = len(data)
        perf_state["encode_miss"] += 1
    return key, data


def _in_container() -> bool:
    """Docker 컨테이너 안에서 도는지 대략 판별합니다.

    확실히 알 방법은 없어서 흔한 신호 두 가지만 봅니다. 틀려도 경고 문구가
    한 번 더 뜰 뿐이라 위험하지 않습니다.
    """
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r") as f:
            return "docker" in f.read() or "containerd" in f.read()
    except OSError:
        return False


# 같은 물체를 가리키는 박스로 볼 IoU 기준.
# 서로 다른 클래스는 겹쳐도 제거하지 않고, 같은 클래스의 중복만 제거한다.
DEDUP_IOU_THRESHOLD = float(os.getenv("DEDUP_IOU_THRESHOLD", "0.5"))

PERSON_LABEL = "person"
HELMET_LABEL = "safety helmet"
HELMET_VIOLATION_LABEL = "person with no helmet"
# NanoOWL은 긍정 객체인 safety helmet만 찾고, 출력은 기존 파이프라인
# 계약인 아래 네 라벨만 사용한다. safety helmet 박스 자체는 전송하지 않는다.
OUTPUT_LABELS = ["person with no helmet", "person", "fire", "vehicle"]

# 사람이 처음 보인 후 이 시간 동안 머리 영역에서 안전모가 한 번도
# 확인되지 않을 때만 미착용으로 확정한다. 현장 튜닝 없이 3~5초 요구의
# 중간값인 4초를 기본으로 쓴다.
PPE_NO_HELMET_CONFIRM_SEC = float(os.getenv(
    "PPE_NO_HELMET_CONFIRM_SEC", "4.0"))
PPE_HEAD_REGION_RATIO = float(os.getenv("PPE_HEAD_REGION_RATIO", "0.40"))
PPE_TRACK_IOU_THRESHOLD = float(os.getenv(
    "PPE_TRACK_IOU_THRESHOLD", "0.20"))
PPE_TRACK_STALE_SEC = float(os.getenv("PPE_TRACK_STALE_SEC", "1.5"))


def box_iou(a, b) -> float:
    """두 박스의 교집합 / 합집합. 겹치지 않으면 0."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0


def _label_name(prompt_names, label_idx):
    """라벨 인덱스 -> 프롬프트 문자열. 범위를 벗어나면 빈 문자열."""
    idx = int(label_idx)
    return prompt_names[idx] if 0 <= idx < len(prompt_names) else ""


def suppress_overlapping_detections(boxes, labels, scores, prompt_names,
                                    iou_threshold=DEDUP_IOU_THRESHOLD):
    """같은 클래스의 겹치는 박스만 신뢰도 순으로 하나로 줄인다.

    person 박스 안에 safety helmet 박스가 있어야 하므로 다른 클래스
    사이는 중복으로 보지 않는다.
    """
    count = len(boxes)
    if count == 0:
        return boxes, labels

    labels = np.array(labels).copy()

    if scores is None or len(scores) != count:
        scores = np.zeros(count, dtype=np.float32)
    else:
        scores = np.asarray(scores, dtype=np.float32)

    order = sorted(range(count), key=lambda i: float(scores[i]), reverse=True)

    keep = []
    for idx in order:
        box = boxes[idx]
        duplicate_of = next(
            (kept for kept in keep
             if int(labels[kept]) == int(labels[idx])
             and box_iou(box, boxes[kept]) >= iou_threshold), None)
        if duplicate_of is None:
            keep.append(idx)
            continue
    keep.sort()
    return boxes[keep], labels[keep]


def helmet_in_head_region(person_box, helmet_box,
                          head_ratio=PPE_HEAD_REGION_RATIO) -> bool:
    """helmet 중심이 사람 박스의 머리 영역에 들어오는지 확인한다."""
    px1, py1, px2, py2 = map(float, person_box)
    hx1, hy1, hx2, hy2 = map(float, helmet_box)
    width = max(0.0, px2 - px1)
    height = max(0.0, py2 - py1)
    if width <= 0.0 or height <= 0.0:
        return False

    helmet_cx = (hx1 + hx2) / 2.0
    helmet_cy = (hy1 + hy2) / 2.0
    side_padding = width * 0.08
    top_padding = height * 0.08
    head_bottom = py1 + height * max(0.1, min(0.7, float(head_ratio)))
    return (px1 - side_padding <= helmet_cx <= px2 + side_padding
            and py1 - top_padding <= helmet_cy <= head_bottom)


class HelmetComplianceTracker:
    """IoU로 사람을 이어 붙이며 안전모 미탐지 시간을 재다."""

    def __init__(self, confirm_sec=PPE_NO_HELMET_CONFIRM_SEC,
                 match_iou=PPE_TRACK_IOU_THRESHOLD,
                 stale_sec=PPE_TRACK_STALE_SEC):
        self.confirm_sec = max(0.0, float(confirm_sec))
        self.match_iou = max(0.0, min(1.0, float(match_iou)))
        self.stale_sec = max(0.1, float(stale_sec))
        self._tracks = {}
        self._next_track_id = 1

    def update(self, person_boxes, helmet_boxes, now=None):
        """각 사람이 현재 미착용 확정 상태인지 bool 목록으로 돌려준다."""
        now = time.monotonic() if now is None else float(now)
        self._tracks = {
            track_id: track for track_id, track in self._tracks.items()
            if now - track["last_seen"] <= self.stale_sec
        }

        available_tracks = set(self._tracks)
        violations = []
        for person_box in person_boxes:
            candidates = [
                (box_iou(person_box, self._tracks[track_id]["box"]), track_id)
                for track_id in available_tracks
            ]
            best_iou, track_id = max(candidates, default=(0.0, None))
            if track_id is None or best_iou < self.match_iou:
                track_id = self._next_track_id
                self._next_track_id += 1
                self._tracks[track_id] = {
                    "box": np.asarray(person_box, dtype=np.float32).copy(),
                    "last_seen": now,
                    "without_helmet_since": None,
                }
            else:
                available_tracks.remove(track_id)

            track = self._tracks[track_id]
            track["box"] = np.asarray(person_box, dtype=np.float32).copy()
            track["last_seen"] = now
            has_helmet = any(
                helmet_in_head_region(person_box, helmet_box)
                for helmet_box in helmet_boxes)
            if has_helmet:
                track["without_helmet_since"] = None
                violations.append(False)
                continue

            if track["without_helmet_since"] is None:
                track["without_helmet_since"] = now
            violations.append(
                now - track["without_helmet_since"] >= self.confirm_sec)
        return violations


def apply_helmet_compliance(boxes, labels, prompt_names, tracker, now=None):
    """helmet은 내부 판정에만 쓰고, 전송할 박스와 최종 라벨을 만든다."""
    names = [_label_name(prompt_names, label) for label in labels]
    person_indices = [
        index for index, name in enumerate(names) if name == PERSON_LABEL]
    helmet_boxes = [
        boxes[index] for index, name in enumerate(names)
        if name == HELMET_LABEL]
    violation_flags = tracker.update(
        [boxes[index] for index in person_indices], helmet_boxes, now=now)
    person_violation = dict(zip(person_indices, violation_flags))

    keep_indices = []
    output_names = []
    for index, name in enumerate(names):
        if name == HELMET_LABEL or not name:
            continue
        keep_indices.append(index)
        if name == PERSON_LABEL and person_violation.get(index, False):
            output_names.append(HELMET_VIOLATION_LABEL)
        else:
            output_names.append(name)
    return boxes[keep_indices], output_names


def camera_capture_loop(cap, rectifier=None):
    """카메라를 계속 읽어 최신 프레임만 갱신합니다. 추론을 기다리지 않습니다.

    rectifier 가 있으면 여기서 좌우를 정렬(rectify)합니다. **하류 전체가 같은
    좌표계를 보게 하려면 여기서 해야 합니다.** 추론 직전에 정렬하면 박스 좌표는
    정렬된 영상 기준인데 스트리밍 화면은 원본이라, 화면의 박스가 물체에서
    어긋납니다. 캡처 단계에서 한 번 정렬해 두면 추론·거리·박스·스트림이 모두
    같은 그림 위에서 돕니다.

    추론과 같은 루프에서 read() 하면 두 가지가 겹칩니다.
      1) 스트림 FPS 가 추론 FPS 로 떨어짐 (원래 문제)
      2) 그 사이 드라이버 버퍼에 프레임이 쌓여, 꺼낸 프레임이 이미 낡음(지연)
    별도 스레드로 계속 비워주면 항상 "가장 최근" 프레임을 쓰게 됩니다.
    """
    global latest_video_frame, latest_right_frame, frame_seq

    camera_error_streak = 0
    last_camera_log_time = 0.0
    last_frame_at = 0.0

    while True:
        read_started = time.perf_counter()
        ret, frame = cap.read()
        if not ret:
            # 예전에는 아무 로그 없이 100Hz 로 계속 돌았습니다. 카메라 케이블이
            # 빠져도 화면은 마지막 프레임이 계속 재전송되어 멀쩡해 보이고,
            # 터미널도 조용해서 원인을 찾을 단서가 없었습니다.
            camera_error_streak += 1
            with health_lock:
                health_state["camera_errors"] += 1
            now = time.time()
            if now - last_camera_log_time > 2.0:
                print(f"⚠ [카메라] 프레임을 읽지 못했습니다 (연속 {camera_error_streak}회). "
                      f"USB 연결과 /dev/video0 을 확인하세요.")
                last_camera_log_time = now
            time.sleep(0.01)
            continue

        if camera_error_streak:
            print(f"✅ [카메라] 복구됨 (실패 {camera_error_streak}회 후)")
            camera_error_streak = 0

        _, w, _ = frame.shape
        mid = w // 2
        rectify_started = time.perf_counter()
        if rectifier is not None:
            # remap 은 새 배열을 만들어 돌려주므로 따로 복사하지 않아도 됩니다.
            # **비용이 공짜가 아닙니다** — 1280x720 두 장이라 젯슨 CPU 에서
            # 프레임당 10~30ms 나갈 수 있고, 그만큼 캡처 FPS 상한이 내려갑니다.
            # /health 의 rectify_ms 로 실제 비용을 보세요.
            left, right = rectifier.rectify_pair(frame[:, 0:mid], frame[:, mid:w])
        else:
            # frame 은 드라이버 버퍼의 뷰라서, 다음 read() 가 덮어쓰기 전에 복사해야
            # 합니다. 복사하지 않으면 스트리밍 중인 프레임이 도중에 바뀝니다.
            left = frame[:, 0:mid].copy()
            right = frame[:, mid:w].copy()
        rectify_ms = (time.perf_counter() - rectify_started) * 1000.0
        read_ms = (rectify_started - read_started) * 1000.0

        with frame_lock:
            latest_video_frame = left
            latest_right_frame = right
            frame_seq += 1

        now_perf = time.perf_counter()
        interval_ms = (now_perf - last_frame_at) * 1000.0 if last_frame_at else 0.0
        last_frame_at = now_perf

        with health_lock:
            health_state["video_frames"] += 1
            health_state["last_video_at"] = time.time()
        with perf_lock:
            perf_state["rectify_ms"] = _blend(perf_state["rectify_ms"], rectify_ms)
            # read_ms 가 크면 카메라가 느린 것이고, 작은데 capture_fps 가 낮으면
            # 우리 쪽 처리(rectify 등)가 발목을 잡는 것입니다. 둘을 갈라 봅니다.
            perf_state["read_ms"] = _blend(perf_state["read_ms"], read_ms)
            if interval_ms > 0:
                perf_state["capture_interval_ms"] = _blend(
                    perf_state["capture_interval_ms"], interval_ms)


def draw_overlays(frame, overlays):
    """최신 영상 위에 최신 박스를 얹습니다. 원본 배열은 건드리지 않습니다.

    박스는 추론 시점의 좌표라 지금 프레임보다 조금 낡았습니다. 사람/차량이
    빠르게 움직이면 박스가 뒤따라오는 것처럼 보이는데, 이건 의도한 절충입니다.
    """
    if not overlays:
        return frame
    out = frame.copy()
    for xmin, ymin, xmax, ymax, info_text in overlays:
        # ✨ [시각화 품질] 선 두께 및 글꼴 크기 최적화 (2px, font-scale 0.6)
        cv2.rectangle(out, (xmin, ymin), (xmax, ymax), (0, 255, 0), 2)
        cv2.putText(out, info_text, (xmin, max(ymin - 10, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (36, 255, 12), 2)
    return out


def get_placeholder_jpeg() -> bytes:
    key = 'rgb'
    with _placeholder_lock:
        cached = _placeholder_cache.get(key)
        if cached is None:
            placeholder = np.zeros((360, 640, 3), dtype=np.uint8)
            text = "Initializing Camera & AI..."
            cv2.putText(placeholder, text, (50, 180),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            ok, encoded = cv2.imencode(
                '.jpg', placeholder, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY_SETTING])
            cached = encoded.tobytes() if ok else b''
            _placeholder_cache[key] = cached
        return cached


# MJPEG 이중 스트리밍 핸들러
class DualStreamingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/health'):
            self.send_health()
            return

        # depth 컬러맵 스트림은 제거했습니다. 관제 화면에 필요한 것은 카메라
        # 영상과 SLAM 미니맵이고, 시차맵은 사람이 해석하기 어려운 데다 대역폭만
        # 먹었습니다. 거리 계산(시차 -> distance_meter)은 그대로 돕니다.
        # 옛 주소로 들어오면 조용히 RGB 를 보여주는 대신 분명히 알려줍니다.
        if self.path.startswith('/depth'):
            self.send_response(404)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(
                b'depth stream removed - use / for the camera view')
            return

        self.send_response(200)
        self.send_header('Content-type', 'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        try:
            # 인코딩은 get_stream_jpeg() 안에서 프레임당 한 번만 일어납니다.
            # 클라이언트가 여럿이어도 같은 결과를 나눠 쓰므로, 보는 사람이 늘어도
            # 젯슨 부하가 그만큼 늘지 않습니다.
            #
            # 예전에는 이 루프가 클라이언트마다 draw_overlays(2.7MB 복사) +
            # imencode 를 따로 했습니다. 대시보드 iframe 과 브라우저 탭을 같이 열면
            # 그 비용이 두 배가 되고, 결국 모두에게 화면이 느려졌습니다.
            last_sent = 0.0
            last_key = None
            with perf_lock:
                perf_state["stream_clients"] += 1

            while True:
                loop_started = time.perf_counter()
                key, jpeg_bytes = get_stream_jpeg()

                # 같은 그림을 두 번 보내지 않습니다. 카메라가 스트림 목표보다
                # 느리면(흔합니다) 그냥 두 배의 대역폭을 쓰게 됩니다.
                # 다만 너무 오래 침묵하면 연결이 끊길 수 있어 주기적으로 한 번은
                # 다시 보냅니다.
                stale = (loop_started - last_sent) >= STREAM_KEEPALIVE_SEC
                if jpeg_bytes and (key != last_key or stale):
                    self.wfile.write(b"--frame\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpeg_bytes)))
                    self.end_headers()
                    self.wfile.write(jpeg_bytes)
                    self.wfile.write(b"\r\n")

                    if last_sent and key != last_key:
                        # 새 프레임을 보낸 간격만 잽니다. keepalive 재전송까지
                        # 세면 실제 영상 속도가 부풀려 보입니다.
                        with perf_lock:
                            perf_state["stream_interval_ms"] = _blend(
                                perf_state["stream_interval_ms"],
                                (loop_started - last_sent) * 1000.0)
                    last_sent = loop_started
                    last_key = key

                # **경과 시간을 빼고 남은 만큼만 잡니다.** 예전에는 인코딩이 끝난 뒤
                # 무조건 33ms 를 더 쉬어서, 인코딩이 20ms 면 실제 주기가 53ms
                # (약 19fps) 였습니다. "30fps 로 맞춰 놨는데 왜 느리지" 의 답입니다.
                elapsed = time.perf_counter() - loop_started
                time.sleep(max(0.0, STREAM_FRAME_INTERVAL - elapsed))
        except Exception:
            pass
        finally:
            with perf_lock:
                perf_state["stream_clients"] = max(
                    0, perf_state["stream_clients"] - 1)

    def send_health(self):
        """카메라와 추론이 각각 돌고 있는지 알려줍니다.

        영상만으로는 판단할 수 없습니다 — 카메라가 빠지거나 루프가 멈춰도
        마지막 프레임이 계속 재전송되므로 화면은 멀쩡해 보입니다.

        캡처와 추론을 분리한 뒤로는 둘이 따로 멈출 수 있어서 나눠서 보고합니다.
          frame_age_sec  — 추론이 멈추면 커집니다 (영상은 계속 흐르는데 박스만 굳음)
          video_age_sec  — 카메라가 멈추면 커집니다 (화면 자체가 정지)
        ok 는 기존 의미 그대로 추론 기준입니다.
        """
        with health_lock:
            state = dict(health_state)
        with perf_lock:
            perf = dict(perf_state)

        def as_fps(interval_ms):
            """구간 간격(ms) -> FPS. 아직 표본이 없으면 None."""
            return round(1000.0 / interval_ms, 1) if interval_ms > 0 else None

        now = time.time()
        body = json.dumps({
            "ok": state["last_frame_at"] > 0 and (now - state["last_frame_at"]) < 5.0,
            "uptime_sec": round(now - state["started_at"], 1),
            "frames": state["frames"],
            "frame_age_sec": (round(now - state["last_frame_at"], 2)
                              if state["last_frame_at"] else None),
            "video_frames": state["video_frames"],
            "video_age_sec": (round(now - state["last_video_at"], 2)
                              if state["last_video_at"] else None),
            "camera_errors": state["camera_errors"],
            "last_detection_count": state["last_detection_count"],
            "udp_errors": state["udp_errors"],
            "udp_targets": dict(network_targets),

            # 영상이 느릴 때 어디가 병목인지 가르는 값들.
            #   capture_fps  카메라가 실제로 주는 속도. 여기가 낮으면 나머지는 무의미
            #   read_ms      cap.read() 가 막힌 시간. 크면 카메라가 느린 것,
            #                작은데 capture_fps 가 낮으면 우리 처리가 느린 것
            #   stream_fps   웹으로 실제로 나가는 속도
            #   rectify_ms   좌우 정렬(캘리브레이션) 비용. 없으면 0
            #   encode_ms    JPEG 인코딩 비용. JPEG_QUALITY 를 낮추면 줄어듭니다
            #   stereo_ms    시차 계산 비용(추론 루프에서 가장 비쌈).
            #                stereo_skipped 가 크면 평상시에는 안 도는 것입니다
            #   encode_reuse 캐시 재사용. 클라이언트가 여럿일 때 커야 정상입니다
            "capture_fps": as_fps(perf["capture_interval_ms"]),
            "stream_fps": as_fps(perf["stream_interval_ms"]),
            "read_ms": round(perf["read_ms"], 2),
            "stereo_ms": round(perf["stereo_ms"], 2),
            "stereo_ran": perf["stereo_ran"],
            "stereo_skipped": perf["stereo_skipped"],
            "rectify_ms": round(perf["rectify_ms"], 2),
            "encode_ms": round(perf["encode_ms"], 2),
            "jpeg_kb": round(perf["jpeg_bytes"] / 1024.0, 1),
            "encode_reuse": perf["encode_reuse"],
            "encode_miss": perf["encode_miss"],
            "stream_clients": perf["stream_clients"],
            "jpeg_quality": JPEG_QUALITY_SETTING,
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 기본 구현은 매 요청을 stderr 에 찍습니다. MJPEG 는 연결이 오래
        # 유지되므로 시끄럽지 않지만, /health 를 주기적으로 폴링하면 로그가
        # 탐지 로그를 덮어버립니다.
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global latest_overlays, overlay_seq, network_targets

    # 1. 네트워크 설정
    # 포트는 받는 쪽 스크립트와 반드시 같아야 합니다. 한쪽만 바꾸면 에러 없이
    # 조용히 데이터가 끊기므로, 양쪽이 같은 환경변수를 읽도록 통일했습니다.
    #   ROS2_UDP_PORT      <-> vision_inference_node.py
    #   DASHBOARD_UDP_PORT <-> pipeline/jetson_patrol_pipeline.py (--udp-port)
    #   DETECTION_UDP_PORT <-> dashboard_link/minimap_renderer.py
    #
    # 목적지 IP는 세 개가 서로 다른 기기입니다. 예전에는 9999와 9998이 HOST_IP
    # 하나를 함께 썼는데, 이 둘은 애초에 다른 기기에서 돕니다.
    #
    #   9999 -> vision_inference_node.py : 젯슨 "호스트"(이 컨테이너 바깥)
    #   9998 -> patrol_pipeline_rag.py   : 이상민님 PC
    #   9091 -> minimap_renderer.py      : 이다은님 노트북
    #
    # 즉 HOST_IP 를 젯슨 호스트로 맞추면 대시보드 파이프라인이 아무것도 못 받고,
    # 이상민님 PC로 맞추면 ROS2 브릿지가 죽습니다. 둘 다 만족시킬 값이 없습니다.
    # 그래서 목적지마다 변수를 따로 두되, 기존 설정이 깨지지 않도록 HOST_IP 를
    # 공통 기본값으로 남겨둡니다.
    host_ip = os.getenv("HOST_IP", "127.0.0.1")
    ros2_ip = os.getenv("ROS2_BRIDGE_IP", host_ip)
    dashboard_ip = os.getenv("DASHBOARD_IP", host_ip)
    ros2_port = int(os.getenv("ROS2_UDP_PORT", "9999"))
    dashboard_port = int(os.getenv("DASHBOARD_UDP_PORT", "9998"))

    # 다은님 노트북(미니맵 렌더러) - 탐지 각도(angle_offset)만 별도로 보냄
    daeun_ip = os.getenv("DAEUN_LAPTOP_IP", "203.0.113.20")
    minimap_angle_port = int(os.getenv("DETECTION_UDP_PORT", "9091"))

    stream_port = int(os.getenv("JETSON_VIDEO_PORT", "8500"))

    network_targets = {
        "ros2_bridge": f"{ros2_ip}:{ros2_port}",
        "dashboard_rag": f"{dashboard_ip}:{dashboard_port}",
        "minimap": f"{daeun_ip}:{minimap_angle_port}",
    }

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("📡 [Docker AI Sender] UDP 전송 대상")
    print(f"     ROS2 브릿지   -> {ros2_ip}:{ros2_port}      (vision_inference_node.py)")
    print(f"     대시보드/DB   -> {dashboard_ip}:{dashboard_port}      (patrol_pipeline_rag.py)")
    print(f"     미니맵 각도   -> {daeun_ip}:{minimap_angle_port}      (minimap_renderer.py)")

    # 두 목적지가 같은 IP인데 그게 루프백이면, 둘 중 하나는 반드시 못 받습니다.
    # UDP는 목적지가 없어도 에러를 주지 않으므로 여기서 미리 경고합니다.
    if ros2_ip == dashboard_ip:
        print()
        print("  [주의] ROS2 브릿지와 대시보드 파이프라인의 대상 IP가 같습니다.")
        print("         이 둘은 보통 다른 기기에서 돕니다. 다르다면 아래처럼 나눠 주세요:")
        print("           export ROS2_BRIDGE_IP=127.0.0.1        # 젯슨 호스트")
        print("           export DASHBOARD_IP=192.168.0.xxx      # 이상민님 PC")
        print()

    # 이 스크립트는 보통 Jetson **위의 Docker 컨테이너 안**에서 돕니다.
    # --network host 이면 컨테이너와 호스트가 같은 네트워크 네임스페이스를 쓰므로
    # 127.0.0.1 이 올바릅니다. bridge 네트워크일 때만 127.0.0.1 이 컨테이너
    # 자신을 뜻합니다. 실행 중인 프로세스에서는 Docker의 네트워크 모드를 확실하게
    # 판별할 수 없으므로, 단정적인 오류 대신 실행 명령에서 확인할 항목을 안내합니다.
    loopback = ("127.0.0.1", "localhost", "::1")
    if _in_container() and (ros2_ip in loopback or dashboard_ip in loopback):
        print()
        print("  [네트워크 확인] 컨테이너에서 ROS2 목적지로 루프백을 사용합니다.")
        print("         docker run 출력에 --network host 가 있으면 정상이며 그대로 사용합니다.")
        print("         --network host 가 없다면 ROS2_BRIDGE_IP를 호스트 IP로 바꾸고")
        print("         영상 포트에 -p 8500:8500 을 추가해야 합니다.")
        print()

    # 2. 웹 스트리밍 서버 먼저 시작
    try:
        server = ThreadedHTTPServer(('0.0.0.0', stream_port), DualStreamingHandler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        print(f"★ [성공] 이중 웹 스트리밍 가동 중 (화질 설정: {JPEG_QUALITY_SETTING}%) ★")
        print(f"   -> 영상: http://localhost:{stream_port}  |  상태: /health")
    except OSError as e:
        print(f"❌ [에러] 웹 스트리밍 서버 바인딩 실패: {e}")
        if getattr(e, "errno", None) == 98:
            print(f"   -> {stream_port}번 포트를 기존 프로세스가 사용 중입니다.")
            print(f"   -> Jetson 호스트에서: sudo ss -ltnp 'sport = :{stream_port}'")
            print("   -> 기존 ai_inference_sender 또는 NanoOWL 컨테이너를 종료한 후 다시 실행하세요.")
        return
    except Exception as e:
        print(f"❌ [에러] 웹 스트리밍 서버 바인딩 실패: {e}")
        return

    # 3. 카메라 로드 및 고화질 해상도 설정
    # Jetson의 USB 스테레오 카메라는 OpenCV가 GStreamer를 자동 선택하면
    # "Internal data stream error"로 열렸다가 0x0 해상도가 되는 경우가 있다.
    # V4L2로 직접 열면 해당 카메라가 2560x720 프레임을 안정적으로 반환한다.
    # 다른 입력 장치가 필요하면 CAMERA_INDEX/CAMERA_BACKEND로만 바꾼다.
    camera_index = int(os.getenv("CAMERA_INDEX", "0"))
    camera_backend = os.getenv("CAMERA_BACKEND", "V4L2").strip().upper()
    if camera_backend == "V4L2" and hasattr(cv2, "CAP_V4L2"):
        cap = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)
        if not cap.isOpened():
            # V4L2 가 없는 환경(다른 플랫폼)이나 장치가 V4L2 로 안 잡히는 경우.
            print("⚠ [카메라] V4L2 로 열지 못해 기본 백엔드로 재시도합니다.")
            cap = cv2.VideoCapture(camera_index)
            camera_backend = "기본(폴백)"
    else:
        cap = cv2.VideoCapture(camera_index)
    print(f"📷 [카메라] /dev/video{camera_index} · 백엔드: {camera_backend}")

    # 해상도를 설정하기 전에 카메라가 실제로 열렸는지 먼저 확인합니다.
    if not cap.isOpened():
        print(f"❌ [에러] /dev/video{camera_index} 카메라인식 실패!")
        print("   장치 번호를 확인하고 다르면 CAMERA_INDEX 로 지정하세요.")
        print("     ls -l /dev/video*")
        print("     sudo apt install -y v4l-utils && v4l2-ctl --list-devices")
        return

    # ✨ [화질 조절 포인트 2] 고해상도 출력을 위한 MJPG 코덱 지정
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))

    # ✨ [화질 조절 포인트 3] 스테레오 목표 해상도 요청 (좌+우 통합 2560x720)
    target_w = 2560
    target_h = 720
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, target_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, target_h)

    # FPS 를 요청하지 않으면 카메라가 이 해상도에서 제멋대로 협상합니다.
    # 많은 USB 스테레오 모듈이 2560x720 에서 15fps 이하로 붙는데, 그러면 아래
    # 파이프라인을 아무리 최적화해도 그 위로 못 올라갑니다.
    if CAMERA_TARGET_FPS > 0:
        cap.set(cv2.CAP_PROP_FPS, CAMERA_TARGET_FPS)

    # 드라이버 버퍼를 1로 줄입니다. 캡처 스레드가 계속 비우긴 하지만, 버퍼가 깊으면
    # 꺼낸 프레임이 이미 몇 프레임 낡은 상태라 "느리다" 가 아니라 "밀린다" 로
    # 보입니다. 지원하지 않는 드라이버에서는 조용히 무시됩니다.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    actual_fps = cap.get(cv2.CAP_PROP_FPS)

    # 0x0 은 "열리기는 했는데 스트림이 죽은" 상태입니다. 예전에는 이 상태에서도
    # "카메라 성공" 을 찍고 첫 프레임에서야 죽어서, 진짜 원인(백엔드/장치 번호)을
    # 한참 뒤에야 알았습니다. 여기서 바로 가릅니다.
    if actual_w <= 0 or actual_h <= 0:
        print(f"❌ [에러] 카메라는 열렸지만 스트림이 없습니다 "
              f"(보고된 크기 {int(actual_w)}x{int(actual_h)}).")
        print("   백엔드가 안 맞을 때 나오는 전형적인 증상입니다. 확인 순서:")
        print("     1) CAMERA_BACKEND=V4L2 인지 (지금: %s)" % camera_backend)
        print("     2) ls -l /dev/video*        장치가 보이는지")
        print(f"     3) 다른 장치면 CAMERA_INDEX=1 처럼 지정")
        cap.release()
        return

    print("✅ [카메라 성공] 스테레오 카메라 정상 로드")
    print(f"📐 [해상도 점검] 요청: {target_w}x{target_h} | 실제 입력 캡처 크기: {int(actual_w)}x{int(actual_h)}")
    print(f"🎞  [FPS 점검]    요청: {CAMERA_TARGET_FPS:.0f} | 카메라 보고값: {actual_fps:.1f}")
    if actual_fps and actual_fps < CAMERA_TARGET_FPS - 1:
        print("     ⚠ 카메라가 요청 FPS 를 거부했습니다. 이 값이 영상 속도의 상한입니다.")
        print("       해상도를 낮추면(예: 1280x480) 대개 FPS 가 올라갑니다.")
    print(f"🖼  [스트리밍]    목표 {STREAM_TARGET_FPS:.0f}fps · JPEG 품질 {JPEG_QUALITY_SETTING}")
    print(f"     실제 속도는 http://<젯슨IP>:{stream_port}/health 에서 확인하세요.")

    # 3-1. 스테레오 캘리브레이션 적용
    #
    # cap.get() 이 알려주는 크기는 드라이버가 거짓말을 하기도 하므로, 프레임을
    # 한 장 실제로 읽어서 한쪽 눈 크기를 확정합니다. 정렬 맵은 이 크기에
    # 정확히 맞아야 합니다. 캡처 스레드는 아직 시작 전이라 여기서 읽어도
    # 경합이 없습니다.
    ok, probe = cap.read()
    if not ok:
        print("❌ [에러] 첫 프레임을 읽지 못했습니다. 카메라 연결을 확인하세요.")
        cap.release()
        return

    probe_h, probe_w = probe.shape[:2]
    if check_stereo_frame is not None:
        stereo_problem = check_stereo_frame(probe_w, probe_h)
    else:
        ratio = probe_w / float(probe_h) if probe_h > 0 else 0.0
        stereo_problem = ("좌우 결합 스테레오 프레임이 아닙니다 "
                          f"({probe_w}x{probe_h}, 가로/세로 {ratio:.2f})."
                          if ratio < 1.9 else "")
    if stereo_problem:
        print(f"❌ [에러] {stereo_problem}")
        print("   카메라가 모노 해상도로 협상됐을 수 있습니다. ")
        print("   v4l2-ctl --list-formats-ext -d /dev/video0 으로 지원 모드를 확인하세요.")
        cap.release()
        return

    eye_h, eye_w = probe_h, probe_w // 2

    rectifier = load_rectifier(eye_w, eye_h) if load_rectifier else None

    # 4. NanoOWL VLM 엔진 로드
    engine_path = '/opt/nanoowl/data/owl_image_encoder_patch32.engine'
    print(f"🧠 [VLM Engine] 로딩 중: {engine_path}")
    predictor = OwlPredictor(image_encoder_engine=engine_path)

    # NanoOWL에는 부정문인 "person with no helmet"을 묻지 않는다.
    # person과 긍정 객체 safety helmet을 각각 찾은 뒤, 사람 머리 영역에
    # 안전모가 4초 연속 보이지 않을 때 출력만 person with no helmet으로
    # 바꾼다. 안전모 박스 자체는 UDP/화면에 출력하지 않는다.
    text_prompt = ["person", "safety helmet", "fire", "vehicle"]
    text_encodings = predictor.encode_text(text_prompt)
    helmet_tracker = HelmetComplianceTracker()
    print(f"⛑ [PPE] 안전모가 {helmet_tracker.confirm_sec:.1f}초 연속 보이지 "
          f"않으면 '{HELMET_VIOLATION_LABEL}'으로 전송합니다")

    # 5. 거리(시차) 계산 파라미터 (StereoSGBM)
    #
    # 시차맵을 화면으로 내보내지는 않습니다(컬러맵 스트림 제거). 그래도 이 값이
    # 곧 UDP 로 나가는 distance_meter 이므로 품질이 그대로 위험도 판정에 반영됩니다.
    #
    # **StereoSGBM 은 좌우 영상이 정렬돼 있다고 가정합니다.** 왼쪽 N번째 줄의 점을
    # 오른쪽 N번째 줄에서만 찾습니다. 두 카메라가 1~2도만 틀어져 있어도 진짜
    # 대응점은 다른 줄에 있어서 아예 못 찾습니다. 위에서 rectifier 를 적용한
    # 이유가 이것입니다 — 캘리브레이션이 없으면 아래 파라미터를 아무리 잘 잡아도
    # 시차가 -1 로 많이 나옵니다.
    #
    # 명시하지 않은 인자는 전부 기본값 0이고, OpenCV에서 0은 "해당 기능 끔"입니다.
    # 특히 P1/P2(평활도 페널티)가 0이면 SGBM이 사실상 노이즈투성이 블록 매칭처럼
    # 동작해서, 텍스처가 없는 벽/바닥에서 시차가 프레임마다 무작위로 튑니다.
    # 그러면 박스 안 median 거리값도 노이즈 낀 값에서 계산됩니다.
    #
    # P1/P2 값은 OpenCV 권장 공식(8/32 * 채널수 * blockSize^2)을 따릅니다.
    # 입력이 그레이스케일이므로 채널수는 1입니다.
    BLOCK_SIZE = 5
    stereo = cv2.StereoSGBM_create(
        minDisparity=0,
        numDisparities=16 * 5,
        blockSize=BLOCK_SIZE,
        P1=8 * BLOCK_SIZE * BLOCK_SIZE,    # 시차 +-1 변화 페널티 (200)
        P2=32 * BLOCK_SIZE * BLOCK_SIZE,   # 시차 큰 변화 페널티 (800), P2 > P1 필수
        uniquenessRatio=10,                # 애매한 매칭 기각 (%)
        speckleWindowSize=100,             # 이보다 작은 반점 덩어리는 무효 처리
        speckleRange=2,                    # 한 덩어리 안에서 허용할 시차 편차
        disp12MaxDiff=1,                   # 좌우 일관성 검사
    )
    # 초점거리 f 와 렌즈 간격 B.
    #
    # 이 두 값이 거리 계산(Z = f*B/d)과 미니맵 방향각(atan2(dx, f))에 **동시에**
    # 쓰입니다. 그래서 틀리면 거리와 방향이 같이 틀립니다. 예전에는 500.0 / 0.06
    # 이 그냥 박혀 있었는데 둘 다 실측이 아니라 추측값이었습니다.
    #
    # 우선순위:
    #   1) stereo_calibration.npz (실측)      <- 권장
    #   2) 환경변수 STEREO_FOCAL_PX / STEREO_BASELINE_M
    #      (캘리브레이션 전에 "2m 에 세워 두고 나온 값" 으로 급히 맞출 때)
    #   3) 예전 상수 (추측값)
    if rectifier is not None:
        focal_length = rectifier.focal_px
        baseline = rectifier.baseline_m
        principal_x = rectifier.principal_x
        hfov = math.degrees(2.0 * math.atan2(eye_w / 2.0, focal_length))
        print("🎯 [캘리브레이션] 적용됨 — 좌우 영상을 정렬(rectify)합니다")
        if CALIBRATION_PATH:
            print(f"     파일       : {CALIBRATION_PATH}")
        print(f"     초점거리 f : {focal_length:.1f} px (수평 화각 약 {hfov:.0f}도)")
        print(f"     렌즈 간격 B: {baseline*1000:.1f} mm")
        print(f"     주점 cx    : {principal_x:.1f} px")
        # 다은님 쪽 event_detector 는 이 값을 모르면 화면 중심을 주점으로 가정합니다.
        # 그러면 여기서 캘리브레이션으로 얻은 정확도를 지도 좌표에서 그대로 잃습니다.
        # 그대로 복사해 쓸 수 있게 한 줄로 찍습니다.
        print("     ※ event_detector.py 에 아래를 그대로 주세요")
        print(f"       -p jetson_focal_length:={focal_length:.1f} "
              f"-p jetson_frame_width:={float(eye_w)} "
              f"-p jetson_frame_height:={float(eye_h)} "
              f"-p jetson_principal_x:={principal_x:.1f} "
              f"-p jetson_principal_y:={rectifier.principal_y:.1f}")
    else:
        focal_length = float(os.getenv("STEREO_FOCAL_PX", "500.0"))
        baseline = float(os.getenv("STEREO_BASELINE_M", "0.06"))
        principal_x = eye_w / 2.0
        print()
        print("  [주의] 스테레오 캘리브레이션이 없습니다.")
        print(f"         f={focal_length}px / B={baseline*1000:.0f}mm 는 **추측값**이라,")
        print("         거리와 미니맵 방향각이 그만큼 틀어집니다. 좌우 정렬도 하지")
        print("         않으므로 StereoSGBM 이 대응점을 놓쳐 거리가 -1 로 자주 나옵니다.")
        if CALIBRATION_PATH:
            print(f"         확인한 파일 경로: {CALIBRATION_PATH}")
        else:
            print("         stereo_calibrate.py도 없어 결과 파일 경로를 확인할 수 없습니다.")
        print("         해결: python3 stereo_calibrate.py capture  (자세한 순서는 README)")
        print()

    last_udp_log_time = time.time()
    udp_error_count = 0
    last_udp_error = ""

    # 카메라 읽기는 별도 스레드가 담당합니다. 이 루프는 추론만 합니다.
    capture_thread = threading.Thread(
        target=camera_capture_loop, args=(cap, rectifier), daemon=True)
    capture_thread.start()
    print("★ [성공] 카메라 캡처 스레드 시작 — 영상은 추론 속도와 무관하게 흐릅니다 ★")

    last_seq = -1

    while True:
        # 아직 새 프레임이 없으면 같은 프레임을 두 번 추론하지 않고 잠깐 쉽니다.
        with frame_lock:
            frame_left = latest_video_frame
            frame_right = latest_right_frame
            seq = frame_seq

        if frame_left is None or seq == last_seq:
            time.sleep(0.005)
            continue
        last_seq = seq


        # VLM 추론
        frame_left_rgb = cv2.cvtColor(frame_left, cv2.COLOR_BGR2RGB)
        pil_img_left = Image.fromarray(frame_left_rgb)
        outputs = predictor.predict(image=pil_img_left, text=text_prompt, text_encodings=text_encodings, threshold=0.1)

        boxes = outputs.boxes.detach().cpu().numpy()
        labels = outputs.labels.detach().cpu().numpy()
        # 같은 클래스의 중복 박스를 신뢰도 순으로 줄이기 위해 점수를 꺼냅니다.
        raw_scores = getattr(outputs, 'scores', None)
        scores = raw_scores.detach().cpu().numpy() if raw_scores is not None else None

        # 같은 클래스의 중복만 정리한다. person과 safety helmet은 중첩되어야
        # 정상이므로 서로 지우지 않는다.
        boxes, labels = suppress_overlapping_detections(
            boxes, labels, scores, text_prompt)
        boxes, output_labels = apply_helmet_compliance(
            boxes, labels, text_prompt, helmet_tracker)

        # 시차 -> 거리 계산.
        #
        # **탐지가 없으면 아예 하지 않습니다.** StereoSGBM 은 이 루프에서 가장 비싼
        # 연산이고(1280x720 에서 젯슨 CPU 수백 ms), 결과는 오직 "박스 안의 거리"
        # 하나를 구하는 데만 씁니다. 박스가 없으면 계산할 이유가 없는데 예전에는
        # 매 프레임 돌렸습니다. 대부분의 프레임에는 탐지가 없으므로, 이 조건 하나가
        # 평상시 CPU 사용을 크게 줄입니다.
        #
        # CPU 가 놀면 캡처 스레드와 JPEG 인코딩도 같이 빨라집니다 — 젯슨은 코어가
        # 적어서 한 스레드가 100% 를 먹으면 영상까지 밀립니다.
        disparity = None
        if len(boxes) > 0:
            stereo_started = time.perf_counter()
            gray_l = cv2.cvtColor(frame_left, cv2.COLOR_BGR2GRAY)
            gray_r = cv2.cvtColor(frame_right, cv2.COLOR_BGR2GRAY)
            # 원시 시차(int16, 1/16 픽셀 단위)를 그대로 둡니다. 실수 변환은
            # 박스 안에서만 합니다 — 전체 프레임을 float32 로 바꾸면 3.7MB 를
            # 새로 할당하고 전체를 세 번 더 훑게 됩니다.
            disparity = stereo.compute(gray_l, gray_r)
            with perf_lock:
                perf_state["stereo_ms"] = _blend(
                    perf_state["stereo_ms"],
                    (time.perf_counter() - stereo_started) * 1000.0)
                perf_state["stereo_ran"] += 1
        else:
            with perf_lock:
                perf_state["stereo_skipped"] += 1

        # 캡처 스레드가 이미 복사본을 넘겨주므로 여기서 또 복사하지 않습니다.

        detections_summary = []
        angle_payloads = []
        new_overlays = []

        for box, label_text in zip(boxes, output_labels):
            xmin, ymin, xmax, ymax = map(int, box)

            # 거리는 **박스 안에서만** 계산합니다. 예전에는 전체 프레임을 float 로
            # 바꿔 depth_map 을 만들었는데(3.7MB 할당 + 전체 순회 3회), 정작 쓰는 건
            # 박스 안 median 하나뿐입니다. 결과값은 예전과 같습니다.
            if disparity is not None and xmax > xmin and ymax > ymin:
                # SGBM 은 1/16 픽셀 고정소수점으로 돌려줍니다.
                roi = disparity[ymin:ymax, xmin:xmax].astype(np.float32) / 16.0
                valid = roi[roi > 0]          # 시차를 못 구한 픽셀 제외
                if valid.size:
                    depths = (focal_length * baseline) / valid
                    # 예전과 같은 하한(0.1m). 시차가 비정상적으로 커서 나오는
                    # 가짜 근접값을 걸러냅니다.
                    depths = depths[depths > 0.1]
                    distance = float(np.median(depths)) if depths.size else -1.0
                else:
                    distance = -1.0
            else:
                distance = -1.0

            detections_summary.append({
                "object": label_text,
                "bbox": [xmin, ymin, xmax, ymax],
                "distance_meter": round(distance, 2)
            })
            box_center_x = (xmin + xmax) / 2.0
            angle_offset = math.atan2(box_center_x - principal_x, focal_length)
            angle_payloads.append(json.dumps({
                "label": label_text,
                "angle_offset": angle_offset
            }).encode('utf-8'))

            info_text = f"{label_text} [{round(distance, 2)}m]"
            new_overlays.append((xmin, ymin, xmax, ymax, info_text))

        with overlay_lock:
            latest_overlays = new_overlays
            overlay_seq += 1

        with health_lock:
            health_state["frames"] += 1
            health_state["last_frame_at"] = time.time()
            health_state["last_detection_count"] = len(detections_summary)

        # UDP 패킷 전송
        payload = json.dumps({"timestamp": time.time(), "detections": detections_summary}).encode('utf-8')

        send_jobs = [
            (ros2_ip, ros2_port, payload),
            (dashboard_ip, dashboard_port, payload),
        ]
        send_jobs.extend((daeun_ip, minimap_angle_port, p) for p in angle_payloads)

        for dest_ip, dest_port, data in send_jobs:
            try:
                sock.sendto(data, (dest_ip, dest_port))
            except OSError as exc:
                udp_error_count += 1
                last_udp_error = f"{dest_ip}:{dest_port} → {exc}"
                with health_lock:
                    health_state["udp_errors"] += 1

        if time.time() - last_udp_log_time > 2.0:
            status = (f"📤 [UDP Live Transmission] Target: {dashboard_ip}:{dashboard_port} | "
                      f"감지된 객체: {len(detections_summary)}개")
            if udp_error_count:
                status += f" | ⚠ 전송 실패 {udp_error_count}건 (최근: {last_udp_error})"
            print(status)
            udp_error_count = 0
            last_udp_log_time = time.time()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n종료합니다.")
