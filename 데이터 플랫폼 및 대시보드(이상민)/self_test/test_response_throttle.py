r"""
self_test/test_response_throttle.py

common/schema.py의 ResponsePlanThrottle(대응안 중복 생성 억제기)이 의도대로
동작하는지 확인하는 자체 테스트.

- ROS2, Jetson, PostgreSQL, 인터넷 연결 전부 필요 없습니다.
- 시간도 실제로 흐르게 두지 않고 가짜 시계를 주입해서 검사하므로 즉시 끝납니다.

실행 방법 (프로젝트 루트에서):
    python self_test\test_response_throttle.py

모든 줄에 "OK"가 뜨면 통과입니다. AssertionError가 나면 그 줄이 실패한 겁니다.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 기본 콘솔(cp949)에서 이모지/em대시를 출력하다 죽는 것을 막습니다.
from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from common.schema import (
    RESPONSE_PLAN_SUPPRESS_SECONDS,
    DetectionEvent,
    ResponsePlanThrottle,
    risk_rank,
)


def check(desc, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {desc}")
    if not condition:
        raise AssertionError(desc)


class FakeClock:
    """time.monotonic() 대신 주입할 가짜 시계. 테스트가 실제로 기다리지 않도록."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def event(zone="A", obj="안전모 미착용", risk="위험"):
    """테스트용 DetectionEvent 한 건. 억제 판단에 쓰이는 필드만 의미가 있습니다."""
    return DetectionEvent(
        zone=zone,
        detected_object=obj,
        distance=1.0,
        box_position="x:0,y:0",
        risk_level=risk,
        issue="테스트",
    )


def main():
    print("=== 기본 상수 ===")
    check("기본 억제 간격은 30초", RESPONSE_PLAN_SUPPRESS_SECONDS == 30.0)

    print("\n=== risk_rank() ===")
    check("위험 > 주의 > 정상 순서", risk_rank("위험") > risk_rank("주의") > risk_rank("정상"))
    check("모르는 등급은 가장 낮게 취급", risk_rank("듣보등급") == 0)

    print("\n=== 억제 창 안/밖 ===")
    clock = FakeClock()
    t = ResponsePlanThrottle(30.0, time_func=clock)

    check("첫 이벤트는 통과", t.should_generate(event()) is True)
    check("곧바로 들어온 같은 조합은 억제", t.should_generate(event()) is False)

    clock.advance(29.9)
    check("29.9초 뒤에도 아직 억제", t.should_generate(event()) is False)

    clock.advance(0.2)  # 총 30.1초
    check("30초를 넘기면 다시 통과", t.should_generate(event()) is True)

    print("\n=== 억제된 이벤트가 창을 밀지 않는지 ===")
    # 억제될 때마다 기준 시각을 갱신하면, 계속 감지되는 물체는 영영 다음
    # 대응안이 나오지 않게 됩니다. "생성 후 30초"가 유지돼야 합니다.
    clock = FakeClock()
    t = ResponsePlanThrottle(30.0, time_func=clock)
    passes = 1 if t.should_generate(event()) else 0   # t=0 에서 생성
    for _ in range(60):
        clock.advance(1.0)                            # 1초 간격으로 60초간 계속 감지
        if t.should_generate(event()):
            passes += 1
    check("60초 연속 감지 중 통과는 t=0/30/60 세 번뿐", passes == 3)

    print("\n=== 구역/객체가 다르면 별개로 관리 ===")
    clock = FakeClock()
    t = ResponsePlanThrottle(30.0, time_func=clock)
    check("A구역 안전모 첫 건 통과", t.should_generate(event(zone="A")) is True)
    check("B구역 안전모는 별개 -> 통과", t.should_generate(event(zone="B")) is True)
    check("A구역 화재는 별개 -> 통과", t.should_generate(event(zone="A", obj="화재")) is True)
    check("A구역 안전모 재시도는 억제", t.should_generate(event(zone="A")) is False)

    print("\n=== 위험도 상향은 억제 창을 무시 ===")
    clock = FakeClock()
    t = ResponsePlanThrottle(30.0, time_func=clock)
    check("주의 첫 건 통과", t.should_generate(event(obj="차량·중장비", risk="주의")) is True)
    clock.advance(1.0)
    check("같은 주의는 억제", t.should_generate(event(obj="차량·중장비", risk="주의")) is False)
    clock.advance(1.0)
    check("주의 -> 위험 상향은 즉시 통과",
          t.should_generate(event(obj="차량·중장비", risk="위험")) is True)
    clock.advance(1.0)
    check("상향 직후 같은 위험은 다시 억제",
          t.should_generate(event(obj="차량·중장비", risk="위험")) is False)
    clock.advance(1.0)
    check("위험 -> 주의 하향은 통과시키지 않음",
          t.should_generate(event(obj="차량·중장비", risk="주의")) is False)

    print("\n=== 억제 끄기 (0초) ===")
    clock = FakeClock()
    t = ResponsePlanThrottle(0.0, time_func=clock)
    check("0초면 첫 건 통과", t.should_generate(event()) is True)
    check("0초면 연속 이벤트도 전부 통과", t.should_generate(event()) is True)

    print("\n=== reset() ===")
    clock = FakeClock()
    t = ResponsePlanThrottle(30.0, time_func=clock)
    t.should_generate(event())
    check("reset 전에는 억제 상태", t.should_generate(event()) is False)
    t.reset()
    check("reset 후에는 다시 통과", t.should_generate(event()) is True)

    print("\n모든 테스트 통과!")


if __name__ == "__main__":
    main()