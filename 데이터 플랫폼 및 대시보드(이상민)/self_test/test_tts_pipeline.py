r"""
self_test/test_tts_pipeline.py

TTS 송수신 검증을 이 묶음에서도 돌리기 위한 **얇은 다리**입니다.

왜 이 파일이 있나
-----------------
진짜 테스트는 저장소 최상위 `TTS Engine and pipeline/test_tts_pipeline.py` 에
있습니다. 잘 만들어진 테스트인데(실제 소켓 왕복으로 검증) 그 폴더에만 있어서,
`self_test\test_*.py` 를 한 번에 돌리는 평소 흐름에서 **빠져 있었습니다.**
아무도 안 돌리는 테스트는 없는 것과 같습니다.

사본을 만들지 않고 원본을 불러 씁니다 — 사본이 갈라지면 검증이 조용히
무의미해집니다(미니맵 렌더러 사본에서 실제로 겪은 일입니다).

실행 방법 (프로젝트 루트에서):
    python self_test\test_tts_pipeline.py
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_TTS_DIR = os.environ.get("TTS_MODULE_DIR") or os.path.join(
    _REPO_ROOT, "TTS Engine and pipeline")
_TARGET = os.path.join(_TTS_DIR, "test_tts_pipeline.py")


def main():
    if not os.path.exists(_TARGET):
        print("[중단] TTS 테스트 원본을 찾지 못했습니다:")
        print(f"       {_TARGET}")
        print("       저장소를 통째로 받았는지, TTS_MODULE_DIR 이 맞는지 확인하세요.")
        raise SystemExit(1)

    # 원본은 자기 폴더의 Rag_to_Jetson / Rx_pipeline 을 import 합니다.
    if _TTS_DIR not in sys.path:
        sys.path.insert(0, _TTS_DIR)

    print(f"검증 대상: TTS Engine and pipeline/ (원본 그대로)\n")

    import importlib.util
    spec = importlib.util.spec_from_file_location("tts_pipeline_test", _TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()


if __name__ == "__main__":
    main()
