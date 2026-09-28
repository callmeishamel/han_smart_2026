r"""
self_test/test_jetson_sender.py

젯슨 비전 송신기(`ai_inference_sender.py`)를 젯슨 없이 검증합니다.

이 스크립트는 저장소에서 유일하게 **테스트가 하나도 없던 코드**였습니다.
NanoOWL / torch / PIL 이 Jetson L4T 컨테이너에만 있어서 다른 PC에서는 import
자체가 안 되기 때문입니다. 그래서 그 두 개만 스텁으로 꽂고, 나머지(HTTP 서버,
상태 집계, 목적지 계산)는 실제 코드 그대로 돌립니다.
cv2 와 numpy 는 requirements-vision.txt 에 있는 일반 pip 패키지라 그대로 씁니다.

검사하는 것
-----------
1. `/health` 가 "살아 있음"과 "멈춤"을 구분하는가
   — 영상만으로는 구분되지 않습니다. 카메라가 빠져도 마지막 프레임이 계속
     재전송되므로 화면은 멀쩡해 보입니다.
2. 목적지 IP 세 개가 각각 다른 환경변수에서 오는가
   — 예전에는 9999(젯슨 호스트)와 9998(이상민님 PC)이 HOST_IP 하나를 함께 써서
     둘 중 하나는 반드시 못 받았습니다.
3. depth 컬러맵 스트림이 제거되었는가
   — 관제에 필요한 것은 카메라 영상과 미니맵뿐입니다. 단, 거리 계산은 남아야 합니다.
4. 대기 화면 JPEG 캐시가 매 프레임 새로 그리지 않는가

실행 방법 (프로젝트 루트에서):
    python self_test\test_jetson_sender.py
"""

import importlib.util
import json
import os
import sys
import threading
import time
import types
import urllib.request

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()
_SENDER = os.path.join(_REPO_ROOT, "젯슨 연결용 프로그램(손준영)", "ai_inference_sender.py")


def check(desc, condition, detail=""):
    print(f"[{'OK ' if condition else 'FAIL'}] {desc}")
    if not condition:
        raise AssertionError(f"{desc}{(' — ' + detail) if detail else ''}")


def load_sender():
    """젯슨 전용 의존성만 스텁으로 꽂고 실제 파일을 로드합니다."""
    for name in ("nanoowl", "nanoowl.owl_predictor", "PIL"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nanoowl.owl_predictor"].OwlPredictor = object
    sys.modules["PIL"].Image = types.SimpleNamespace(fromarray=lambda a: a)

    spec = importlib.util.spec_from_file_location("ai_inference_sender", _SENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    if not os.path.exists(_SENDER):
        print(f"[건너뜀] 송신기를 찾지 못했습니다: {_SENDER}")
        print("         저장소를 통째로 받아야 이 테스트가 돕니다.")
        return

    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        print(f"[건너뜀] cv2/numpy 가 필요합니다 ({exc}).")
        print("         pip install -r '젯슨 연결용 프로그램(손준영)/requirements-vision.txt'")
        return

    m = load_sender()

    print("=== /health — 살아있음과 멈춤을 구분하는가 ===")
    srv = m.ThreadedHTTPServer(("127.0.0.1", 0), m.DualStreamingHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def health():
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return json.load(r)

    try:
        h = health()
        check("추론이 아직 안 돌았으면 ok=False", h["ok"] is False)
        check("  프레임이 없으면 frame_age_sec 은 None", h["frame_age_sec"] is None)

        with m.health_lock:
            m.health_state["frames"] = 42
            m.health_state["last_frame_at"] = time.time()
            m.health_state["last_detection_count"] = 2
        h = health()
        check("프레임이 갱신되면 ok=True", h["ok"] is True)
        check("  프레임 수와 탐지 수를 그대로 보고",
              h["frames"] == 42 and h["last_detection_count"] == 2)

        with m.health_lock:
            m.health_state["last_frame_at"] = time.time() - 6
        h = health()
        check("6초간 프레임이 없으면 ok=False (영상은 멀쩡해 보여도)", h["ok"] is False)
        check("  얼마나 멈췄는지 알려줌", h["frame_age_sec"] > 5)

        with m.health_lock:
            m.health_state["camera_errors"] = 7
            m.health_state["udp_errors"] = 3
        h = health()
        check("카메라/UDP 실패 횟수도 노출",
              h["camera_errors"] == 7 and h["udp_errors"] == 3)

        m.network_targets = {
            "ros2_bridge": "127.0.0.1:9999",
            "dashboard_rag": "203.0.113.41:9998",
            "minimap": "203.0.113.41:9091",
        }
        h = health()
        check("Docker 환경변수 누락을 찾도록 실제 UDP 목적지를 노출",
              h["udp_targets"]["dashboard_rag"] == "203.0.113.41:9998"
              and h["udp_targets"]["minimap"] == "203.0.113.41:9091")
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n=== 목적지 IP 는 세 개가 각각 따로 ===")
    # 9999 는 젯슨 호스트, 9998 은 이상민님 PC, 9091 은 이다은님 노트북입니다.
    # 예전에는 앞의 둘이 HOST_IP 하나를 함께 써서 동시에 맞출 값이 없었습니다.
    import re
    src = open(_SENDER, encoding="utf-8").read()
    job = re.search(r"send_jobs\s*=\s*\[(.*?)\]", src, re.S)
    check("전송 목록을 찾음", job is not None)
    body = job.group(1)
    check("ROS2 목적지와 대시보드 목적지가 다른 변수",
          "ros2_ip" in body and "dashboard_ip" in body)
    check("두 목적지에 같은 변수를 쓰지 않음", "server_ip" not in body)

    print("\n=== 컨테이너 감지 (Docker 루프백 경고용) ===")
    check("_in_container() 가 예외 없이 bool 을 돌려줌",
          isinstance(m._in_container(), bool))

    print("\n=== depth 스트림이 제거되었는가 ===")
    # 관제 화면에 필요한 것은 카메라 영상과 SLAM 미니맵뿐입니다. 시차 컬러맵은
    # 사람이 해석하기 어렵고 대역폭만 먹어서 걷어냈습니다. 다만 거리 계산
    # (depth_map -> distance_meter)은 그대로 남아 있어야 합니다.
    check("컬러맵 생성이 없음", "applyColorMap" not in src)
    check("depth 프레임 전역이 없음", "output_frame_depth" not in src)
    check("거리 계산은 그대로 남아 있음",
          "depth_map" in src and "distance_meter" in src)

    print("\n=== 대기 화면 JPEG 캐시 ===")
    a = m.get_placeholder_jpeg()
    b = m.get_placeholder_jpeg()
    check("같은 객체를 재사용 (매 프레임 새로 그리지 않음)", a is b)
    check("빈 바이트가 아님", len(a) > 0)
    print("\n=== 스트리밍 인코딩 캐시 — 클라이언트가 늘어도 부하가 안 늘어야 함 ===")
    # 예전에는 클라이언트마다 draw_overlays(2.7MB 복사) + imencode 를 따로 했습니다.
    # 대시보드 iframe 과 브라우저 탭을 같이 열면 비용이 두 배가 되고, 결국 모두에게
    # 화면이 느려집니다. 같은 (프레임, 박스) 조합이면 한 번만 만들어 나눠 씁니다.
    import numpy as _np

    m.latest_video_frame = _np.zeros((72, 128, 3), dtype=_np.uint8)
    m.frame_seq = 1
    m.latest_overlays = []
    m.overlay_seq = 1
    m.perf_state["encode_reuse"] = 0
    m.perf_state["encode_miss"] = 0

    key1, first = m.get_stream_jpeg()
    check("JPEG 을 만든다", len(first) > 0)
    check("  프레임 식별자를 함께 준다 (중복 전송 판별용)", key1 == (1, 1), str(key1))
    check("  실제 인코딩 1회", m.perf_state["encode_miss"] == 1,
          str(m.perf_state["encode_miss"]))

    key2, second = m.get_stream_jpeg()
    check("같은 프레임이면 같은 바이트를 돌려준다", second is first)
    check("  식별자도 같다 — 호출부가 재전송을 건너뛴다", key2 == key1)
    check("  두 번째는 캐시 재사용 (인코딩 안 함)",
          m.perf_state["encode_miss"] == 1 and m.perf_state["encode_reuse"] == 1,
          f"miss={m.perf_state['encode_miss']} reuse={m.perf_state['encode_reuse']}")

    m.frame_seq = 2
    key3, _third = m.get_stream_jpeg()
    check("새 프레임이면 다시 인코딩한다", m.perf_state["encode_miss"] == 2)
    check("  식별자가 바뀐다", key3 != key1 and key3 == (2, 1), str(key3))

    m.overlay_seq = 2
    key4, _fourth = m.get_stream_jpeg()
    check("박스만 바뀌어도 다시 인코딩한다", m.perf_state["encode_miss"] == 3)
    check("  식별자가 바뀐다", key4 == (2, 2), str(key4))

    m.latest_video_frame = None
    key_none, placeholder = m.get_stream_jpeg()
    check("프레임이 없으면 대기 화면", len(placeholder) > 0)
    check("  대기 화면 식별자는 (-1, -1)", key_none == (-1, -1), str(key_none))
    m.latest_video_frame = _np.zeros((72, 128, 3), dtype=_np.uint8)

    check("인코딩 시간을 잰다", m.perf_state["encode_ms"] >= 0.0)
    check("프레임 크기를 기록한다", m.perf_state["jpeg_bytes"] > 0)

    print("\n=== 스트리밍 페이싱 — 목표 FPS 를 실제로 지키는가 ===")
    # 예전에는 인코딩이 끝난 뒤 무조건 33ms 를 더 쉬어서, 인코딩이 20ms 면 실제
    # 주기가 53ms(약 19fps)였습니다. "30fps 로 맞춰 놨는데 왜 느리지" 의 답입니다.
    src_text = open(_SENDER, encoding="utf-8").read()
    check("경과 시간을 빼고 남은 만큼만 잔다",
          "STREAM_FRAME_INTERVAL - elapsed" in src_text)
    check("  고정 sleep(0.033) 이 남아 있지 않다", "time.sleep(0.033)" not in src_text)
    check("목표 FPS 를 환경변수로 조절할 수 있다", "STREAM_FPS" in src_text)
    check("같은 프레임을 두 번 보내지 않는다 (대역폭 낭비 방지)",
          "key != last_key" in src_text)
    check("  다만 너무 오래 침묵하면 재전송한다 (연결 유지)",
          "STREAM_KEEPALIVE_SEC" in src_text)
    check("JPEG 품질을 환경변수로 조절할 수 있다", "JPEG_QUALITY" in src_text)

    print("\n=== 카메라 속도 상한 ===")
    check("카메라에 FPS 를 요청한다 (안 하면 드라이버가 제멋대로 협상)",
          "CAP_PROP_FPS" in src_text)
    check("드라이버 버퍼를 줄인다 (깊으면 낡은 프레임이 나온다)",
          "CAP_PROP_BUFFERSIZE" in src_text)
    check("카메라가 요청 FPS 를 거부하면 경고한다",
          "요청 FPS 를 거부" in src_text)

    print("\n=== /health 가 병목을 가르는 값을 내는가 ===")
    for key in ("capture_fps", "stream_fps", "read_ms", "rectify_ms", "encode_ms",
                "jpeg_kb", "encode_reuse", "stream_clients"):
        check(f"  {key}", f'"{key}"' in src_text)

    print("\n=== 시차 계산을 필요할 때만 하는가 (추론 루프 최대 비용) ===")
    # StereoSGBM 은 1280x720 에서 이 PC 기준 ~148ms, 젯슨은 그 3~5배입니다.
    # 그런데 결과는 오직 "박스 안의 거리" 하나에만 쓰입니다. 박스가 없으면
    # 계산할 이유가 없는데 예전에는 매 프레임 돌렸습니다.
    check("탐지가 없으면 시차 계산을 건너뛴다",
          "if len(boxes) > 0:" in src_text and "stereo_skipped" in src_text)
    check("  건너뛴 횟수를 세어 /health 로 보여준다",
          '"stereo_skipped"' in src_text and '"stereo_ran"' in src_text)
    check("  시차 계산 비용도 잰다", '"stereo_ms"' in src_text)

    # 문자열이 주석에 남는 것까지 잡으면 과하므로, **전체 프레임 depth_map 을
    # 만드는 코드 자체**가 없는지를 봅니다.
    check("전체 프레임 depth_map 을 만들지 않는다",
          "np.full_like(disparity" not in src_text
          and "depth_map[valid_disparity]" not in src_text)
    check("  원시 시차(int16)를 그대로 두고 박스에서만 실수 변환",
          "disparity[ymin:ymax, xmin:xmax].astype" in src_text)
    check("  시차가 없으면 거리 -1 (거리 미상)",
          "distance = -1.0" in src_text)


    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()
