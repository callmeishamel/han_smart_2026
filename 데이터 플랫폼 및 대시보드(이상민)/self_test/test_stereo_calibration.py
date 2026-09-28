r"""
self_test/test_stereo_calibration.py

젯슨 `stereo_calibrate.py` 의 계산·저장·적용 로직을 **카메라와 체스보드 없이**
검증합니다.

왜 필요한가
-----------
거리(`distance_meter`)는 `Z = f * B / d` 로 나오고, 그 `f` 는 미니맵 방향각
(`atan2(dx, f)`) 에도 그대로 쓰입니다. 예전에는 `f=500.0` / `B=0.06` 이 코드에
박혀 있었는데 둘 다 실측이 아니라 추측값이었습니다. `f` 가 k배 틀리면 **모든
거리가 k배 틀리고**, `common/schema.py` 의 1.5m / 3.0m 위험 기준이 그만큼 밀립니다.

여기서 보는 것은 실제 촬영이 필요 없는 부분입니다.

  - 저장/로드가 값을 그대로 보존하는가
  - 카메라가 다른 해상도를 줬을 때 내부 파라미터를 옳게 조정하는가,
    그리고 **가로세로 비가 다르면 거부하는가**(틀린 거리를 자신 있게 내놓느니
    거리를 포기하는 쪽이 안전합니다)
  - 정렬 결과에서 뽑는 f / B / 주점이 넣은 값과 맞는가
  - 캘리브레이션이 없을 때 죽지 않고 None 으로 물러나는가

실제 렌즈 왜곡 보정 품질은 체스보드 촬영본이 있어야 하므로
`python3 stereo_calibrate.py verify` 가 담당합니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_stereo_calibration.py
"""

import os
import sys
import tempfile

import cv2
import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_TARGET = os.path.join(_REPO_ROOT, "smart_factory_project", "stereo_calibrate.py")

# 합성 카메라 한 쌍. 실제 촬영본 대신 "정답을 아는" 값을 넣고, 계산이 그 값을
# 되돌려 주는지 봅니다.
TRUE_FOCAL_PX = 900.0
TRUE_BASELINE_M = 0.06
IMAGE_W, IMAGE_H = 1280, 720
PRINCIPAL_X, PRINCIPAL_Y = 640.0, 360.0


def check(desc, condition, detail=""):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(desc)


def load_module():
    if not os.path.exists(_TARGET):
        print(f"[중단] 검증 대상을 찾지 못했습니다:\n       {_TARGET}")
        raise SystemExit(1)
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("[건너뜀] opencv-python 이 없어 이 테스트는 돌릴 수 없습니다.")
        print("         pip install opencv-python")
        raise SystemExit(0)

    import importlib.util
    spec = importlib.util.spec_from_file_location("stereo_calibrate_test", _TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ideal_calibration(sc):
    """왜곡 없고 완벽히 평행한 이상적인 스테레오 쌍.

    T = [-B, 0, 0] 인 이유: stereoCalibrate 의 T 는 "카메라1 좌표계에서 본
    카메라2 원점" 이 아니라 `X2 = R*X1 + T` 의 T 입니다. 오른쪽 카메라가 +x 로
    B 만큼 떨어져 있으면, 카메라1 원점은 카메라2 기준으로 -B 에 있습니다.
    """
    K = np.array([[TRUE_FOCAL_PX, 0.0, PRINCIPAL_X],
                  [0.0, TRUE_FOCAL_PX, PRINCIPAL_Y],
                  [0.0, 0.0, 1.0]], dtype=np.float64)
    D = np.zeros((1, 5), dtype=np.float64)
    return sc.StereoCalibration(K, D, K.copy(), D.copy(),
                                np.eye(3), np.array([-TRUE_BASELINE_M, 0.0, 0.0]),
                                (IMAGE_W, IMAGE_H), rms=0.21)


def test_roundtrip(sc):
    print("=== 저장 / 로드 ===")
    original = ideal_calibration(sc)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "calib.npz")
        original.save(path)
        loaded = sc.StereoCalibration.load(path)

        check("파일에서 다시 읽힌다", loaded is not None)
        check("내부 파라미터가 보존된다", np.allclose(loaded.K1, original.K1))
        check("두 카메라 관계(T)가 보존된다", np.allclose(loaded.T, original.T))
        check("촬영 해상도가 보존된다", loaded.image_size == (IMAGE_W, IMAGE_H))
        check("RMS 가 보존된다", abs(loaded.rms - 0.21) < 1e-9)

        # 파일이 없거나 깨졌을 때 예외를 던지면 탐지 전체가 죽습니다.
        check("없는 파일은 None (예외 아님)",
              sc.StereoCalibration.load(os.path.join(tmp, "없음.npz")) is None)
        broken = os.path.join(tmp, "broken.npz")
        with open(broken, "wb") as f:
            f.write(b"not an npz file")
        check("깨진 파일도 None (예외 아님)", sc.StereoCalibration.load(broken) is None)
    print()


def test_derived_values(sc):
    print("=== 정렬 결과에서 f / B / 주점을 되찾는가 ===")
    rectifier = sc.StereoRectifier(ideal_calibration(sc))

    check("초점거리 f 를 되찾는다",
          abs(rectifier.focal_px - TRUE_FOCAL_PX) < 1.0,
          f"{rectifier.focal_px} vs {TRUE_FOCAL_PX}")
    check("렌즈 간격 B 를 되찾는다 (하드코딩 0.06 이 아니라 실측)",
          abs(rectifier.baseline_m - TRUE_BASELINE_M) < 1e-4,
          f"{rectifier.baseline_m} vs {TRUE_BASELINE_M}")
    check("주점 cx 를 되찾는다",
          abs(rectifier.principal_x - PRINCIPAL_X) < 1.0,
          f"{rectifier.principal_x} vs {PRINCIPAL_X}")

    # 거리 공식이 실제로 맞는 값을 내는지. 시차 27px 이면 2.0m 여야 합니다.
    disparity = TRUE_FOCAL_PX * TRUE_BASELINE_M / 2.0
    distance = rectifier.focal_px * rectifier.baseline_m / disparity
    check("시차 -> 거리 계산이 맞는다 (2.0m)", abs(distance - 2.0) < 0.01,
          f"{distance:.3f}m")

    # 예전 상수(500 / 0.06)로 같은 시차를 풀면 얼마나 틀리는지 — 이게 이 작업의 이유.
    wrong = 500.0 * 0.06 / disparity
    check("옛 추측값이었다면 같은 시차가 다른 거리로 읽혔다",
          abs(wrong - 2.0) > 0.5, f"{wrong:.2f}m")
    print(f"       (참고: 같은 장면을 f=500 으로 풀면 {wrong:.2f}m — "
          f"schema.py 의 1.5m 위험 기준이 그만큼 밀립니다)")
    print()


def test_resolution_scaling(sc):
    print("=== 카메라가 다른 해상도를 줬을 때 ===")
    original = ideal_calibration(sc)

    same = original.scaled_to(IMAGE_W, IMAGE_H)
    check("같은 해상도면 그대로 쓴다", same is original)

    half = original.scaled_to(IMAGE_W // 2, IMAGE_H // 2)
    check("절반 해상도로 조정된다", half is not None)
    check("초점거리도 절반", abs(half.K1[0, 0] - TRUE_FOCAL_PX / 2) < 1e-6,
          str(half.K1[0, 0]))
    check("주점도 절반", abs(half.K1[0, 2] - PRINCIPAL_X / 2) < 1e-6,
          str(half.K1[0, 2]))
    check("왜곡계수는 해상도와 무관하므로 그대로", np.allclose(half.D1, original.D1))
    check("두 카메라 관계(T)도 그대로", np.allclose(half.T, original.T))

    half_rect = sc.StereoRectifier(half)
    check("절반 해상도에서도 B 는 같은 물리값",
          abs(half_rect.baseline_m - TRUE_BASELINE_M) < 1e-4,
          f"{half_rect.baseline_m}")

    # 가로세로 비가 달라졌다면 리사이즈가 아니라 크롭이라, 비례 조정이 성립하지
    # 않습니다. 틀린 거리를 자신 있게 내놓느니 거리를 포기하는 쪽이 안전합니다.
    cropped = original.scaled_to(640, 720)
    check("가로세로 비가 다르면 거부한다 (None)", cropped is None)
    check("잘못된 크기도 거부한다", original.scaled_to(0, 0) is None)
    print()


def test_rectify_pair(sc):
    print("=== 정렬 적용 ===")
    rectifier = sc.StereoRectifier(ideal_calibration(sc))

    left = np.zeros((IMAGE_H, IMAGE_W), dtype=np.uint8)
    right = np.zeros((IMAGE_H, IMAGE_W), dtype=np.uint8)
    left[300:320, 600:640] = 255
    right[300:320, 560:600] = 255

    out_l, out_r = rectifier.rectify_pair(left, right)
    check("좌우 모두 같은 크기로 나온다",
          out_l.shape == left.shape and out_r.shape == right.shape)

    # 왜곡이 없고 두 카메라가 이미 평행하므로, 이상적인 경우 정렬은 거의
    # 항등변환이어야 합니다. (실제 카메라에서는 여기서 영상이 휘어집니다.)
    check("왜곡 없는 이상적 쌍에서는 거의 그대로 통과한다",
          np.abs(out_l.astype(int) - left.astype(int)).mean() < 1.0)

    color = np.zeros((IMAGE_H, IMAGE_W, 3), dtype=np.uint8)
    c_l, c_r = rectifier.rectify_pair(color, color)
    check("컬러 영상도 처리된다 (스트리밍용)", c_l.shape == color.shape)
    print()


def test_load_rectifier_fallback(sc):
    print("=== 캘리브레이션이 없을 때 물러나는가 ===")
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "없음.npz")
        check("파일이 없으면 None — 호출부가 추측값으로 돈다",
              sc.load_rectifier(IMAGE_W, IMAGE_H, path=missing) is None)

        path = os.path.join(tmp, "calib.npz")
        ideal_calibration(sc).save(path)
        check("파일이 있으면 rectifier 를 만든다",
              sc.load_rectifier(IMAGE_W, IMAGE_H, path=path) is not None)
        check("해상도 비가 안 맞으면 None (틀린 거리보다 거리 없음이 안전)",
              sc.load_rectifier(640, 720, path=path) is None)
    print()


def test_sender_wiring():
    """송신기가 실제로 이 모듈을 쓰도록 배선돼 있는지 소스에서 확인합니다.

    모듈만 맞고 배선이 빠지면 아무것도 달라지지 않는데, 그 상태로도 이 파일의
    다른 테스트는 전부 통과합니다.
    """
    print("=== ai_inference_sender.py 배선 ===")
    sender_path = os.path.join(_REPO_ROOT, "smart_factory_project",
                               "ai_inference_sender.py")
    with open(sender_path, encoding="utf-8") as f:
        code = f.read()

    check("load_rectifier 를 import 한다", "from stereo_calibrate import load_rectifier" in code)
    check("sender도 모노 프레임을 거부한다",
          "from stereo_calibrate import check_stereo_frame" in code
          and "stereo_problem = check_stereo_frame(probe_w, probe_h)" in code)
    check("import 실패를 견딘다 (손으로 복사해 붙여넣는 배포 대비)",
          "except ImportError" in code and "load_rectifier = None" in code)
    check("캡처 단계에서 정렬한다 (하류 전체가 같은 좌표계를 보도록)",
          "rectifier.rectify_pair(" in code)
    check("f 를 캘리브레이션에서 가져온다", "rectifier.focal_px" in code)
    check("B 를 캘리브레이션에서 가져온다", "rectifier.baseline_m" in code)
    check("방향각 기준이 화면 중심이 아니라 주점이다",
          "box_center_x - principal_x" in code and "frame_center_x" not in code)
    # 두 파일이 **같은 카메라**를 엽니다. 방식이 갈리면 "센더는 되는데
    # 캘리브레이션만 안 되는" 상황이 생깁니다 — 실제로 그랬습니다. 젯슨에서
    # 백엔드를 안 주면 OpenCV 가 GStreamer 를 골라 0x0 으로 죽습니다.
    check("**센더가 V4L2 백엔드를 명시한다**", "cv2.CAP_V4L2" in code)
    sc_path = os.path.join(_REPO_ROOT, "smart_factory_project", "stereo_calibrate.py")
    with open(sc_path, encoding="utf-8") as f:
        sc_code = f.read()
    check("**stereo_calibrate 도 같은 백엔드를 쓴다**", "cv2.CAP_V4L2" in sc_code)
    check("  카메라 여는 곳이 한 군데로 모여 있다",
          sc_code.count("cv2.VideoCapture(") == 2, str(sc_code.count("cv2.VideoCapture(")))
    check("  0x0(스트림 없음)을 잡아낸다", "스트림이 없습니다" in sc_code)
    check("  드라이버 보고값 외에 실제 첫 프레임도 검사한다",
          "check_stereo_image(first_frame)" in sc_code)
    check("  v4l2-ctl 이 없을 수 있다고 알려준다", "v4l-utils" in sc_code)
    quality_guard = sc_code.index("if not is_rms_acceptable(rms, args.max_rms)")
    save_result = sc_code.index("calibration.save(args.output)")
    check("RMS 불합격 결과는 저장하기 전에 거부한다", quality_guard < save_result)

    check("캘리브레이션이 없으면 경고를 낸다",
          "스테레오 캘리브레이션이 없습니다" in code)
    check("sender가 확인한 캘리브레이션 경로를 알려준다",
          "확인한 파일 경로" in code and "CALIBRATION_PATH" in code)

    # 젯슨 폴더 사본이 갈라지면 실제 장비에는 반영되지 않습니다.
    mirror = os.path.join(_REPO_ROOT, "젯슨 연결용 프로그램(손준영)",
                          "ai_inference_sender.py")
    with open(mirror, encoding="utf-8") as f:
        check("젯슨 배포 폴더 사본이 같다", f.read() == code)
    mirror_sc = os.path.join(_REPO_ROOT, "젯슨 연결용 프로그램(손준영)",
                             "stereo_calibrate.py")
    check("stereo_calibrate.py 도 젯슨 폴더에 있다", os.path.exists(mirror_sc))
    print()


def _synthetic_board(cols=9, rows=6, square_px=60, pad=80, width=1280, height=720):
    """체스보드가 그려진 회색 영상. 내부 코너가 cols x rows 가 되도록 그립니다."""
    img = np.full((height, width), 220, np.uint8)
    for r in range(rows + 1):
        for c in range(cols + 1):
            if (r + c) % 2 == 0:
                y0, x0 = pad + r * square_px, pad + c * square_px
                img[y0:y0 + square_px, x0:x0 + square_px] = 30
    return img


def test_capture_diagnostics(sc):
    """capture 가 조용히 멈춘 것처럼 보이던 문제를 막는 장치들.

    예전 capture 는 코너를 못 찾으면 아무 출력 없이 continue 만 했습니다.
    미탐지가 프레임당 최대 수 초라(장면에 따라) 루프가 기어가는데 화면은
    조용해서, "멈췄다"와 "체스보드를 못 알아본다"를 구분할 수 없었습니다.
    """
    print("=== 촬영 진단 ===")

    # --- 좌우가 나란히 붙은 스테레오인지 ---
    # 카메라가 2560x720 을 거부하고 1280x720(모노)을 주면, 한 장을 반으로 쪼개서
    # 오른쪽 눈에 체스보드가 절반만 들어갑니다 -> 영원히 못 찾습니다.
    check("스테레오 해상도는 통과", sc.check_stereo_frame(2560, 720) == "")
    check("  1280x480 도 스테레오", sc.check_stereo_frame(1280, 480) == "")
    check("**모노 1280x720 은 걸러낸다**", sc.check_stereo_frame(1280, 720) != "")
    check("  640x480 도 걸러낸다", sc.check_stereo_frame(640, 480) != "")
    check("  높이 0 도 걸러낸다", sc.check_stereo_frame(100, 0) != "")
    check("  폭이 홀수인 프레임도 걸러낸다", sc.check_stereo_frame(2559, 720) != "")
    check("실제 스테레오 프레임 배열은 통과",
          sc.check_stereo_image(np.zeros((720, 2560, 3), np.uint8)) == "")
    check("실제 단안 프레임 배열은 거부",
          sc.check_stereo_image(np.zeros((720, 1280, 3), np.uint8)) != "")
    check("비어 있는 실제 프레임도 거부", sc.check_stereo_image(None) != "")

    check("RMS 경계값은 승인", sc.is_rms_acceptable(1.0, 1.0))
    check("RMS 초과 결과는 거부", not sc.is_rms_acceptable(1.01, 1.0))
    check("NaN RMS 결과는 거부", not sc.is_rms_acceptable(float("nan"), 1.0))

    class FakeCapture:
        def __init__(self):
            self.released = False

        def read(self):
            # 드라이버 보고값은 2560x720이지만 실제 프레임은 1280x720인 상황.
            return True, np.zeros((720, 1280, 3), np.uint8)

        def release(self):
            self.released = True

    fake_cap = FakeCapture()
    original_open_camera = sc.open_camera
    try:
        sc.open_camera = lambda *_args: (fake_cap, 2560, 720)
        with tempfile.TemporaryDirectory() as tmp:
            args = type("CaptureArgs", (), {
                "device": 0, "width": 2560, "height": 720, "output": tmp,
                "cols": 9, "rows": 6, "square": 0.025,
            })()
            check("보고값이 정상이어도 실제 단안 프레임이면 capture 중단",
                  sc.capture(args) == 1)
            check("중단할 때 카메라를 해제", fake_cap.released)
    finally:
        sc.open_camera = original_open_camera

    # --- 보이는 것을 말로 설명해 주는가 ---
    none_seen = {"both": 0, "left": 0, "right": 0, "none": 12}
    lines = " ".join(sc._describe_capture_status(0, 20, 12, none_seen, (9, 6)))
    check("한 장도 못 찾으면 규격을 의심하라고 알려준다", "9x6" in lines and "칸" in lines)
    check("  doctor 를 안내한다", "doctor" in lines)

    left_only = {"both": 1, "left": 9, "right": 0, "none": 2}
    lines = " ".join(sc._describe_capture_status(0, 20, 12, left_only, (9, 6)))
    check("한쪽 눈에만 보이면 어느 쪽으로 옮길지 알려준다", "오른쪽" in lines)

    ok_seen = {"both": 8, "left": 1, "right": 1, "none": 2}
    lines = sc._describe_capture_status(3, 20, 12, ok_seen, (9, 6))
    check("잘 되고 있으면 잔소리하지 않는다", len(lines) == 1, str(len(lines)))
    check("  그래도 진행 상황은 늘 보인다", "3/20" in lines[0])
    print()


def test_chessboard_detection(sc):
    """축소 탐색이 보드를 놓치지 않는지 — 속도를 위해 정확도를 잃으면 안 됩니다."""
    print("=== 체스보드 탐지 ===")
    board = _synthetic_board(9, 6)
    blank = np.full((720, 1280), 200, np.uint8)

    for downscale in (1, 2, 3):
        check(f"downscale={downscale} 에서 9x6 판을 찾는다",
              sc.has_chessboard(board, (9, 6), downscale))
        check(f"  downscale={downscale} 에서 빈 화면은 안 찾는다",
              not sc.has_chessboard(blank, (9, 6), downscale))

    # capture 는 좌표를 안 쓰고 저장 여부만 정하므로 축소본으로 봐도 됩니다.
    # 정확한 코너는 calibrate 가 저장된 원본에서 다시 구합니다.
    check("원본에서는 좌표까지 정확히 구한다",
          sc._find_corners(board, (9, 6)) is not None)

    # 아주 작은 영상에서는 축소를 건너뛰어야 합니다(줄이면 못 찾습니다).
    small = cv2.resize(board, (200, 112))
    check("작은 영상에서는 축소를 건너뛴다", sc.has_chessboard(small, (9, 6), 2))

    # 규격이 틀리면 못 찾아야 정상입니다 — doctor 가 이걸로 맞는 규격을 찾습니다.
    check("틀린 규격(8x6)으로는 9x6 판을 못 찾는다",
          not sc.has_chessboard(board, (8, 6)))

    hits = sc.find_matching_patterns(board)
    check("**맞는 규격을 스스로 찾아낸다**", (9, 6) in hits, str(hits))
    check("  가장 먼저 제안하는 것이 9x6", hits[0] == (9, 6), str(hits))
    check("보드가 없으면 아무 규격도 안 맞는다",
          sc.find_matching_patterns(blank) == [])
    print()


def main():
    sc = load_module()
    print("검증 대상: smart_factory_project/stereo_calibrate.py\n")

    test_roundtrip(sc)
    test_derived_values(sc)
    test_resolution_scaling(sc)
    test_rectify_pair(sc)
    test_load_rectifier_fallback(sc)
    test_capture_diagnostics(sc)
    test_chessboard_detection(sc)
    test_sender_wiring()

    print("모든 테스트 통과!")


if __name__ == "__main__":
    main()
