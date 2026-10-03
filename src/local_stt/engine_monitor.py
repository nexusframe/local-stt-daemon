"""EngineMonitor: polls whisper-server health and reports engine state (docs/04 §4.5).

Polls every 500 ms in STARTING and DOWN and every 10 s in READY. A connection error counts as
STARTING until `stt.startup_timeout_s` has passed since daemon start or a requested server
restart, and as DOWN afterwards. Only changes are posted, as `EngineStateChanged`.

PipelineWorker reports connection failures through `report_connection_failure()` rather than
posting DOWN itself: otherwise the monitor, still believing READY, would see no change when the
server comes back and the controller would stay DOWN.
"""

import logging
import threading
import time
from collections.abc import Callable

from local_stt.events import EngineStateChanged, Event
from local_stt.interfaces import EngineHealth

log = logging.getLogger("local_stt.stt")

POLL_FAST_S = 0.5
POLL_READY_S = 10.0

HealthCheck = Callable[[], EngineHealth]


class EngineMonitor:
    def __init__(
        self,
        health: HealthCheck,
        post: Callable[[Event], None],
        *,
        startup_timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._health = health
        self._post = post
        self._startup_timeout_s = startup_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at = clock()
        self._state = EngineHealth.STARTING  # the Controller's initial engine state
        self._epoch = 0  # bumped by restarted()/failures: a poll started earlier is stale
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> EngineHealth:
        with self._lock:
            return self._state

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="engine-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()

    def set_startup_timeout(self, startup_timeout_s: float) -> None:
        """Live reload of `stt.startup_timeout_s` (04 §4.6); the current grace period keeps
        its start time."""
        with self._lock:
            self._startup_timeout_s = startup_timeout_s

    def restarted(
        self, health: HealthCheck | None = None, startup_timeout_s: float | None = None
    ) -> None:
        """A server restart was requested (04 §4.6): restart the startup grace period, optionally
        with a new client and timeout, and poll immediately."""
        with self._lock:
            if health is not None:
                self._health = health
            if startup_timeout_s is not None:
                self._startup_timeout_s = startup_timeout_s
            self._started_at = self._clock()
            self._state = EngineHealth.STARTING
            self._epoch += 1
        self._wake.set()

    def report_connection_failure(self) -> None:
        """PipelineWorker could not connect (04 §4.4): DOWN now, then fast polling."""
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
        self._set(EngineHealth.DOWN, epoch)
        self._wake.set()

    def poll_once(self) -> EngineHealth:
        with self._lock:
            health, started_at, timeout = self._health, self._started_at, self._startup_timeout_s
            epoch = self._epoch
        state = health()  # outside the lock: up to the client's health timeout
        if state is EngineHealth.DOWN and self._clock() - started_at < timeout:
            state = EngineHealth.STARTING
        self._set(state, epoch)
        return self.state

    def _set(self, state: EngineHealth, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                return  # restarted or failure reported while this poll was running
            changed, self._state = self._state is not state, state
        if changed:
            level = logging.ERROR if state is EngineHealth.DOWN else logging.INFO
            log.log(level, "engine %s", state.value)
            self._post(EngineStateChanged(state))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()  # a wake-up from now on cuts the next wait short
            state = self.poll_once()
            self._wake.wait(POLL_READY_S if state is EngineHealth.READY else POLL_FAST_S)
