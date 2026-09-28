#!/usr/bin/env python3
"""스테레오 카메라 캘리브레이션 — 촬영 · 계산 · 검증 · 런타임 적용.

왜 필요한가
-----------
`ai_inference_sender.py` 는 좌/우 영상의 시차(disparity)로 거리를 잽니다.

    거리 Z = (초점거리 f × 두 렌즈 간격 B) / 시차 d

여기서 `f` 와 `B` 를 코드에 상수로 박아 두면(예전에는 500.0 / 0.06 이었습니다)
**그 값이 틀린 만큼 모든 거리가 그대로 틀립니다.** `f` 가 2배 작으면 2.7m 에 선
사람이 1.35m 로 읽히고, common/schema.py 의 1.5m 위험 기준에 걸려 "주의" 여야 할
상황이 "위험" 으로 올라갑니다. 같은 `f` 가 미니맵 방향각 계산에도 쓰이므로
(`angle_offset = atan2(dx, f)`), 작업자 마커가 엉뚱한 각도에 찍히기도 합니다.

그리고 더 중요한 것이 **rectification(정렬)** 입니다. `cv2.StereoSGBM` 은 왼쪽
영상 N번째 줄의 점을 오른쪽 영상 **같은 N번째 줄에서만** 찾습니다. 이 가정은 두
카메라가 완벽히 평행하고 같은 높이일 때만 성립하는데, 실제 모듈은 조립 공차로
1~2° 씩 틀어져 있습니다. 그러면 진짜 대응점이 302번째 줄에 있어도 SGBM 은 못 찾고,
시차가 아예 안 나오거나(-1 로 마스킹) 엉뚱한 값이 나옵니다.

캘리브레이션은 이 두 가지를 한 번에 해결합니다.
  - 실측한 f, B (더 이상 추측값이 아님)
  - 두 영상을 미리 보정해 대응점이 반드시 같은 줄에 오도록 만드는 remap 맵


사용법 (젯슨에서, 화면 없이 동작합니다)
---------------------------------------
1. 체스보드를 인쇄해 단단한 판에 붙입니다. 기본값은 **10x7 칸 = 내부 코너 9x6**,
   한 칸 25mm 입니다. 다르면 --cols/--rows/--square 로 알려주세요.

    # 20쌍을 자동으로 모읍니다. 체스보드가 좌우 **양쪽**에 다 보일 때만 저장됩니다.
    # 거리·각도·화면 위치를 바꿔 가며 천천히 움직이세요.
    python3 stereo_calibrate.py capture --count 20

2. 계산합니다. RMS 오차가 1.0 픽셀을 넘으면 촬영을 다시 하세요.

    python3 stereo_calibrate.py calibrate

3. 검증합니다. 정렬 오차(평균 수직 어긋남)가 0.5픽셀 아래면 성공입니다.

    python3 stereo_calibrate.py verify

4. 결과 확인. 여기서 나오는 초점거리를 event_detector.py 에도 알려줘야 합니다.

    python3 stereo_calibrate.py report

이후 `ai_inference_sender.py` 는 `stereo_calibration.npz` 가 옆에 있으면 자동으로
읽어서 씁니다. 없으면 예전처럼 추측값으로 돌되 시작할 때 경고를 냅니다.
"""

import argparse
import glob
import os
import sys
import time

import cv2
import numpy as np

# Windows 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
#
# import 를 감싸는 이유는 ai_inference_sender.py 와 같습니다 — 이 파일은 젯슨에
# 단독으로 복사되는 일이 있어서, common/ 패키지가 옆에 없을 수 있습니다.
# 젯슨(리눅스)은 원래 UTF-8 이라 없어도 아무 문제가 없습니다.
try:
    from common.console import enable_utf8_console
    enable_utf8_console()
except ImportError:
    # 이 파일만 단독 복사해 Windows에서 실행해도 진단 메시지의 이모지 때문에
    # CP949 인코딩 오류로 죽지 않게 합니다. Jetson의 UTF-8 콘솔에는 영향 없습니다.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

# 캘리브레이션 결과 파일. 환경변수로 위치를 옮길 수 있습니다.
DEFAULT_CALIBRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "stereo_calibration.npz")
CALIBRATION_PATH = os.getenv("STEREO_CALIBRATION_FILE", DEFAULT_CALIBRATION_PATH)

# 촬영본을 모아 두는 곳.
DEFAULT_CAPTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "calib_shots")

# 해상도가 다를 때 가로세로 비가 이보다 더 어긋나면 스케일링을 거부합니다.
# 비가 바뀌었다는 건 리사이즈가 아니라 크롭됐다는 뜻이라, 내부 파라미터를
# 비례로 늘리는 것이 성립하지 않습니다. 이럴 때는 틀린 거리를 자신 있게
# 내놓느니 거리를 포기하는 쪽이 낫습니다(하류가 -1 을 "거리 미상"으로 다룹니다).
ASPECT_TOLERANCE = 0.01

# 주점이 화면 중심에서 이 비율 넘게 벗어나면 촬영 분포를 의심합니다.
# (렌즈 광축이 센서 정중앙을 살짝 벗어나는 것은 정상이지만, 10%는 큽니다.)
PRINCIPAL_OFFSET_WARN = 0.10

# 코너 위치를 서브픽셀까지 다듬는 기준. 캘리브레이션 정확도가 여기서 갈립니다.
_CORNER_CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)

_DETECT_FLAGS = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE

# 촬영 루프에서 "보드가 보이나"만 볼 때 줄이는 배수.
#
# **미탐지가 비쌉니다.** 1280x720 원본에서 장면에 따라 프레임당 0.3~2.6초까지
# 나오고(텍스처가 많을수록 느립니다), 좌우 두 장이면 그 두 배입니다. 그동안
# 화면에 아무것도 안 나오니 사용자는 자세를 바꿔도 반응이 없다고 느낍니다.
# 절반으로 줄이면 3~8배 빨라지는데, 보드가 있으면 축소본에서도 그대로 찾힙니다.
#
# (cv2.CALIB_CB_FAST_CHECK 도 시험해 봤지만 이 빌드에서는 이득이 없었습니다 —
#  오히려 2~6ms 더 걸려서 쓰지 않습니다.)
DETECT_DOWNSCALE = 2

# capture 가 진행 상황을 알려주는 간격(초).
#
# **이게 없으면 화면이 완전히 조용합니다.** 코너를 못 찾는 동안 예전 코드는
# 아무것도 출력하지 않고 continue 만 했습니다. 그런데 미탐지 한 번이 프레임당
# 수백 ms(젯슨 CPU)라 루프가 1~2fps 로 돌고, 사용자 입장에서는 "멈춘 것"과
# "체스보드를 못 알아보는 것"을 구분할 방법이 없었습니다.
CAPTURE_STATUS_INTERVAL_SEC = 2.0

# doctor 가 시험해 보는 흔한 체스보드 규격 (내부 코너 가로 x 세로).
# 인쇄물마다 다르고, 칸 수와 내부 코너 수를 헷갈리는 것이 가장 흔한 실수입니다
# (10x7 칸 = 9x6 내부 코너). 그래서 맞는 것을 직접 찾아 줍니다.
COMMON_PATTERNS = ((9, 6), (8, 6), (7, 6), (9, 7), (8, 5), (7, 5), (6, 5),
                   (11, 8), (10, 7), (6, 9), (6, 8), (5, 8))


# ==========================================================================
# 저장 형식
# ==========================================================================
class StereoCalibration:
    """카메라 두 대의 내부 파라미터와 서로의 위치 관계.

    저장하는 것은 **실측값뿐** 입니다. 정렬(rectification) 결과는 이 값들에서
    언제든 다시 계산할 수 있으므로 저장하지 않습니다 — 저장해 두면 해상도나
    alpha 를 바꿀 때 둘이 어긋나기 시작합니다.
    """

    def __init__(self, K1, D1, K2, D2, R, T, image_size, rms=None):
        self.K1 = np.asarray(K1, dtype=np.float64)
        self.D1 = np.asarray(D1, dtype=np.float64)
        self.K2 = np.asarray(K2, dtype=np.float64)
        self.D2 = np.asarray(D2, dtype=np.float64)
        self.R = np.asarray(R, dtype=np.float64)
        self.T = np.asarray(T, dtype=np.float64).reshape(3, 1)
        self.image_size = (int(image_size[0]), int(image_size[1]))  # (width, height)
        self.rms = None if rms is None else float(rms)

    # ---------------------------------------------------------------- 입출력
    def save(self, path: str) -> None:
        np.savez(path, K1=self.K1, D1=self.D1, K2=self.K2, D2=self.D2,
                 R=self.R, T=self.T,
                 image_width=self.image_size[0], image_height=self.image_size[1],
                 rms=(-1.0 if self.rms is None else self.rms))

    @classmethod
    def load(cls, path: str):
        """읽지 못하면 None. 캘리브레이션이 없다고 주행/추론을 멈출 수는 없습니다."""
        if not path or not os.path.exists(path):
            return None
        try:
            with np.load(path) as data:
                rms = float(data["rms"])
                return cls(data["K1"], data["D1"], data["K2"], data["D2"],
                           data["R"], data["T"],
                           (int(data["image_width"]), int(data["image_height"])),
                           None if rms < 0 else rms)
        except (KeyError, ValueError, OSError) as exc:
            print(f"⚠ [캘리브레이션] {path} 를 읽지 못했습니다: {exc}")
            return None

    # ---------------------------------------------------------------- 해상도
    def scaled_to(self, width: int, height: int):
        """다른 해상도용으로 내부 파라미터를 비례 조정한 사본.

        카메라가 요청한 해상도를 거부하고 다른 크기를 줄 수 있어서 필요합니다
        (ai_inference_sender.py 도 요청값과 실제값을 따로 출력합니다). 픽셀 단위인
        초점거리와 주점은 해상도에 비례하므로 늘리면 되지만, **가로세로 비가
        달라지면** 리사이즈가 아니라 크롭이라는 뜻이라 이 방법이 성립하지 않습니다.
        그 경우 None 을 돌려줍니다.
        """
        old_w, old_h = self.image_size
        if width <= 0 or height <= 0 or old_w <= 0 or old_h <= 0:
            return None
        if (width, height) == self.image_size:
            return self

        sx, sy = width / old_w, height / old_h
        if abs(sx - sy) > ASPECT_TOLERANCE * max(sx, sy):
            return None

        def scale(K):
            out = K.copy()
            out[0, 0] *= sx
            out[0, 2] *= sx
            out[1, 1] *= sy
            out[1, 2] *= sy
            return out

        # 왜곡계수(D)는 정규화 좌표계에서 정의되므로 해상도와 무관합니다.
        return StereoCalibration(scale(self.K1), self.D1, scale(self.K2), self.D2,
                                 self.R, self.T, (width, height), self.rms)

    # ---------------------------------------------------------------- 정렬
    def rectification(self, alpha: float = 0.0):
        """(R1, R2, P1, P2, Q) 를 돌려준다.

        alpha=0 은 유효 픽셀만 남기고 잘라내는 설정입니다. 가장자리의 검은 여백에
        시차 계산이 낚이지 않게 하려는 것입니다.
        """
        R1, R2, P1, P2, Q, _roi1, _roi2 = cv2.stereoRectify(
            self.K1, self.D1, self.K2, self.D2, self.image_size,
            self.R, self.T, flags=cv2.CALIB_ZERO_DISPARITY, alpha=alpha)
        return R1, R2, P1, P2, Q


class StereoRectifier:
    """런타임에서 매 프레임 쓰는 것: remap 맵 + 실측 f/B/주점.

    맵을 만드는 것은 한 번뿐이고, 이후에는 cv2.remap 두 번만 돕니다.
    """

    def __init__(self, calibration: StereoCalibration, alpha: float = 0.0):
        self.calibration = calibration
        R1, R2, P1, P2, Q = calibration.rectification(alpha)
        self.P1, self.P2, self.Q = P1, P2, Q

        size = calibration.image_size
        self.map1x, self.map1y = cv2.initUndistortRectifyMap(
            calibration.K1, calibration.D1, R1, P1, size, cv2.CV_16SC2)
        self.map2x, self.map2y = cv2.initUndistortRectifyMap(
            calibration.K2, calibration.D2, R2, P2, size, cv2.CV_16SC2)

    @property
    def focal_px(self) -> float:
        """정렬 후 두 카메라가 공유하는 초점거리(픽셀)."""
        return float(self.P1[0, 0])

    @property
    def baseline_m(self) -> float:
        """정렬 후 두 렌즈 간격(m).

        stereoRectify 는 P2 의 4번째 열에 (Tx * f) 를 넣습니다. 초점거리로 나누면
        실측 baseline 이 그대로 나옵니다 — 자로 잰 값을 넣을 필요가 없습니다.
        """
        return abs(float(self.P2[0, 3]) / self.focal_px)

    @property
    def principal_x(self) -> float:
        """정렬 후 광축이 지나는 x(픽셀).

        미니맵 방향각(angle_offset)은 "화면 중심" 이 아니라 여기를 기준으로 재야
        합니다. 렌즈 광축이 센서 정중앙에 있다는 보장이 없습니다.
        """
        return float(self.P1[0, 2])

    @property
    def principal_y(self) -> float:
        return float(self.P1[1, 2])

    def rectify_pair(self, left, right):
        """좌/우 영상을 정렬된 좌표계로 옮긴다. 흑백·컬러 모두 됩니다."""
        return (cv2.remap(left, self.map1x, self.map1y, cv2.INTER_LINEAR),
                cv2.remap(right, self.map2x, self.map2y, cv2.INTER_LINEAR))


def load_rectifier(width: int, height: int, path: str = None, alpha: float = 0.0):
    """한쪽 눈 크기에 맞는 StereoRectifier. 없거나 못 맞추면 None.

    ai_inference_sender.py 가 부르는 진입점입니다. None 이 돌아오면 호출부가
    예전처럼 추측값으로 돌되 경고를 냅니다 — 여기서 예외를 던지면 캘리브레이션이
    없다는 이유만으로 탐지 자체가 죽습니다.
    """
    calibration = StereoCalibration.load(path or CALIBRATION_PATH)
    if calibration is None:
        return None

    scaled = calibration.scaled_to(width, height)
    if scaled is None:
        print(f"⚠ [캘리브레이션] 촬영 해상도 {calibration.image_size[0]}x"
              f"{calibration.image_size[1]} 와 현재 {width}x{height} 의 "
              f"가로세로 비가 다릅니다. 정렬을 건너뜁니다.")
        print("   → 지금 해상도로 다시 촬영해서 캘리브레이션하세요.")
        return None

    try:
        return StereoRectifier(scaled, alpha)
    except cv2.error as exc:
        print(f"⚠ [캘리브레이션] 정렬 맵 생성 실패: {exc}")
        return None


# ==========================================================================
# 1단계 — 촬영
# ==========================================================================
def open_camera(index: int, width: int, height: int):
    """스테레오 카메라를 연다. (cap, 실제너비, 실제높이). 실패하면 (None, 0, 0).

    **백엔드를 V4L2 로 못박는 이유.** 젯슨에서 백엔드를 안 주면 OpenCV 가
    GStreamer 를 고르는데, USB UVC 스테레오 카메라에서 이렇게 죽습니다.

        (Argus) Error EndOfFile ...
        v4l2src0 reported: Internal data stream error
        실제 입력 캡처 크기: 0x0

    게다가 CAP_PROP_FOURCC(MJPG) 설정이 GStreamer 경로에서는 먹지 않아
    2560x720 협상 자체가 실패합니다. 같은 이유로 ai_inference_sender.py 도
    V4L2 를 먼저 씁니다 — **두 파일이 같은 카메라를 여는데 방식이 다르면,
    센더는 되는데 캘리브레이션만 안 되는 상황이 생깁니다.** 실제로 그랬습니다.
    """
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        print("⚠ V4L2 백엔드로 열지 못해 기본 백엔드로 재시도합니다.")
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        return None, 0, 0

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))


def print_camera_help(index: int) -> None:
    """카메라가 안 열리거나 스트림이 죽었을 때 짚어 볼 것들."""
    print(f"     ls -l /dev/video*                     장치가 보이는지")
    print(f"     docker inspect -f '{{{{range .HostConfig.Devices}}}}"
          f"{{{{println .PathOnHost}}}}{{{{end}}}}' <컨테이너>   "
          f"컨테이너에 넘어갔는지")
    print(f"     장치 번호가 다르면: --device 1")
    print(f"   해상도를 보려면 v4l2-ctl 이 필요합니다(젯슨에 기본으로 없습니다):")
    print(f"     sudo apt install -y v4l-utils")
    print(f"     v4l2-ctl -d /dev/video{index} --list-formats-ext")


def _split_eyes(frame):
    height, width = frame.shape[:2]
    mid = width // 2
    return frame[:, 0:mid], frame[:, mid:width]


def _find_corners(gray, pattern):
    """체스보드 내부 코너를 (N, 1, 2) float32 로 돌려준다. 못 찾으면 None.

    모양을 여기서 통일하는 이유: findChessboardCorners 의 반환 모양이 OpenCV
    버전마다 다릅니다. 4.x 는 (N, 1, 2), 5.0 은 (N, 2) 를 줍니다. 젯슨(4.x)과
    개발 PC(5.x)가 서로 다른 버전을 쓰고 있어서, 한쪽 모양만 가정하면 다른
    쪽에서 IndexError 로 죽습니다. 캘리브레이션 함수들은 두 모양을 다 받지만
    verify 의 좌표 비교(corners[:, 0, 1])는 모양을 탑니다.
    """
    found, corners = cv2.findChessboardCorners(gray, pattern, _DETECT_FLAGS)
    if not found:
        return None
    corners = np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), _CORNER_CRITERIA)


def check_stereo_frame(width: int, height: int) -> str:
    """좌우가 나란히 붙은 스테레오 프레임처럼 보이는지. 문제 없으면 빈 문자열.

    **가장 찾기 어려운 실패가 여기서 옵니다.** 카메라가 요청한 2560x720 을
    거부하고 1280x720 (모노) 을 주면, _split_eyes 가 그 한 장을 좌우로 쪼갭니다.
    그러면 "오른쪽 눈"에는 체스보드의 오른쪽 절반만 들어가므로 코너를 **영원히**
    못 찾습니다. 예전 코드는 실제 해상도를 찍어만 주고 넘어가서, 사용자는
    체스보드나 조명을 의심하며 시간을 씁니다.

    나란히 붙은 스테레오는 가로가 세로의 약 2배 이상입니다(1280x480 은 2.67,
    2560x720 은 3.56). 1.9 미만이면 한 장짜리로 봅니다.
    """
    if width <= 0 or height <= 0:
        return f"프레임 크기가 올바르지 않습니다 ({width}x{height})."
    if width % 2:
        return f"프레임 폭 {width}은 좌우로 정확히 나눌 수 없는 홀수입니다."
    ratio = width / float(height)
    if ratio < 1.9:
        return (f"{width}x{height} (가로/세로 {ratio:.2f}) 는 좌우가 나란히 붙은 "
                f"스테레오로 보이지 않습니다. 한 장짜리 영상을 반으로 쪼개면 "
                f"오른쪽 눈에 체스보드가 절반만 들어가 코너를 영영 못 찾습니다.")
    return ""


def check_stereo_image(frame) -> str:
    """드라이버 보고값이 아닌 실제 프레임 배열을 검사합니다."""
    if frame is None or not hasattr(frame, "shape") or len(frame.shape) < 2:
        return "실제 프레임 배열이 비어 있거나 형식이 잘못됐습니다."
    height, width = frame.shape[:2]
    return check_stereo_frame(int(width), int(height))


def _describe_capture_status(saved, target, frames, seen, pattern) -> list:
    """최근 구간에서 무엇이 보였는지 + 다음에 뭘 해야 하는지. 출력할 줄 목록.

    순수 함수라 카메라 없이 테스트할 수 있습니다.
    """
    lines = [f"  [{saved}/{target}] 탐색 중 — 최근 {frames}프레임: "
             f"양쪽 {seen['both']} · 왼쪽만 {seen['left']} · "
             f"오른쪽만 {seen['right']} · 못 찾음 {seen['none']}"]

    if frames == 0:
        return lines

    if seen["none"] == frames:
        lines.append(f"      한 프레임도 못 알아봤습니다. 내부 코너 수가 "
                     f"{pattern[0]}x{pattern[1]} 이 맞나요? "
                     f"(10x7 **칸** 판이면 9x6 입니다)")
        lines.append("      맞는데도 계속 이러면: python3 stereo_calibrate.py doctor")
    elif seen["left"] + seen["right"] > seen["both"]:
        side = "오른쪽" if seen["left"] > seen["right"] else "왼쪽"
        lines.append(f"      한쪽 눈에만 보입니다. 체스보드를 {side}으로 조금 옮기거나 "
                     f"뒤로 물러나 **양쪽 화면에 다 들어오게** 하세요.")
    return lines


def has_chessboard(gray, pattern, downscale: int = DETECT_DOWNSCALE) -> bool:
    """이 영상에 체스보드가 보이는지만 판단합니다. 좌표는 돌려주지 않습니다.

    **capture 는 코너 좌표를 쓰지 않습니다.** 사진을 저장할지 말지 정하는 데만
    쓰고, 실제 좌표는 calibrate 가 저장된 원본에서 다시 구합니다. 그래서 여기서는
    정확도가 아니라 **속도**가 중요합니다 — 축소본에서 판단해도 최종 결과의
    정밀도에는 아무 영향이 없습니다.

    downscale=1 이면 원본 그대로 봅니다(느리지만 가장 민감).
    """
    if downscale > 1 and min(gray.shape[:2]) // downscale >= 60:
        h, w = gray.shape[:2]
        gray = cv2.resize(gray, (w // downscale, h // downscale),
                          interpolation=cv2.INTER_AREA)
    found, _ = cv2.findChessboardCorners(gray, pattern, _DETECT_FLAGS)
    return bool(found)


def capture(args) -> int:
    """체스보드가 좌우 **양쪽**에 보이는 순간만 골라 저장합니다.

    화면 표시(cv2.imshow)를 쓰지 않습니다. 젯슨의 도커 컨테이너 안에는 보통
    디스플레이가 없어서, GUI 를 쓰면 거기서 바로 죽습니다. 대신 몇 장 모았는지
    터미널에 계속 알려줍니다.
    """
    pattern = (args.cols, args.rows)
    os.makedirs(args.output, exist_ok=True)

    cap, actual_w, actual_h = open_camera(args.device, args.width, args.height)
    if cap is None:
        print(f"❌ 카메라를 열지 못했습니다: /dev/video{args.device}")
        print_camera_help(args.device)
        return 1

    print(f"카메라 {actual_w}x{actual_h} (요청 {args.width}x{args.height})")

    # 0x0 은 "열리기는 했는데 스트림이 죽은" 상태입니다. 이대로 두면 아래
    # 탐색 루프가 프레임을 한 장도 못 받으면서 조용히 계속 돕니다.
    if actual_w <= 0 or actual_h <= 0:
        print()
        print(f"❌ 카메라는 열렸지만 스트림이 없습니다 ({actual_w}x{actual_h}).")
        print_camera_help(args.device)
        cap.release()
        return 1

    reported_problem = check_stereo_frame(actual_w, actual_h)
    if reported_problem:
        # cap.get() 값이 틀리는 드라이버도 있으므로 여기서는 경고만 하고,
        # 바로 아래 실제 read() 결과로 최종 판정합니다.
        print(f"⚠ 드라이버 보고 크기 주의: {reported_problem}")

    # 일부 UVC 드라이버는 cap.get()에는 요청값을 그대로 돌려주면서 실제 read()에는
    # 더 작은 단안 프레임을 반환합니다. 보고값만 믿으면 이 한 장을 좌우로 쪼개고
    # 체스보드를 영원히 못 찾으므로 첫 실제 프레임으로 다시 검증합니다.
    first_frame = None
    for _ in range(10):
        ok, candidate = cap.read()
        if ok and candidate is not None:
            first_frame = candidate
            break
    if first_frame is None:
        print("❌ 카메라는 열렸지만 실제 프레임을 읽지 못했습니다.")
        print_camera_help(args.device)
        cap.release()
        return 1

    frame_h, frame_w = first_frame.shape[:2]
    if (frame_w, frame_h) != (actual_w, actual_h):
        print(f"⚠ 드라이버 보고값 {actual_w}x{actual_h}와 실제 프레임 "
              f"{frame_w}x{frame_h}가 다릅니다. 실제 프레임을 기준으로 검사합니다.")
    problem = check_stereo_image(first_frame)
    if problem:
        print(f"❌ 실제 프레임 검사 실패: {problem}")
        print("   카메라가 지원하는 좌우 결합 해상도를 확인하세요:")
        print("     v4l2-ctl --list-formats-ext -d /dev/video%d" % args.device)
        print("   python3 stereo_calibrate.py doctor 로 좌우 영상을 확인하세요.")
        cap.release()
        return 1
    actual_w, actual_h = frame_w, frame_h
    print(f"체스보드 내부 코너 {args.cols}x{args.rows}, 한 칸 {args.square*1000:.0f}mm")
    print(f"{args.count}쌍을 모읍니다. 최소 {args.interval:.1f}초 간격으로 저장합니다.")
    print()
    print("  좋은 촬영 요령")
    print("   - 가까이(0.5m)부터 멀리(1.5m)까지 거리를 바꿔 가며")
    print("   - 화면 가운데뿐 아니라 네 귀퉁이에도 체스보드를 두고")
    print("   - 정면뿐 아니라 상하좌우로 20~30도씩 기울여서")
    print("   - ★ 기울기와 화면 위치를 **따로** 섞으세요. 예를 들어 왼쪽에 둘 때")
    print("        항상 같은 방향으로만 기울이면, 초점거리와 주점이 서로 상쇄되는")
    print("        엉뚱한 값으로 수렴합니다 (RMS 는 낮게 나와서 알아채기 어렵습니다).")
    print("   - 흔들리지 않게 잠깐씩 멈춰 주세요")
    print()

    saved = 0
    last_saved_at = 0.0
    misses = 0

    # 진행 상황 집계. 예전에는 저장에 성공했을 때만 출력해서, 코너를 못 찾는
    # 동안 화면이 통째로 조용했습니다 — "멈췄나?" 와 "체스보드를 못 알아보나?"
    # 를 구분할 단서가 없었습니다.
    last_status_at = time.time()
    seen = {"both": 0, "left": 0, "right": 0, "none": 0}
    frames = 0
    try:
        while saved < args.count:
            if first_frame is not None:
                ok, frame = True, first_frame
                first_frame = None
            else:
                ok, frame = cap.read()
            if not ok:
                misses += 1
                if misses % 50 == 0:
                    print(f"⚠ 프레임을 읽지 못했습니다 (연속 {misses}회)")
                time.sleep(0.01)
                continue
            misses = 0

            if time.time() - last_saved_at < args.interval:
                continue

            left, right = _split_eyes(frame)
            gray_l = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
            gray_r = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)

            # 축소본에서 "보이나"만 봅니다. 좌표는 어차피 안 쓰고(저장할 사진을
            # 고르는 용도), 정확한 코너는 calibrate 가 원본에서 다시 구합니다.
            has_left = has_chessboard(gray_l, pattern, args.downscale)
            has_right = has_chessboard(gray_r, pattern, args.downscale)

            frames += 1
            if has_left and has_right:
                seen["both"] += 1
            elif has_left:
                seen["left"] += 1
            elif has_right:
                seen["right"] += 1
            else:
                seen["none"] += 1

            now = time.time()
            if now - last_status_at >= CAPTURE_STATUS_INTERVAL_SEC:
                for line in _describe_capture_status(saved, args.count, frames,
                                                     seen, pattern):
                    print(line)
                last_status_at = now
                frames = 0
                seen = dict.fromkeys(seen, 0)

            if not (has_left and has_right):
                continue

            index = saved + 1
            cv2.imwrite(os.path.join(args.output, f"left_{index:02d}.png"), left)
            cv2.imwrite(os.path.join(args.output, f"right_{index:02d}.png"), right)
            saved = index
            last_saved_at = time.time()
            print(f"  [{saved}/{args.count}] 저장했습니다. 자세를 바꿔 주세요.")
    except KeyboardInterrupt:
        print("\n중단했습니다.")
    finally:
        cap.release()

    print()
    if saved < args.min_pairs:
        print(f"❌ {saved}쌍뿐입니다. 최소 {args.min_pairs}쌍은 있어야 합니다.")
        return 1
    print(f"✅ {saved}쌍을 {args.output} 에 저장했습니다.")
    print("   다음: python3 stereo_calibrate.py calibrate")
    return 0


# ==========================================================================
# 2단계 — 계산
# ==========================================================================
def is_rms_acceptable(rms: float, max_rms: float) -> bool:
    """NaN/무한대와 허용치를 넘는 결과는 런타임 파일로 승인하지 않습니다."""
    return bool(np.isfinite(rms) and rms <= max_rms)


def calibrate(args) -> int:
    pattern = (args.cols, args.rows)
    left_paths = sorted(glob.glob(os.path.join(args.input, "left_*.png")))
    if not left_paths:
        print(f"❌ {args.input} 에 촬영본이 없습니다. 먼저 capture 를 실행하세요.")
        return 1

    # 체스보드 한 장의 3D 좌표 (판이 평면이므로 z=0). 실제 칸 크기를 곱해야
    # baseline 이 미터 단위로 나옵니다 — 안 곱하면 "칸 수" 단위가 됩니다.
    objp = np.zeros((args.rows * args.cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:args.cols, 0:args.rows].T.reshape(-1, 2)
    objp *= args.square

    obj_points, left_points, right_points = [], [], []
    image_size = None
    skipped = []

    for left_path in left_paths:
        right_path = left_path.replace("left_", "right_")
        if not os.path.exists(right_path):
            skipped.append((os.path.basename(left_path), "짝이 되는 right_ 없음"))
            continue

        left = cv2.imread(left_path, cv2.IMREAD_GRAYSCALE)
        right = cv2.imread(right_path, cv2.IMREAD_GRAYSCALE)
        if left is None or right is None:
            skipped.append((os.path.basename(left_path), "읽기 실패"))
            continue

        if image_size is None:
            image_size = (left.shape[1], left.shape[0])
        elif (left.shape[1], left.shape[0]) != image_size:
            skipped.append((os.path.basename(left_path), "해상도가 다름"))
            continue

        corners_l = _find_corners(left, pattern)
        corners_r = _find_corners(right, pattern)
        if corners_l is None or corners_r is None:
            skipped.append((os.path.basename(left_path), "코너를 못 찾음"))
            continue

        obj_points.append(objp)
        left_points.append(corners_l)
        right_points.append(corners_r)

    for name, reason in skipped:
        print(f"  건너뜀: {name} ({reason})")

    if len(obj_points) < args.min_pairs:
        print(f"❌ 쓸 수 있는 쌍이 {len(obj_points)}개뿐입니다 "
              f"(최소 {args.min_pairs}). 더 촬영하세요.")
        return 1

    print(f"{len(obj_points)}쌍으로 계산합니다 ({image_size[0]}x{image_size[1]})...")

    # 각 카메라를 따로 먼저 구하고, 그 값을 고정한 채 둘 사이 관계만 구합니다.
    # 한 번에 전부 최적화하는 것보다 안정적입니다 — 자유도가 적을수록 촬영본의
    # 노이즈에 덜 휘둘립니다.
    rms_l, K1, D1, _, _ = cv2.calibrateCamera(
        obj_points, left_points, image_size, None, None)
    rms_r, K2, D2, _, _ = cv2.calibrateCamera(
        obj_points, right_points, image_size, None, None)
    print(f"  단안 RMS: 왼쪽 {rms_l:.3f}px / 오른쪽 {rms_r:.3f}px")

    rms, K1, D1, K2, D2, R, T, _E, _F = cv2.stereoCalibrate(
        obj_points, left_points, right_points, K1, D1, K2, D2, image_size,
        flags=cv2.CALIB_FIX_INTRINSIC, criteria=_CORNER_CRITERIA)

    calibration = StereoCalibration(K1, D1, K2, D2, R, T, image_size, rms)

    print()
    print(f"  스테레오 RMS: {rms:.3f}px")
    _print_report(calibration)

    if not is_rms_acceptable(rms, args.max_rms):
        print(f"\n❌ RMS 가 허용 기준 {args.max_rms}px 를 넘거나 유효하지 않습니다.")
        print("   체스보드가 평평한지, 초점이 맞았는지 확인하고 다시 촬영하세요.")
        print(f"   불량 결과는 {args.output} 에 저장하지 않았습니다.")
        print("   같은 경로에 기존 파일이 있었다면 덮어쓰지 않고 그대로 보존했습니다.")
        return 1

    _warn_if_poorly_distributed(calibration)
    calibration.save(args.output)
    print(f"\n✅ {args.output} 에 저장했습니다.")
    print("   다음: python3 stereo_calibrate.py verify")
    return 0


def _warn_if_poorly_distributed(calibration: StereoCalibration) -> None:
    """주점이 화면 중심에서 크게 벗어났으면 촬영 분포를 의심한다.

    **RMS 만으로는 잘못된 캘리브레이션을 못 거릅니다.** 체스보드를 늘 화면
    같은 쪽에만 두고 찍으면, 최적화가 초점거리와 주점을 서로 상쇄시키는 조합을
    찾아냅니다. 그러면 재투영 오차(RMS)는 작게 나오는데 f 와 cx 는 둘 다
    실제와 다릅니다 — 그리고 거리는 f 에 정비례하므로 그대로 틀립니다.

    주점은 보통 화면 중심 근처에 있으므로, 많이 벗어났다면 촬영이 한쪽에
    치우쳤다는 신호입니다.
    """
    rectifier = StereoRectifier(calibration)
    width, height = calibration.image_size
    off_x = abs(rectifier.principal_x - width / 2.0) / width
    off_y = abs(rectifier.principal_y - height / 2.0) / height
    if max(off_x, off_y) <= PRINCIPAL_OFFSET_WARN:
        return

    print(f"\n⚠ 주점이 화면 중심에서 많이 벗어났습니다 "
          f"(가로 {off_x*100:.0f}%, 세로 {off_y*100:.0f}%).")
    print("   RMS 가 낮아도 초점거리가 틀어졌을 수 있습니다 — 체스보드를 늘 화면")
    print("   같은 쪽에만 두고 찍으면 생기는 증상입니다.")
    print("   화면 네 귀퉁이와 가운데, 가까이와 멀리를 골고루 섞어 다시 촬영하세요.")


# ==========================================================================
# 3단계 — 검증
# ==========================================================================
def verify(args) -> int:
    """정렬이 실제로 됐는지 숫자로 확인합니다.

    같은 코너가 좌우 영상에서 **몇 번째 줄에 있는지** 비교합니다. 정렬이 잘 됐다면
    두 줄 번호가 같아야 하므로(수직 차이 0), 이 값이 정렬 품질을 그대로 보여줍니다.
    StereoSGBM 은 같은 줄만 훑기 때문에, 이 값이 크면 시차가 아예 안 잡힙니다.
    """
    calibration = StereoCalibration.load(args.calibration)
    if calibration is None:
        print(f"❌ 캘리브레이션 파일이 없습니다: {args.calibration}")
        return 1

    pattern = (args.cols, args.rows)
    left_paths = sorted(glob.glob(os.path.join(args.input, "left_*.png")))
    if not left_paths:
        print(f"❌ {args.input} 에 촬영본이 없습니다.")
        return 1

    rectifier = StereoRectifier(calibration)
    before, after = [], []

    for left_path in left_paths:
        right_path = left_path.replace("left_", "right_")
        left = cv2.imread(left_path, cv2.IMREAD_GRAYSCALE)
        right = cv2.imread(right_path, cv2.IMREAD_GRAYSCALE)
        if left is None or right is None:
            continue

        raw_l = _find_corners(left, pattern)
        raw_r = _find_corners(right, pattern)
        if raw_l is not None and raw_r is not None:
            before.append(np.abs(raw_l[:, 0, 1] - raw_r[:, 0, 1]).mean())

        rect_l, rect_r = rectifier.rectify_pair(left, right)
        fix_l = _find_corners(rect_l, pattern)
        fix_r = _find_corners(rect_r, pattern)
        if fix_l is not None and fix_r is not None:
            after.append(np.abs(fix_l[:, 0, 1] - fix_r[:, 0, 1]).mean())

    if not after:
        print("❌ 정렬 후 체스보드를 한 장도 찾지 못했습니다. 캘리브레이션이 잘못됐습니다.")
        return 1

    before_mean = float(np.mean(before)) if before else float('nan')
    after_mean = float(np.mean(after))

    print("같은 코너가 좌우 영상에서 세로로 얼마나 어긋나 있는가 (작을수록 좋음)")
    print(f"  정렬 전: {before_mean:.2f} px")
    print(f"  정렬 후: {after_mean:.2f} px   ({len(after)}쌍)")
    print()
    _print_report(calibration)

    if after_mean <= args.max_error:
        print(f"\n✅ 정렬 오차 {after_mean:.2f}px — 기준({args.max_error}px) 안입니다.")
        print("   ai_inference_sender.py 를 다시 시작하면 자동으로 적용됩니다.")
        return 0

    print(f"\n❌ 정렬 오차 {after_mean:.2f}px 가 기준({args.max_error}px)을 넘습니다.")
    print("   이 상태로는 StereoSGBM 이 대응점을 놓쳐 거리가 -1 로 많이 나옵니다.")
    print("   촬영본을 지우고 자세를 더 다양하게 해서 다시 찍어 보세요.")
    return 1


# ==========================================================================
# 결과 보기
# ==========================================================================
def _print_report(calibration: StereoCalibration) -> None:
    rectifier = StereoRectifier(calibration)
    width, height = calibration.image_size
    focal = rectifier.focal_px
    # 화각은 사람이 "이 값이 말이 되나" 를 가늠하는 데 가장 쉬운 숫자입니다.
    hfov = 2.0 * np.degrees(np.arctan2(width / 2.0, focal))

    print(f"  해상도(한쪽 눈): {width}x{height}")
    print(f"  초점거리 f     : {focal:.1f} px   (수평 화각 약 {hfov:.0f}도)")
    print(f"  렌즈 간격 B    : {rectifier.baseline_m*1000:.1f} mm")
    print(f"  주점 (cx, cy)  : ({rectifier.principal_x:.1f}, {rectifier.principal_y:.1f})")
    if calibration.rms is not None:
        print(f"  스테레오 RMS   : {calibration.rms:.3f} px")
    print()
    print("  이 값들을 event_detector.py 에도 알려줘야 합니다 "
          "(거리 -> 지도 좌표 투영에 같은 값을 씁니다).")
    print("  아래 한 줄을 그대로 복사하세요:")
    # **주점(cx, cy)도 함께 넘겨야 합니다.** ai_inference_sender.py 는 미니맵
    # 방향각을 실측 주점 기준으로 재는데, event_detector 가 화면 중심을 쓰면
    # 같은 탐지가 두 화면에서 다른 자리에 찍힙니다. 사람이 위 표에서 숫자를
    # 옮겨 적다 틀리는 일이 없도록 붙여넣을 수 있는 형태로 찍습니다.
    print(f"    ros2 run smart_factory_sim event_detector --ros-args "
          f"-p jetson_focal_length:={focal:.1f} "
          f"-p jetson_frame_width:={float(width)} "
          f"-p jetson_frame_height:={float(height)} "
          f"-p jetson_principal_x:={rectifier.principal_x:.1f} "
          f"-p jetson_principal_y:={rectifier.principal_y:.1f}")


def report(args) -> int:
    calibration = StereoCalibration.load(args.calibration)
    if calibration is None:
        print(f"❌ 캘리브레이션 파일이 없습니다: {args.calibration}")
        print("   python3 stereo_calibrate.py capture 부터 시작하세요.")
        return 1
    print(f"파일: {args.calibration}")
    print()
    _print_report(calibration)
    return 0


# ==========================================================================
# 진단 — "코너가 안 잡힌다" 의 원인을 한 번에 가른다
# ==========================================================================
def find_matching_patterns(gray, patterns=COMMON_PATTERNS,
                           downscale: int = DETECT_DOWNSCALE):
    """이 영상에서 맞는 체스보드 규격을 찾아 목록으로. 순수 함수.

    규격을 여러 개 시험하므로 원본 해상도로 돌리면 아주 느립니다 — 안 맞는
    규격 하나가 최대 2.6초라, 12종이면 30초가 넘습니다. 축소본으로 봅니다.
    """
    return [p for p in patterns if has_chessboard(gray, p, downscale)]


def doctor(args) -> int:
    """카메라 -> 좌우 분할 -> 체스보드 규격 순서로 짚어 원인을 가릅니다.

    capture 가 조용히 아무것도 저장하지 않을 때, 원인이 넷 중 어디인지
    사람이 알 방법이 없어서 만들었습니다.

      1) 카메라가 안 열린다
      2) 좌우가 나란히 붙은 스테레오가 아니다 (한 장을 반으로 쪼개고 있다)
      3) 체스보드 규격(--cols/--rows)이 실제 인쇄물과 다르다
      4) 판은 맞는데 한쪽 눈에만 들어온다
    """
    print("=" * 60)
    print(" 스테레오 캘리브레이션 진단")
    print("=" * 60)
    problems = 0

    print()
    print("── 1. 카메라")
    cap, rep_w, rep_h = open_camera(args.device, args.width, args.height)
    if cap is None:
        print(f"  ❌ /dev/video{args.device} 를 열지 못했습니다.")
        print_camera_help(args.device)
        return 1
    print(f"  드라이버 보고 크기: {rep_w}x{rep_h}")
    if rep_w <= 0 or rep_h <= 0:
        print("  ❌ 열리기는 했지만 스트림이 없습니다 (0x0).")
        print("     백엔드 문제일 때 나오는 전형적인 증상입니다.")
        print_camera_help(args.device)
        cap.release()
        return 1

    ok, frame = None, None
    for _ in range(10):        # 첫 몇 장은 버려집니다(드라이버 워밍업)
        ok, frame = cap.read()
        if ok:
            break
    cap.release()
    if not ok or frame is None:
        print("  ❌ 프레임을 읽지 못했습니다.")
        print_camera_help(args.device)
        return 1

    height, width = frame.shape[:2]
    print(f"  ✅ 열렸습니다. 실제 프레임 {width}x{height} (요청 {args.width}x{args.height})")
    if (width, height) != (args.width, args.height):
        print(f"     ⚠ 요청과 다릅니다 — 카메라가 거부했습니다.")
        print_camera_help(args.device)

    print()
    print("── 2. 좌우 분할")
    problem = check_stereo_frame(width, height)
    if problem:
        print(f"  ❌ {problem}")
        problems += 1
    else:
        print(f"  ✅ 가로/세로 {width/float(height):.2f} — 나란히 붙은 스테레오로 보입니다.")

    left, right = _split_eyes(frame)
    gray_l = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
    gray_r = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
    print(f"     한쪽 눈 크기 {gray_l.shape[1]}x{gray_l.shape[0]}")

    os.makedirs(args.output, exist_ok=True)
    lp = os.path.join(args.output, "doctor_left.png")
    rp = os.path.join(args.output, "doctor_right.png")
    cv2.imwrite(lp, left)
    cv2.imwrite(rp, right)
    print(f"     지금 보이는 화면을 저장했습니다 — 눈으로 확인하세요:")
    print(f"       {lp}")
    print(f"       {rp}")
    print("     두 장이 **서로 조금 어긋난 같은 장면**이어야 합니다.")
    print("     한 장면의 왼쪽/오른쪽 절반이라면 스테레오 카메라가 아닙니다.")

    print()
    print(f"── 3. 체스보드 (지금 설정: 내부 코너 {args.cols}x{args.rows})")
    asked = (args.cols, args.rows)
    hit_l = has_chessboard(gray_l, asked)
    hit_r = has_chessboard(gray_r, asked)

    if hit_l and hit_r:
        print(f"  ✅ 양쪽 눈에서 {asked[0]}x{asked[1]} 을 찾았습니다. 그대로 capture 하세요.")
    else:
        where = ("왼쪽에서만" if hit_l else "오른쪽에서만" if hit_r else "어느 쪽에서도")
        print(f"  ❌ {where} 못 찾았습니다.")
        problems += 1
        print("     다른 규격으로 다시 찾아봅니다...")
        found_l = find_matching_patterns(gray_l)
        found_r = find_matching_patterns(gray_r)
        common = [p for p in found_l if p in found_r]
        if common:
            c, r = common[0]
            print(f"  💡 양쪽에서 **{c}x{r}** 이 맞습니다. 이렇게 쓰세요:")
            print(f"       python3 stereo_calibrate.py capture --cols {c} --rows {r}")
            print(f"     (calibrate / verify 에도 같은 값을 주세요)")
        elif found_l or found_r:
            print(f"     왼쪽에서 맞는 규격: {found_l or '없음'}")
            print(f"     오른쪽에서 맞는 규격: {found_r or '없음'}")
            print("     한쪽에만 보입니다 — 체스보드를 가운데로 옮기거나 뒤로 물러나세요.")
        else:
            print("     흔한 규격 어느 것도 안 맞습니다. 아래를 확인하세요.")
            print("       - 체스보드가 **화면 안에 다 들어와** 있는지 (잘리면 못 찾습니다)")
            print("       - 조명이 충분하고 반사가 없는지")
            print("       - 판이 평평한지 (휘면 못 찾습니다)")
            print("       - 초점이 맞았는지")
            print(f"       - 위에 저장한 {os.path.basename(lp)} 을 직접 열어 보세요")

    print()
    print("=" * 60)
    if problems == 0:
        print(" ✅ 문제 없습니다. capture 를 실행하세요.")
        return 0
    print(f" ❌ 문제 {problems} 건 — 위 ❌ 항목을 먼저 해결하세요.")
    return 1


# ==========================================================================
# CLI
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="스테레오 카메라 캘리브레이션 (촬영 → 계산 → 검증)")
    sub = parser.add_subparsers(dest="action", required=True)

    def add_pattern(p):
        # 체스보드는 **내부 코너** 수로 셉니다. 10x7 칸 판이면 9x6 입니다.
        p.add_argument("--cols", type=int, default=9, help="가로 내부 코너 수")
        p.add_argument("--rows", type=int, default=6, help="세로 내부 코너 수")
        p.add_argument("--square", type=float, default=0.025,
                       help="한 칸의 실제 크기(m). 이 값이 틀리면 baseline 도 틀립니다")

    p_capture = sub.add_parser("capture", help="체스보드 영상쌍 모으기")
    add_pattern(p_capture)
    p_capture.add_argument("--device", type=int, default=0)
    p_capture.add_argument("--width", type=int, default=2560, help="좌+우 합친 폭")
    p_capture.add_argument("--height", type=int, default=720)
    p_capture.add_argument("--output", default=DEFAULT_CAPTURE_DIR)
    p_capture.add_argument("--count", type=int, default=20)
    p_capture.add_argument("--interval", type=float, default=1.5,
                           help="저장 최소 간격(초). 같은 자세가 연달아 저장되는 것을 막습니다")
    p_capture.add_argument("--min-pairs", type=int, default=10)
    p_capture.add_argument("--downscale", type=int, default=DETECT_DOWNSCALE,
                           help="탐색용 축소 배수. 1 이면 원본(느리지만 가장 민감)")
    p_capture.set_defaults(func=capture)

    p_calib = sub.add_parser("calibrate", help="촬영본으로 계산")
    add_pattern(p_calib)
    p_calib.add_argument("--input", default=DEFAULT_CAPTURE_DIR)
    p_calib.add_argument("--output", default=CALIBRATION_PATH)
    p_calib.add_argument("--min-pairs", type=int, default=10)
    p_calib.add_argument("--max-rms", type=float, default=1.0)
    p_calib.set_defaults(func=calibrate)

    p_verify = sub.add_parser("verify", help="정렬이 실제로 됐는지 확인")
    add_pattern(p_verify)
    p_verify.add_argument("--input", default=DEFAULT_CAPTURE_DIR)
    p_verify.add_argument("--calibration", default=CALIBRATION_PATH)
    p_verify.add_argument("--max-error", type=float, default=0.5,
                          help="허용할 평균 수직 어긋남(px)")
    p_verify.set_defaults(func=verify)

    p_doctor = sub.add_parser(
        "doctor", help="코너가 안 잡힐 때 원인 진단 (카메라/분할/규격)")
    add_pattern(p_doctor)
    p_doctor.add_argument("--device", type=int, default=0)
    p_doctor.add_argument("--width", type=int, default=2560, help="좌+우 합친 폭")
    p_doctor.add_argument("--height", type=int, default=720)
    p_doctor.add_argument("--output", default=DEFAULT_CAPTURE_DIR)
    p_doctor.set_defaults(func=doctor)

    p_report = sub.add_parser("report", help="저장된 값 보기")
    p_report.add_argument("--calibration", default=CALIBRATION_PATH)
    p_report.set_defaults(func=report)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
