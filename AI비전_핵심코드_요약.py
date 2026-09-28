"""
AI 비전 핵심 코드 요약
======================

원본: smart_factory_project/ai_inference_sender.py (총 1113줄, Jetson Docker 컨테이너에서 실행)
      (젯슨 연결용 프로그램(손준영)/ai_inference_sender.py 와 바이트 단위로 동일)
정리 기준: 2026-09-06, with-no-helmet 브랜치 커밋 2f7b26e

목적: 개발보고서 첨부용으로, 비전 AI 파이프라인을 구현하면서 실제로 쌓인
핵심 구현사항과 개선점을 전체적으로 정리한다. 안전모 판정 하나만이 아니라
① 탐지·중복 제거, ② 안전모 판정, ③ 스테레오 캘리브레이션 적용,
④ 카메라 캡처·추론 분리, ⑤ 거리·각도 계산, ⑥ UDP 3-way 전송,
⑦ 입력 안정성 방어 코드, ⑧ 스트리밍 부하 절감까지 8개 영역을 다룬다.
각 영역은 "무엇이 문제였고 어떻게 바뀌었는지"를 코드 옆 주석으로 남겼다.

이 파일은 그대로 실행하는 용도가 아니라 코드 리뷰·보고서 첨부용이다.
순수 로직 함수(box_iou, helmet_in_head_region, HelmetComplianceTracker,
apply_helmet_compliance, suppress_overlapping_detections)는 원본과 동일하게
옮겼고, 나머지는 main()·camera_capture_loop()에 성능 계측·로그와 섞여 있던
로직을 같은 동작 그대로 별도 함수로 재구성했다. 실제 라인 위치는 하단
CODE_MAP 참고.
"""

import json
import math
import time

import numpy as np

# ============================================================
# 0. 탐지 클래스와 하류(downstream) 계약
# ============================================================
# 핵심 아이디어: NanoOWL에는 "안전모 미착용"이라는 부정문을 직접 묻지 않는다.
# person과 safety helmet, 두 긍정 객체만 검출기에 묻고, "안전모 미착용"은
# 3장의 판정 로직이 시간에 걸쳐 만들어내는 파생 라벨이다. (판정 방식이 어떻게
# 바뀌어 왔는지는 이 파일 맨 아래 EVOLUTION_LOG 참고.)

PERSON_LABEL = "person"
HELMET_LABEL = "safety helmet"
HELMET_VIOLATION_LABEL = "person with no helmet"

# NanoOWL 검출기에 실제로 묻는 프롬프트 (모델 입력)
TEXT_PROMPT = ["person", "safety helmet", "fire", "vehicle"]

# ROS2 브릿지 / DB·RAG 파이프라인 / 미니맵이 실제로 받는 라벨 계약 (모델 출력과 다름).
# safety helmet은 내부 판정 전용이라 이 목록에 없고, 하류로 전송되지 않는다.
# 이 목록의 문자열은 common/schema.py, minimap_web.html, event_detector.py와
# 글자 단위로 같아야 하므로 임의로 바꾸지 않는다.
OUTPUT_LABELS = ["person with no helmet", "person", "fire", "vehicle"]

# 안전모가 이 시간(초) 동안 머리 영역에서 계속 안 보여야 위반으로 확정한다.
# 4초는 "3~5초면 된다"는 현장 요구의 중간값.
PPE_NO_HELMET_CONFIRM_SEC = 4.0
# 사람 박스 중 상단 몇 %를 "머리 영역"으로 볼지.
PPE_HEAD_REGION_RATIO = 0.40
# 프레임 간 같은 사람으로 이어 붙일 IoU 기준.
PPE_TRACK_IOU_THRESHOLD = 0.20
# 이 시간(초) 동안 매칭되는 사람이 없으면 추적을 포기(트랙 삭제)한다.
PPE_TRACK_STALE_SEC = 1.5

# 같은 물체를 가리키는 박스로 볼 IoU 기준(같은 클래스 안에서만 적용).
DEDUP_IOU_THRESHOLD = 0.5


# ============================================================
# 1. 박스 유틸리티  (원본 L235-254)
# ============================================================
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


# ============================================================
# 2. 같은 클래스 중복 탐지 제거  (원본 L257-288)
# ============================================================
def suppress_overlapping_detections(boxes, labels, scores, prompt_names,
                                    iou_threshold=DEDUP_IOU_THRESHOLD):
    """같은 클래스의 겹치는 박스만 신뢰도 순으로 하나로 줄인다.

    역할: 한 물체에 박스가 여러 개 잡히는 흔한 중복만 정리한다.
    person 박스 안에 safety helmet 박스가 있는 것은 "정상"이므로,
    서로 다른 클래스는 겹쳐도 중복으로 보지 않는다.

    예전에는 이 함수가 "person vs person with no helmet" 같은 **클래스 간**
    충돌까지 함께 해결했다(점수 + 안전 마진). 안전모 판정을 시간 기반 상태
    추적(3장)으로 옮기면서, 이 함수는 원래 목적인 "동일 클래스 중복 제거"만
    남기고 단순해졌다.
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
    keep.sort()
    return boxes[keep], labels[keep]


# ============================================================
# 3. 안전모 판정 — 긍정 탐지 + 머리 영역 + 4초 지속 확인  (원본 L291-389)
# ============================================================
def helmet_in_head_region(person_box, helmet_box,
                          head_ratio=PPE_HEAD_REGION_RATIO) -> bool:
    """안전모 박스의 중심점이 사람 박스의 머리 영역(상단 head_ratio) 안에 있는가."""
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
    """IoU로 사람을 프레임 간에 이어 붙이며, 안전모 미확인 지속 시간을 잰다.

    역할: 한 프레임만 가려도 즉시 위반으로 판정되는 흔들림을 막는다.
    위반 판정에는 confirm_sec(기본 4초)의 지연을 두고(신중하게),
    안전모 재확인 시 정상 복귀는 지연 없이 즉시 처리한다(빠르게) —
    이 비대칭이 오탐과 실제 위반 놓침 사이의 절충점이다.
    """

    def __init__(self, confirm_sec=PPE_NO_HELMET_CONFIRM_SEC,
                 match_iou=PPE_TRACK_IOU_THRESHOLD,
                 stale_sec=PPE_TRACK_STALE_SEC):
        self.confirm_sec = max(0.0, float(confirm_sec))
        self.match_iou = max(0.0, min(1.0, float(match_iou)))
        self.stale_sec = max(0.1, float(stale_sec))
        self._tracks = {}
        self._next_track_id = 1

    def update(self, person_boxes, helmet_boxes, now=None):
        """각 사람이 현재 '미착용 확정' 상태인지 bool 목록으로 돌려준다."""
        now = time.monotonic() if now is None else float(now)
        # 오래 못 본 트랙은 버린다 — 화면 밖으로 나갔던 사람이 돌아오면 새 트랙.
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
    """helmet 박스는 판정에만 쓰고, 실제로 전송할 박스·최종 라벨만 만든다.

    safety helmet 박스 자체는 반환값에서 완전히 빠진다 — 화면·UDP 어디로도
    나가지 않는다. person이 위반으로 확정되면 라벨만 person with no helmet
    으로 바뀌고 박스는 그대로다.
    """
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


# ============================================================
# 4. 스테레오 캘리브레이션 적용  (원본 main() L894-939 발췌 재구성)
# ============================================================
# 문제였던 것: 예전에는 focal_length=500.0, baseline=0.06 이 그냥 코드에
# 박혀 있었다. 둘 다 실측이 아니라 추측값이었고, 이 두 값은 거리 계산
# (Z = f*B/d)과 미니맵 방향각(atan2(dx, f))에 **동시에** 쓰이므로 틀리면
# 거리와 방향이 같이 틀어졌다.
#
# 개선: stereo_calibrate.py의 실측 캘리브레이션(rectifier)이 있으면 그 값을
# 쓰고, 없으면 환경변수(현장에서 급히 잰 값), 그마저 없으면 예전 추측값
# 순으로 내려간다. 우선순위를 코드 한 곳에서 결정해 main()이 값의 출처를
# 몰라도 되게 했다.
def resolve_stereo_parameters(rectifier, eye_w,
                              env_focal_px=None, env_baseline_m=None):
    """(focal_length, baseline, principal_x, source) 를 우선순위대로 결정한다.

    source는 로그·보고서에 "이 값이 실측인지 추측인지"를 밝히기 위한 표시다.
    """
    if rectifier is not None:
        return rectifier.focal_px, rectifier.baseline_m, rectifier.principal_x, "calibrated"
    if env_focal_px is not None and env_baseline_m is not None:
        return float(env_focal_px), float(env_baseline_m), eye_w / 2.0, "env_override"
    return 500.0, 0.06, eye_w / 2.0, "guessed"


# ============================================================
# 5. 카메라 캡처와 추론 분리, 좌우 정렬(rectify)  (원본 L392-470 발췌 재구성)
# ============================================================
# 문제였던 것: 카메라 read()와 NanoOWL 추론을 한 루프에서 돌리면 스트림 FPS가
# 추론 FPS(수백 ms/frame)에 그대로 묶인다. 30fps로 들어오는 카메라도 화면은
# 몇 분의 1로 끊겨 보였다.
#
# 개선: 캡처를 별도 스레드로 분리해 "가장 최근 프레임"을 계속 갱신하고,
# 추론 루프는 그중 새 프레임이 왔을 때만 처리한다. 정렬(rectify)도 캡처
# 단계에서 미리 해 둔다 — 추론 직전에 정렬하면 박스 좌표는 정렬된 영상
# 기준인데 스트리밍 화면은 원본이라 박스가 물체에서 어긋나기 때문이다.
def split_and_rectify(stereo_frame, rectifier=None):
    """좌우 통합 프레임을 절반으로 나누고, 캘리브레이션이 있으면 정렬한다.

    camera_capture_loop()가 매 프레임 이 함수를 호출해 latest_video_frame /
    latest_right_frame을 갱신한다(실제 코드는 스레드 락과 프레임 시퀀스
    번호 관리가 더 있음 — 이 요약에서는 생략).
    """
    _, w, _ = stereo_frame.shape
    mid = w // 2
    if rectifier is not None:
        # remap은 새 배열을 반환하므로 별도 복사가 필요 없다.
        return rectifier.rectify_pair(stereo_frame[:, 0:mid], stereo_frame[:, mid:w])
    # 원본은 드라이버 버퍼의 뷰이므로, 다음 read()가 덮어쓰기 전에 복사해야
    # 스트리밍 중인 프레임이 도중에 바뀌지 않는다.
    left = stereo_frame[:, 0:mid].copy()
    right = stereo_frame[:, mid:w].copy()
    return left, right


# ============================================================
# 6. StereoSGBM 거리·각도 계산 — 탐지가 있을 때만 실행
# ============================================================
def compute_disparity_if_needed(stereo, frame_left, frame_right, detection_count):
    """탐지가 하나도 없으면 이번 프레임은 SGBM을 아예 돌리지 않는다.

    역할: SGBM은 이 파이프라인에서 가장 비싼 연산(1280x720에서 젯슨 CPU
    수백 ms)인데, 결과는 오직 "박스 안 거리" 하나에만 쓰인다. 대부분의
    프레임에는 탐지가 없으므로, 이 조건 하나로 평상시 CPU 사용량을 크게
    줄인다. CPU가 놀면 캡처 스레드·JPEG 인코딩도 함께 빨라진다(코어가 적은
    젯슨에서는 한 스레드 점유가 전체 영상 속도에 영향을 준다).
    """
    if detection_count == 0:
        return None
    gray_l = _to_gray(frame_left)
    gray_r = _to_gray(frame_right)
    return stereo.compute(gray_l, gray_r)


def _to_gray(frame):
    """cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) 자리 표시 — OpenCV 의존성 없이
    이 요약 파일만 봐도 흐름을 알 수 있도록 이름만 남겨 둔다."""
    return frame


def estimate_distance_m(disparity, box, focal_length, baseline):
    """탐지 박스 내부의 유효 시차만으로 물리적 거리(m)를 구한다.

    역할: 전체 프레임 depth map을 만들지 않고 박스 안 median만 계산해
    연산량을 줄인다. Z = f * B / disparity 식을 그대로 쓴다.
    disparity는 StereoSGBM.compute()의 원시 출력(1/16 픽셀 고정소수점)이다.
    """
    xmin, ymin, xmax, ymax = map(int, box)
    if disparity is None or xmax <= xmin or ymax <= ymin:
        return -1.0

    roi = disparity[ymin:ymax, xmin:xmax].astype(np.float32) / 16.0
    valid = roi[roi > 0]                 # 시차를 못 구한 픽셀 제외
    if not valid.size:
        return -1.0

    depths = (focal_length * baseline) / valid
    depths = depths[depths > 0.1]        # 비정상 근접값(가짜 시차) 제외
    return float(np.median(depths)) if depths.size else -1.0


def estimate_angle_offset(box, focal_length, principal_x):
    """박스 중심이 카메라 정면 기준 몇 라디안 방향인지 계산한다(미니맵 각도 전송용).

    기준을 화면 중심(w/2)이 아니라 캘리브레이션으로 구한 주점(principal_x)으로
    잡는 이유: 렌즈 광축이 센서 정중앙을 지난다는 보장이 없기 때문이다.
    """
    xmin, ymin, xmax, ymax = map(int, box)
    box_center_x = (xmin + xmax) / 2.0
    return math.atan2(box_center_x - principal_x, focal_length)


# ============================================================
# 7. 탐지 -> 판정 -> 거리·각도 -> 패킷 조합  (main() 루프 966-1063행 요약)
# ============================================================
def build_detections(boxes, labels, prompt_names, disparity,
                     focal_length, baseline, principal_x, tracker,
                     scores=None):
    """한 프레임의 탐지 결과를 하류로 보낼 최종 형태로 만든다.

    실제 코드에서는 이 단계가 오버레이 좌표 수집·성능 계측과 한 루프에
    섞여 있다. 이 함수는 순수하게 "탐지 -> 판정 -> 거리/각도" 로직만
    뽑아 정리한 것이다.
    """
    boxes, labels = suppress_overlapping_detections(
        boxes, labels, scores, prompt_names)
    boxes, output_labels = apply_helmet_compliance(
        boxes, labels, prompt_names, tracker)

    detections = []
    for box, label_text in zip(boxes, output_labels):
        xmin, ymin, xmax, ymax = map(int, box)
        distance = estimate_distance_m(disparity, box, focal_length, baseline)
        angle_offset = estimate_angle_offset(box, focal_length, principal_x)
        detections.append({
            "object": label_text,
            "bbox": [xmin, ymin, xmax, ymax],
            "distance_meter": round(distance, 2),
            "angle_offset": angle_offset,
        })
    return detections


# ============================================================
# 8. UDP 3-way 전송과 목적지 분리  (원본 main() L679-701, L1076-1094 요약)
# ============================================================
# 문제였던 것: 9999(ROS2 브릿지)와 9998(데이터 PC)이 한때 HOST_IP 하나를
# 함께 썼다. 이 둘은 보통 서로 다른 물리 기기라서, HOST_IP를 어느 한쪽에
# 맞추면 나머지 하나는 UDP가 조용히(에러 없이) 유실됐다.
#
# 개선: 목적지별 환경변수(ROS2_BRIDGE_IP / DASHBOARD_IP / DAEUN_LAPTOP_IP)를
# 분리하고, 세 목적지가 같은 IP인데 그게 루프백이면 시작 시점에 경고를
# 출력한다.
def send_detections(sock, detections, ros2_target, dashboard_target, minimap_target):
    """탐지 결과 하나를 용도가 다른 세 목적지에 각각 다른 형태로 보낸다.

      - ROS2 브릿지(:9999), 데이터 PC(:9998)
          -> {"timestamp": ..., "detections": [...]}  전체 payload 그대로
      - 미니맵(:9091)
          -> {"label": ..., "angle_offset": ...}  탐지 1건당 각도만 별도 패킷

    실패는 조용히 삼키지 않고 호출부(main)가 카운트해 로그로 남긴다
    (IP 오타·네트워크 단절을 알아채기 위함, 이 요약본에는 로그 생략).
    """
    payload = {"timestamp": time.time(), "detections": detections}
    payload_bytes = json.dumps(payload).encode("utf-8")

    sock.sendto(payload_bytes, ros2_target)
    sock.sendto(payload_bytes, dashboard_target)

    for det in detections:
        angle_payload = json.dumps({
            "label": det["object"],
            "angle_offset": det["angle_offset"],
        }).encode("utf-8")
        sock.sendto(angle_payload, minimap_target)


# ============================================================
# 9. 입력 안정성 방어 코드  (원본 L19-38, L751-843 요약)
# ============================================================
def looks_like_combined_stereo_frame(width, height, min_aspect=1.9) -> bool:
    """카메라가 좌우 통합 해상도(예: 2560x720) 대신 모노 해상도로 잘못
    협상되면, 좌우 절반을 서로 다른 카메라 영상으로 오인해 NanoOWL 탐지와
    StereoSGBM 거리 모두 그럴듯한 모양의 잘못된 값을 낸다.

    원본은 stereo_calibrate.check_stereo_frame()을 우선 쓰고, 그 모듈이
    없는 구버전 배포에서도 최소한의 가로/세로 비율 검사로 이 상태를
    잡아낸다(V4L2 대신 다른 백엔드가 자동 선택될 때 특히 자주 발생했다).
    """
    if height <= 0:
        return False
    return (width / float(height)) >= min_aspect


# stereo_calibrate.py는 Jetson에 손으로 복사해 붙여넣는 일이 많다. 이 모듈이
# 없어도(ImportError) 탐지 자체는 계속 동작해야 한다 — "거리 정확도를 잃는
# 것"과 "아무것도 못 보는 것"은 완전히 다른 심각도의 문제이기 때문이다.
# 원본에서는 이 보호를 위해 아래처럼 import를 개별적으로 감싼다.
#
#   try:
#       from stereo_calibrate import load_rectifier
#   except ImportError:
#       load_rectifier = None


# ============================================================
# 10. (참고) MJPEG 스트리밍 인코딩 캐시  (원본 L151-195 요약)
# ============================================================
# 문제였던 것: 클라이언트(대시보드 iframe + 브라우저 탭)마다 매번 새로
# JPEG을 인코딩했다. 보는 사람이 늘수록 같은 프레임을 여러 번 인코딩해
# 젯슨 CPU가 그만큼 더 소모됐고, 결국 모두에게 화면이 느려졌다.
#
# 개선: (프레임 번호, 박스 번호) 조합이 이전과 같으면 이미 만든 JPEG을
# 그대로 재사용한다. 같은 그림이면 결과가 완전히 같기 때문이다.
def get_cached_jpeg(cache, frame_seq, overlay_seq, encode_fn):
    """캐시 키가 바뀌지 않았으면 encode_fn을 다시 부르지 않는다.

    cache는 {"frame_seq":..., "overlay_seq":..., "data":...} 형태의 dict.
    실제 코드는 이 캐시를 스레드 락으로 보호하고 히트/미스 횟수를
    /health에 노출한다(이 요약에서는 캐시 재사용 개념만 남김).
    """
    if cache.get("frame_seq") == frame_seq and cache.get("overlay_seq") == overlay_seq:
        return cache["data"], True   # (데이터, 캐시 히트 여부)
    data = encode_fn()
    cache["frame_seq"] = frame_seq
    cache["overlay_seq"] = overlay_seq
    cache["data"] = data
    return data, False


# ============================================================
# 판정 로직의 진화 — 왜 지금 방식으로 정착했는가
# ============================================================
EVOLUTION_LOG = [
    (
        "초기 (text_prompt 순서 고정)",
        "\"person with no helmet\"을 0번 프롬프트로 두고, 겹치면 점수와 무관하게 항상 위반이 남도록 고정",
        "안전모를 제대로 쓴 작업자도 위반 쿼리가 함께 걸려, 일반 person이 대시보드에 사실상 나타나지 않음(오탐)",
    ),
    (
        "1차 개선 (점수 + HELMET_SAFETY_MARGIN)",
        "겹친 두 클래스 중 점수가 높은 쪽을 남기되, 위반 클래스에 안전 마진(기본 0.05)을 가산해 애매하면 위반 쪽을 우선",
        "부정문 프롬프트 자체의 근본 문제(같은 'person' 토큰 공유로 인한 동시 반응)는 남아, 판정이 프레임 단위로 흔들림",
    ),
    (
        "현재 (2026-09-06, 긍정 탐지 + 머리 영역 + 4초 지속 확인)",
        "부정문 프롬프트를 없애고 person/safety helmet 두 긍정 객체만 검출, 머리 영역 검사 + 4초 지속 미확인일 때만 위반 확정",
        "한 프레임의 순간적인 가림·각도 변화로 인한 오탐을 줄이고, 정상 복귀는 지연 없이 즉시 처리",
    ),
]


# ============================================================
# 코드 지도 (Code Map) — 원본 파일 대비 위치와 역할
# ============================================================
CODE_MAP = [
    # (이름,                              원본 위치,        역할)
    ("PERSON_LABEL / HELMET_LABEL / ...", "L217-232",   "탐지 클래스명·하류 계약·PPE 판정 상수"),
    ("box_iou",                           "L235-248",   "두 박스 IoU 계산"),
    ("_label_name",                       "L251-254",   "라벨 인덱스 -> 문자열 변환"),
    ("suppress_overlapping_detections",   "L257-288",   "같은 클래스 중복 박스 제거"),
    ("helmet_in_head_region",             "L291-307",   "안전모가 머리 영역 안에 있는지 판정"),
    ("HelmetComplianceTracker",           "L310-364",   "인물 추적 + 4초 지속 미확인 판정"),
    ("apply_helmet_compliance",           "L367-389",   "helmet 박스 제거 + 최종 라벨 확정"),
    ("main() 캘리브레이션 적용 구간",       "L894-939",   "실측 > 환경변수 > 추측값 순으로 f/B/주점 결정"),
    ("camera_capture_loop",               "L392-470",   "카메라 캡처를 추론 루프와 분리하는 스레드, 좌우 정렬"),
    ("main() 거리 계산 구간",              "L994-1037",  "탐지 있을 때만 SGBM 실행, disparity -> distance_meter"),
    ("main() 각도 계산 구간",              "L1052-1057", "박스 중심 -> 카메라 기준 각도(미니맵용)"),
    ("main() 네트워크 설정·UDP 전송 구간",  "L661-701, L1076-1094", "목적지별 IP 분리, ROS2·데이터PC·미니맵 3-way 전송"),
    ("_in_container / 스테레오 프레임 검증", "L198-211, L830-843", "Docker 환경 판별, 좌우 통합 프레임 오협상 검출"),
    ("get_stream_jpeg / _jpeg_cache",     "L133-195",   "MJPEG 인코딩 결과 캐시로 다중 클라이언트 부하 절감"),
]

# 이 요약에서 코드를 옮기지 않고 개념만 정리한 것 (핵심 판정 로직이 아니라
# HTTP 서버·로그 출력 위주라서 발췌만 함):
#   - DualStreamingHandler / send_health : HTTP 서버, /health 성능 지표 JSON
#   - draw_overlays                      : 스트리밍 프레임 위에 박스 그리기
#   - main()의 카메라 열기·해상도 설정·로그 출력 절차
