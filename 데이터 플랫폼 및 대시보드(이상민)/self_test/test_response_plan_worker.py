"""
비동기 대응안 워커 단독 동작 시험 (self_test/test_response_plan_worker.py)

DB 도 LLM 서버도 필요 없습니다. 에이전트와 적재 함수를 가짜로 바꿔 넣고
워커의 동작만 확인합니다.

    python3 self_test/test_response_plan_worker.py

확인 항목
    1. 정상 처리          제출한 건수만큼 생성·적재되는가
    2. 수신 루프 비차단    생성이 느려도 submit() 이 즉시 돌아오는가
    3. 큐 포화 드롭        큐가 차면 오래된 건을 버리고 최신을 남기는가
    4. 생성 예외 격리      한 건이 실패해도 워커가 죽지 않는가
    5. 적재 실패 집계      sink 가 False 를 반환하면 failed 로 잡히는가
    6. 종료 드레인 기한    close() 가 정해진 시간 안에 끝나는가
"""

import os
import sys
import threading
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.console import enable_utf8_console  # noqa: E402
enable_utf8_console()

from integration.response_plan_worker import ResponsePlanWorker  # noqa: E402

BAR = "=" * 70
_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    mark = "[OK]  " if ok else "[실패]"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))


# ------------------------------------------------------------------------------
# 가짜 객체
# ------------------------------------------------------------------------------

class FakeEvent:
    def __init__(self, seq):
        self.seq = seq
        self.zone = "A"
        self.detected_object = f"테스트-{seq}"
        self.risk_level = "위험"
        self.distance = 1.0
        self.issue = "시험용 이벤트"


class FakePlan:
    def __init__(self, event):
        self.seq = event.seq
        self.zone = event.zone
        self.source_event = event
        self.recommended_action = "시험용 대응안"
        self.reference_docs = ["시험 근거"]
        self.confidence = 0.6


class FakeAgent:
    """generate_response 만 흉내 낸다.

    delay 로 LLM 생성 지연을, fail_on 으로 생성 예외를 재현한다.
    """

    def __init__(self, delay=0.0, fail_on=()):
        self.delay = delay
        self.fail_on = set(fail_on)
        self.seen = []

    def generate_response(self, event):
        if self.delay:
            time.sleep(self.delay)
        self.seen.append(event.seq)
        if event.seq in self.fail_on:
            raise RuntimeError("생성 실패 재현")
        return FakePlan(event)


class FakeSink:
    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.written = []
        self._lock = threading.Lock()

    def __call__(self, plan):
        with self._lock:
            if plan.seq in self.fail_on:
                return False
            self.written.append(plan.seq)
            return True


# ------------------------------------------------------------------------------
# 1. 정상 처리
# ------------------------------------------------------------------------------

def test_basic():
    print("\n [1] 정상 처리")
    print(" " + "-" * 68)

    agent = FakeAgent()
    sink = FakeSink()
    worker = ResponsePlanWorker(agent, sink, queue_size=16)
    worker.start()

    for i in range(5):
        worker.submit(FakeEvent(i))
    worker.close(drain_timeout=5.0)

    st = worker.stats()
    check("5건 모두 생성", st["generated"] == 5, f"generated={st['generated']}")
    check("5건 모두 적재", st["written"] == 5, f"written={st['written']}")
    check("드롭·실패 없음", st["dropped"] == 0 and st["failed"] == 0,
          f"dropped={st['dropped']} failed={st['failed']}")
    check("적재 순서 유지", sink.written == [0, 1, 2, 3, 4], str(sink.written))


# ------------------------------------------------------------------------------
# 2. 수신 루프 비차단
# ------------------------------------------------------------------------------

def test_non_blocking():
    print("\n [2] 수신 루프 비차단 (생성 0.3초 × 5건)")
    print(" " + "-" * 68)

    agent = FakeAgent(delay=0.3)
    sink = FakeSink()
    worker = ResponsePlanWorker(agent, sink, queue_size=16)
    worker.start()

    t0 = time.time()
    for i in range(5):
        worker.submit(FakeEvent(i))
    submit_ms = (time.time() - t0) * 1000

    # 동기였다면 5 × 300ms = 1500ms 가 걸린다.
    check("submit 5건이 즉시 반환", submit_ms < 100,
          f"{submit_ms:.0f}ms (동기라면 약 1500ms)")

    worker.close(drain_timeout=5.0)
    st = worker.stats()
    check("뒤이어 5건 모두 적재", st["written"] == 5, f"written={st['written']}")


# ------------------------------------------------------------------------------
# 3. 큐 포화 드롭
# ------------------------------------------------------------------------------

def test_drop_oldest():
    print("\n [3] 큐 포화 — oldest 정책 (큐 3, 생성 0.4초, 12건 투입)")
    print(" " + "-" * 68)

    agent = FakeAgent(delay=0.4)
    sink = FakeSink()
    worker = ResponsePlanWorker(agent, sink, queue_size=3,
                                drop_policy="oldest")
    worker.start()

    for i in range(12):
        worker.submit(FakeEvent(i))
        time.sleep(0.01)

    worker.close(drain_timeout=6.0)
    st = worker.stats()

    check("드롭이 집계됨", st["dropped"] > 0, f"dropped={st['dropped']}")
    check("적재가 0건은 아님", st["written"] > 0, f"written={st['written']}")
    check("생성+드롭+미처리 = 투입",
          st["generated"] + st["dropped"] + st["discarded"] + st["pending"] == 12,
          f"generated={st['generated']} dropped={st['dropped']} "
          f"discarded={st['discarded']} pending={st['pending']}")
    # oldest 정책이면 마지막 이벤트는 살아남아야 한다.
    check("최신 이벤트가 남음", 11 in sink.written or 11 in agent.seen,
          f"처리된 seq={sorted(agent.seen)}")


def test_drop_newest():
    print("\n [4] 큐 포화 — newest 정책 (큐 3, 생성 0.4초, 12건 투입)")
    print(" " + "-" * 68)

    agent = FakeAgent(delay=0.4)
    sink = FakeSink()
    worker = ResponsePlanWorker(agent, sink, queue_size=3,
                                drop_policy="newest")
    worker.start()

    accepted = 0
    for i in range(12):
        if worker.submit(FakeEvent(i)):
            accepted += 1
        time.sleep(0.01)

    worker.close(drain_timeout=6.0)
    st = worker.stats()

    check("일부는 거부됨", accepted < 12, f"accepted={accepted}/12")
    check("이른 이벤트가 남음", 0 in agent.seen, f"처리된 seq={sorted(agent.seen)}")
    check("submitted 집계 일치", st["submitted"] == accepted,
          f"submitted={st['submitted']} accepted={accepted}")


# ------------------------------------------------------------------------------
# 5. 예외 격리
# ------------------------------------------------------------------------------

def test_exception_isolation():
    print("\n [5] 생성 예외 격리 (3건 중 2번째에서 예외)")
    print(" " + "-" * 68)

    agent = FakeAgent(fail_on=(1,))
    sink = FakeSink()
    worker = ResponsePlanWorker(agent, sink, queue_size=16)
    worker.start()

    for i in range(3):
        worker.submit(FakeEvent(i))
    worker.close(drain_timeout=5.0)

    st = worker.stats()
    check("실패 1건 집계", st["failed"] == 1, f"failed={st['failed']}")
    check("나머지 2건 적재", st["written"] == 2, f"written={st['written']}")
    check("예외 뒤에도 처리 계속", 2 in sink.written, str(sink.written))


def test_sink_failure():
    print("\n [6] 적재 실패 집계 (sink 가 False 반환)")
    print(" " + "-" * 68)

    agent = FakeAgent()
    sink = FakeSink(fail_on=(1,))
    worker = ResponsePlanWorker(agent, sink, queue_size=16)
    worker.start()

    for i in range(3):
        worker.submit(FakeEvent(i))
    worker.close(drain_timeout=5.0)

    st = worker.stats()
    check("생성은 3건", st["generated"] == 3, f"generated={st['generated']}")
    check("적재는 2건", st["written"] == 2, f"written={st['written']}")
    check("실패 1건 집계", st["failed"] == 1, f"failed={st['failed']}")


# ------------------------------------------------------------------------------
# 7. 종료 드레인 기한
# ------------------------------------------------------------------------------

def test_close_deadline():
    print("\n [7] 종료 드레인 기한 (생성 1초 × 6건, 기한 1.5초)")
    print(" " + "-" * 68)

    agent = FakeAgent(delay=1.0)
    sink = FakeSink()
    worker = ResponsePlanWorker(agent, sink, queue_size=16)
    worker.start()

    for i in range(6):
        worker.submit(FakeEvent(i))

    t0 = time.time()
    worker.close(drain_timeout=1.5)
    elapsed = time.time() - t0

    # 기한을 지키지 않으면 6초가 걸린다.
    check("기한 안에 종료", elapsed < 4.0,
          f"{elapsed:.1f}초 (기한 없으면 약 6초)")
    st = worker.stats()
    check("미처리분이 discarded 로 집계", st["discarded"] > 0,
          f"discarded={st['discarded']} generated={st['generated']}")


# ------------------------------------------------------------------------------

def main():
    print(BAR)
    print(" 비동기 대응안 워커 단독 동작 시험")
    print(BAR)

    test_basic()
    test_non_blocking()
    test_drop_oldest()
    test_drop_newest()
    test_exception_isolation()
    test_sink_failure()
    test_close_deadline()

    total = len(_results)
    passed = sum(1 for r in _results if r)

    print()
    print(BAR)
    print(f" 결과: {passed}/{total} 통과")
    print(BAR)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
