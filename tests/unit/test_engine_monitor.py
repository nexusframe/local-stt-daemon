import threading
import time

import pytest

from local_stt.engine_monitor import POLL_FAST_S, EngineMonitor
from local_stt.events import EngineStateChanged, Event
from local_stt.interfaces import EngineHealth

READY, STARTING, DOWN = EngineHealth.READY, EngineHealth.STARTING, EngineHealth.DOWN


class Server:
    def __init__(self) -> None:
        self.health = DOWN  # no connection
        self.calls = 0

    def __call__(self) -> EngineHealth:
        self.calls += 1
        return self.health


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def posted() -> list[Event]:
    return []


@pytest.fixture
def monitor(server: Server, clock: Clock, posted: list[Event]) -> EngineMonitor:
    return EngineMonitor(server, posted.append, startup_timeout_s=60, clock=clock)


def states(posted: list[Event]) -> list[EngineHealth]:
    return [e.state for e in posted if isinstance(e, EngineStateChanged)]


def test_connection_error_is_starting_during_the_grace_period(
    monitor: EngineMonitor, server: Server, clock: Clock, posted: list[Event]
) -> None:
    clock.now += 59.9
    assert monitor.poll_once() is STARTING
    assert posted == []  # the Controller also starts in STARTING
    clock.now += 0.1
    assert monitor.poll_once() is DOWN
    assert states(posted) == [DOWN]


def test_loading_model_then_ready(
    monitor: EngineMonitor, server: Server, posted: list[Event]
) -> None:
    server.health = STARTING  # 503 loading model
    monitor.poll_once()
    server.health = READY
    monitor.poll_once()
    monitor.poll_once()
    assert states(posted) == [READY]


def test_ready_then_server_dies_then_returns(
    monitor: EngineMonitor, server: Server, clock: Clock, posted: list[Event]
) -> None:
    server.health = READY
    monitor.poll_once()
    clock.now += 600
    server.health = DOWN
    monitor.poll_once()
    server.health = STARTING  # systemd restarted it, model loading
    monitor.poll_once()
    server.health = READY
    monitor.poll_once()
    assert states(posted) == [READY, DOWN, STARTING, READY]


def test_worker_failure_is_reported_and_recovery_is_seen(
    monitor: EngineMonitor, server: Server, clock: Clock, posted: list[Event]
) -> None:
    server.health = READY
    monitor.poll_once()
    clock.now += 600
    monitor.report_connection_failure()  # the monitor itself had not noticed yet
    monitor.report_connection_failure()
    assert monitor.state is DOWN
    monitor.poll_once()  # server already back
    assert states(posted) == [READY, DOWN, READY]


def test_restart_starts_a_new_grace_period_with_the_new_client(
    monitor: EngineMonitor, server: Server, clock: Clock, posted: list[Event]
) -> None:
    server.health = READY
    monitor.poll_once()
    clock.now += 600
    new = Server()
    monitor.restarted(new, startup_timeout_s=30)
    assert monitor.state is STARTING
    clock.now += 29
    assert monitor.poll_once() is STARTING
    assert new.calls == 1 and server.calls == 1
    clock.now += 1
    assert monitor.poll_once() is DOWN
    assert states(posted) == [READY, DOWN]


def test_poll_started_before_restart_is_discarded(
    monitor: EngineMonitor, server: Server, posted: list[Event]
) -> None:
    def slow_ready() -> EngineHealth:
        monitor.restarted()  # happens while the old health request is in flight
        return READY

    monitor.restarted(slow_ready)
    assert monitor.poll_once() is STARTING
    assert posted == []


def test_thread_polls_fast_and_wakes_on_restart(server: Server, posted: list[Event]) -> None:
    lock = threading.Lock()
    events: list[Event] = []

    def post(event: Event) -> None:
        with lock:
            events.append(event)

    monitor = EngineMonitor(server, post, startup_timeout_s=0)
    monitor.start()
    try:
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert states(events) == [DOWN]
        server.health = READY
        time.sleep(POLL_FAST_S + 0.2)
        assert states(events) == [DOWN, READY]
        calls = server.calls
        monitor.restarted()  # READY polls every 10 s; a restart must not wait for that
        time.sleep(0.2)
        assert server.calls == calls + 1
    finally:
        monitor.stop()
