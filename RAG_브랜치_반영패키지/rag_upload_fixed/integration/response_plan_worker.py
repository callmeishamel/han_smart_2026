"""
================================================================================
파일명   : integration/response_plan_worker.py
목적     : 대응안 생성을 UDP 수신 루프에서 분리하는 비동기 워커
버전     : v1.0
담당     : 대응안 생성 파트
--------------------------------------------------------------------------------
왜 필요한가
  --use-llm 을 켜면 generate_response() 안에서 GEMMA 2B 응답을 기다린다.
  CPU 추론(Ollama)에서는 한 건에 수 초에서 수십 초가 걸리며, 그동안
  파이프라인의 UDP 수신 루프가 그대로 멈춘다.

  UDP 는 재전송이 없다. 수신 루프가 멈춰 있는 사이 도착한 탐지 패킷은
  소켓 버퍼가 차면 커널에서 버려지고, 그 이벤트는 patrol_logs 에도
  남지 않는다. 대응안 한 건을 만드느라 탐지 기록을 잃는 구조다.

  이 워커는 생성 단계만 별도 스레드로 옮긴다. 수신 루프는 큐에 넣고
  즉시 돌아가므로 적재는 계속된다.

      (기존)  UDP 수신 -> 적재 -> 대응안 생성 대기 -> 적재 -> 다음 수신
      (워커)  UDP 수신 -> 적재 -> 큐 투입 -> 다음 수신
                                    L 워커 스레드에서 생성 -> 적재

사용 규약
  1. agent 는 이 워커 전용 인스턴스여야 한다.
     RagResponseAgent 는 스레드 안전하지 않다. 메인 루프에서 같은
     인스턴스로 generate_response() 를 호출하면 커넥션이 깨진다.

  2. sink 가 쓰는 DB 커넥션은 메인 루프의 것과 달라야 한다.
     psycopg2 커넥션을 두 스레드가 공유하면 한쪽 commit 이 다른 쪽의
     열린 트랜잭션까지 커밋한다. 파이프라인에서 워커 전용 writer 를
     따로 만들어 넘기면 된다.

  3. 중복 억제(스로틀)는 submit() 앞에서 처리한다.
     큐에 넣은 뒤에 걸러 봐야 이미 자리를 차지한 뒤다.

연결 예시
      from integration.response_plan_worker import ResponsePlanWorker

      plan_writer = RagPatrolLogWriter(get_db_config())   # 워커 전용 커넥션
      worker = ResponsePlanWorker(agent, plan_writer.write_response_plan)
      worker.start()
      ...
      worker.submit(event)          # 즉시 반환
      ...
      worker.close()                # 남은 건을 정리하고 종료
================================================================================
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

# 큐 기본 크기.
# 너무 크게 잡으면 생성이 밀릴 때 오래된 이벤트의 대응안이 뒤늦게
# 저장되어 관제 화면에 과거 상황이 올라온다. 32건은 CPU 추론(건당 수 초)
# 기준으로 수 분치에 해당하며, 이보다 밀리면 구성 자체를 손봐야 한다.
DEFAULT_QUEUE_SIZE = 32

# 큐 포화 경고를 다시 출력하기까지의 간격(초).
# 포화 상태에서는 매 이벤트마다 경고가 찍혀 로그가 덮이므로 간격을 둔다.
DROP_LOG_INTERVAL_SEC = 10.0

# close() 가 남은 작업을 기다리는 기본 시간(초).
DEFAULT_DRAIN_TIMEOUT_SEC = 15.0


class ResponsePlanWorker:
    """대응안 생성을 별도 스레드에서 수행하는 워커.

    Parameters
    ----------
    agent : RagResponseAgent
        이 워커 전용 인스턴스. 다른 스레드에서 함께 쓰면 안 된다.
    sink : callable
        생성된 ResponsePlan 을 저장하는 함수. `sink(plan) -> bool` 형태이며
        저장 성공 여부를 반환한다. 메인 루프와 다른 DB 커넥션을 써야 한다.
    queue_size : int, default 32
        대기 큐 크기. 가득 차면 drop_policy 에 따라 처리한다.
    drop_policy : {"oldest", "newest"}, default "oldest"
        큐가 가득 찼을 때 무엇을 버릴지 결정한다.

        oldest  가장 오래 기다린 건을 버리고 새 이벤트를 넣는다(기본값).
                안전 관제에서는 3분 전 상황에 대한 대응안보다 방금 감지된
                상황의 대응안이 쓸모 있으므로 최신을 남긴다.
        newest  새 이벤트를 버리고 큐에 있는 순서를 유지한다. 대응안을
                시간순 기록으로 남겨야 할 때 쓴다.

        어느 쪽이든 patrol_logs 적재는 메인 루프에서 이미 끝났으므로
        탐지 이벤트 자체가 사라지지는 않는다. 버려지는 것은 대응안이다.
    logger : logging.Logger, optional
        지정하면 이 로거로 출력한다. 없으면 verbose 일 때만 print 한다.
    verbose : bool, default False
        logger 가 없을 때 콘솔 출력 여부.
    """

    def __init__(self, agent, sink: Callable[[object], bool],
                 queue_size: int = DEFAULT_QUEUE_SIZE,
                 drop_policy: str = "oldest",
                 logger=None, verbose: bool = False):
        if drop_policy not in ("oldest", "newest"):
            raise ValueError("drop_policy 는 'oldest' 또는 'newest' 여야 합니다")

        self._agent = agent
        self._sink = sink
        self._drop_policy = drop_policy
        self._logger = logger
        self._verbose = verbose

        self._queue: "queue.Queue" = queue.Queue(maxsize=max(1, queue_size))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._deadline: Optional[float] = None

        self._lock = threading.Lock()
        self._n_submitted = 0
        self._n_dropped = 0
        self._n_generated = 0
        self._n_written = 0
        self._n_failed = 0
        self._n_discarded = 0
        self._max_lag_ms = 0.0
        self._last_drop_log = 0.0

    # --------------------------------------------------------------------------
    # 내부 유틸
    # --------------------------------------------------------------------------

    def _log(self, level: str, msg: str, *args):
        if self._logger is not None:
            getattr(self._logger, level)(msg, *args)
        elif self._verbose:
            print(f"[ResponsePlanWorker] {msg % args if args else msg}")

    # --------------------------------------------------------------------------
    # 공개 인터페이스
    # --------------------------------------------------------------------------

    def start(self):
        """워커 스레드를 시작한다. 두 번 호출해도 하나만 뜬다."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="response-plan-worker", daemon=True)
        self._thread.start()
        self._log("info", "대응안 워커 시작 (큐 %d, 드롭 정책 %s)",
                  self._queue.maxsize, self._drop_policy)

    def submit(self, event) -> bool:
        """이벤트를 생성 큐에 넣는다. 즉시 반환한다.

        Returns
        -------
        bool
            큐에 들어갔으면 True, 포화로 버려졌으면 False.
            호출부는 반환값을 무시해도 된다. 통계는 stats() 로 확인한다.
        """
        item = (event, time.time())

        try:
            self._queue.put_nowait(item)
        except queue.Full:
            if self._drop_policy == "newest":
                # 이번 이벤트를 버린다.
                self._count_drop()
                return False

            # oldest: 가장 오래 기다린 건을 비우고 새 이벤트를 넣는다.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._count_drop()      # 밀려난 건이 버려졌다
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # 자리를 비우는 사이 다른 경로에서 다시 채웠다.
                self._count_drop()
                return False

        with self._lock:
            self._n_submitted += 1
        return True

    def stats(self) -> dict:
        """처리 통계를 반환한다.

        submitted  큐에 들어간 건수
        dropped    큐 포화로 버려진 건수
        generated  생성이 끝난 건수
        written    DB 적재까지 성공한 건수
        failed     생성 또는 적재에서 예외가 난 건수
        discarded  종료 대기 시간을 넘겨 처리하지 못한 건수
        max_lag_ms 큐 투입부터 생성 시작까지의 최대 지연(ms)
        pending    아직 큐에 남아 있는 건수
        """
        with self._lock:
            return {
                "submitted": self._n_submitted,
                "dropped": self._n_dropped,
                "generated": self._n_generated,
                "written": self._n_written,
                "failed": self._n_failed,
                "discarded": self._n_discarded,
                "max_lag_ms": round(self._max_lag_ms),
                "pending": self._queue.qsize(),
            }

    def close(self, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SEC):
        """남은 작업을 정해진 시간까지 처리하고 스레드를 정리한다.

        생성이 느린 환경에서 큐가 남아 있으면 종료가 무한정 늘어지므로
        기한을 둔다. 기한을 넘긴 건은 discarded 로 집계한다.
        """
        if self._thread is None:
            return
        self._deadline = time.time() + max(0.0, drain_timeout)
        self._stop.set()
        self._thread.join(timeout=max(0.0, drain_timeout) + 1.0)
        if self._thread.is_alive():
            self._log("warning", "대응안 워커가 시간 안에 끝나지 않아 남은 %d건을 "
                                 "정리하지 못했습니다", self._queue.qsize())
        self._thread = None

    # --------------------------------------------------------------------------
    # 워커 스레드 본체
    # --------------------------------------------------------------------------

    def _count_drop(self):
        now = time.time()
        with self._lock:
            self._n_dropped += 1
            n = self._n_dropped
            should_log = now - self._last_drop_log >= DROP_LOG_INTERVAL_SEC
            if should_log:
                self._last_drop_log = now
        if should_log:
            self._log("warning",
                      "대응안 큐 포화 — 누적 %d건을 건너뜁니다. "
                      "탐지 적재는 계속됩니다 (LLM 생성이 이벤트 속도를 "
                      "따라가지 못하는 상태)", n)

    def _run(self):
        while True:
            if self._stop.is_set() and self._queue.empty():
                break
            try:
                event, enqueued_at = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue

            try:
                # 종료 기한을 넘겼으면 생성하지 않고 비운다.
                if self._deadline is not None and time.time() > self._deadline:
                    with self._lock:
                        self._n_discarded += 1
                    continue
                self._process(event, enqueued_at)
            finally:
                self._queue.task_done()

    def _process(self, event, enqueued_at: float):
        lag_ms = (time.time() - enqueued_at) * 1000.0
        with self._lock:
            if lag_ms > self._max_lag_ms:
                self._max_lag_ms = lag_ms

        try:
            plan = self._agent.generate_response(event)
        except Exception as exc:
            with self._lock:
                self._n_failed += 1
            self._log("error", "대응안 생성 실패 [%s] %s: %s",
                      getattr(event, "zone", "?"),
                      getattr(event, "detected_object", "?"), exc)
            return

        with self._lock:
            self._n_generated += 1

        try:
            ok = bool(self._sink(plan))
        except Exception as exc:
            with self._lock:
                self._n_failed += 1
            self._log("error", "대응안 적재 실패 [%s]: %s",
                      getattr(event, "zone", "?"), exc)
            return

        if ok:
            with self._lock:
                self._n_written += 1
                written = self._n_written
            self._log("info",
                      "대응 지침 생성 [%s] 확신도 %.2f | 근거 %d건 | "
                      "대기 %.0fms (누적 %d건)",
                      plan.zone, plan.confidence or 0.0,
                      len(plan.reference_docs or []), lag_ms, written)
        else:
            with self._lock:
                self._n_failed += 1
