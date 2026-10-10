"""Control socket protocol (docs/10-cli-ipc-status.md §10.2) over a real Unix socket."""

import json
import os
import socket
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from local_stt import events as ev
from local_stt import ipc


class Daemon:
    """Answers every event like a controller would, recording what it received."""

    def __init__(self) -> None:
        self.events: list[ev.Event] = []
        self.answer: dict[str, Any] | None = {"ok": True}

    def post(self, event: ev.Event) -> None:
        self.events.append(event)
        reply = getattr(event, "reply", None)
        if reply is not None and self.answer is not None:
            reply.set_result(self.answer)


@pytest.fixture
def path() -> Iterator[Path]:
    # AF_UNIX paths are limited to 108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="lstt-") as d:
        yield Path(d) / "local-stt" / ipc.SOCKET_NAME


@pytest.fixture
def daemon() -> Daemon:
    return Daemon()


@pytest.fixture
def server(path: Path, daemon: Daemon) -> Iterator[ipc.IpcServer]:
    s = ipc.IpcServer(path, daemon.post, reply_timeout_s=0.2, poll_interval_s=0.01)
    s.start()
    yield s
    s.stop()


def raw(path: Path, data: bytes) -> list[dict[str, Any]]:
    """Sends raw bytes, half-closes, returns every response line until the server closes."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(path))
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        return [json.loads(line) for line in s.makefile("rb")]


def test_socket_path() -> None:
    assert ipc.socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == Path(
        "/run/user/1000/local-stt/control.sock"
    )
    with pytest.raises(ipc.IpcError):
        ipc.socket_path({})


def test_permissions(server: ipc.IpcServer, path: Path) -> None:
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("request_", "event_type"),
    [
        ({"cmd": "status"}, ev.StatusRequested),
        ({"cmd": "ptt", "action": "start"}, ev.PttPressed),
        ({"cmd": "ptt", "action": "stop"}, ev.PttReleased),
        ({"cmd": "cancel"}, ev.CancelRequested),
        ({"cmd": "reload"}, ev.ReloadRequested),
        ({"cmd": "toggle"}, ev.ContinuousToggle),
        ({"cmd": "language"}, ev.LanguageSwitch),
        ({"cmd": "language", "set": "en"}, ev.LanguageSwitch),
        ({"cmd": "last"}, ev.HistoryInsert),
        ({"cmd": "last", "n": 3}, ev.HistoryInsert),
        ({"cmd": "history"}, ev.HistoryRequested),
    ],
)
def test_commands_become_events(
    server: ipc.IpcServer, path: Path, daemon: Daemon, request_: dict[str, Any], event_type: type
) -> None:
    daemon.answer = {"ok": True, "status": {"state": "IDLE"}}
    assert ipc.call(request_, path=path) == daemon.answer
    assert [type(e) for e in daemon.events] == [event_type]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b'{"cmd": "nope"}\n', "unknown_command"),
        (b'{"cmd": "ptt", "action": "hold"}\n', "bad_request"),
        (b'{"cmd": "language", "set": 1}\n', "bad_request"),
        (b'{"cmd": "last", "n": 0}\n', "bad_request"),
        (b'{"cmd": "last", "n": "2"}\n', "bad_request"),
        (b'{"cmd": "last", "n": true}\n', "bad_request"),
        (b"[1, 2]\n", "bad_request"),
        (b"{not json\n", "bad_request"),
        (b"\xff\xfe\n", "bad_request"),
    ],
)
def test_rejected_requests(
    server: ipc.IpcServer, path: Path, daemon: Daemon, data: bytes, code: str
) -> None:
    [response] = raw(path, data)
    assert (response["ok"], response["error"]) == (False, code)
    assert daemon.events == []


def test_several_requests_on_one_connection(server: ipc.IpcServer, path: Path) -> None:
    responses = raw(path, b'{"cmd": "cancel"}\n{"cmd": "reload"}\n')
    assert responses == [{"ok": True}, {"ok": True}]


def test_overlong_line_closes_the_connection(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    data = b'{"cmd": "status", "pad": "' + b"x" * ipc.MAX_LINE + b'"}\n{"cmd": "cancel"}\n'
    responses = raw(path, data)
    assert [r["error"] for r in responses] == ["too_long"]
    assert daemon.events == []


def test_line_at_the_limit_is_accepted(server: ipc.IpcServer, path: Path) -> None:
    head = b'{"cmd": "cancel", "pad": "'
    line = head + b"x" * (ipc.MAX_LINE - len(head) - 3) + b'"}\n'
    assert len(line) == ipc.MAX_LINE
    assert raw(path, line) == [{"ok": True}]


def test_controller_timeout(server: ipc.IpcServer, path: Path, daemon: Daemon) -> None:
    daemon.answer = None  # the controller never answers
    response = ipc.call({"cmd": "status"}, path=path)
    assert (response["ok"], response["error"]) == (False, "timeout")


def test_other_uid_is_disconnected(
    server: ipc.IpcServer, path: Path, daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ipc, "peer_uid", lambda sock: 4242)
    with pytest.raises(ipc.IpcError):  # closed or reset, depending on timing
        ipc.call({"cmd": "cancel"}, path=path)
    assert daemon.events == []


def test_peer_uid_of_a_real_connection() -> None:
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        assert ipc.peer_uid(a) == os.getuid()


def test_daemon_not_running(path: Path) -> None:
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call({"cmd": "status"}, path=path)


def test_stale_socket_is_replaced(path: Path, daemon: Daemon) -> None:
    path.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # the file stays, nobody listens
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call({"cmd": "status"}, path=path)
    s = ipc.IpcServer(path, daemon.post, poll_interval_s=0.01)
    s.start()
    try:
        assert ipc.call({"cmd": "cancel"}, path=path) == {"ok": True}
    finally:
        s.stop()
    assert not path.exists()


def test_second_instance_is_refused(server: ipc.IpcServer, path: Path, daemon: Daemon) -> None:
    with pytest.raises(ipc.AnotherInstanceRunning):
        ipc.IpcServer(path, daemon.post).start()
    assert ipc.call({"cmd": "cancel"}, path=path) == {"ok": True}  # the first one is intact


# --- subscribe (task 2.5) ---------------------------------------------------------------------


def wait_until(condition: Any, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_subscribe_streams_the_state_then_published_messages(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": True, "status": {"state": "IDLE"}}
    stream = ipc.subscribe(path=path)
    assert next(stream) == {"event": "state", "status": {"state": "IDLE"}}
    assert [type(e) for e in daemon.events] == [ev.StatusRequested]
    wait_until(lambda: len(server.subscribers._subs) == 1)
    job = {"event": "job", "job_id": 3, "result": "injected"}
    server.publish(job)
    assert next(stream) == job
    server.stop()  # the daemon shuts down: the stream ends
    assert list(stream) == []


def test_subscribe_without_a_controller_answer_is_an_error(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = None
    assert next(ipc.subscribe(path=path))["error"] == "timeout"
    wait_until(lambda: not server.subscribers._subs)


def test_a_disconnected_subscriber_is_removed(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": True, "status": {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(path))
        s.sendall(b'{"cmd": "subscribe"}\n')
        assert json.loads(s.makefile("rb").readline())["event"] == "state"
        wait_until(lambda: len(server.subscribers._subs) == 1)
    wait_until(lambda: not server.subscribers._subs)  # noticed without a published message


def test_a_subscriber_that_falls_behind_is_dropped() -> None:
    subscribers = ipc.Subscribers()
    sub = subscribers.add()
    for i in range(ipc.SUBSCRIBER_BACKLOG + 1):
        subscribers.publish({"n": i})
    assert sub.dropped and not subscribers._subs


# --- transcript stream (task 6.2; ADR-019) ---------------------------------------------------


def test_text_events_go_only_to_transcript_subscribers() -> None:
    subscribers = ipc.Subscribers()
    plain, text = subscribers.add(), subscribers.add(transcripts=True)
    speech = {"event": "speech_end", "utt": 1}
    transcript = {"event": "transcript", "utt": 1, "text": "Ala ma kota."}
    subscribers.publish(speech)
    subscribers.publish(transcript)
    subscribers.publish({"event": "transcript_retracted", "utt": 1})
    assert plain.messages.get_nowait() == speech and plain.messages.empty()
    assert [text.messages.get_nowait()["event"] for _ in range(3)] == [
        "speech_end",
        "transcript",
        "transcript_retracted",
    ]


def test_subscribe_with_transcripts_receives_text(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": True, "status": {"state": "IDLE"}}
    stream = ipc.subscribe(path=path, transcripts=True)
    assert next(stream)["event"] == "state"
    wait_until(lambda: len(server.subscribers._subs) == 1)
    transcript = {"event": "transcript", "utt": 2, "text": "Dzień dobry."}
    server.publish(transcript)
    assert next(stream) == transcript
    server.stop()


def test_subscribe_rejects_a_non_boolean_transcripts_flag(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": True, "status": {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(path))
        s.sendall(b'{"cmd": "subscribe", "transcripts": "yes"}\n')
        reply = json.loads(s.makefile("rb").readline())
    assert reply["error"] == "bad_request"
    assert not server.subscribers._subs


# --- conversation mode (task 6.3) ------------------------------------------------------------


def test_conversation_subscription_owns_the_mode_until_it_closes(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": True, "status": {"state": "LISTENING"}}
    stream = ipc.subscribe(path=path, transcripts=True, conversation=True)
    assert next(stream)["event"] == "state"
    start = daemon.events[0]
    assert isinstance(start, ev.ConversationStart)
    stream.close()  # the client closes the connection
    wait_until(lambda: any(isinstance(e, ev.ConversationEnd) for e in daemon.events))
    (end,) = [e for e in daemon.events if isinstance(e, ev.ConversationEnd)]
    assert end.owner == start.owner


def test_a_closed_conversation_is_noticed_quickly(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    """K6 (v0.6): dictation works again within 1 s after the client disconnects."""
    daemon.answer = {"ok": True, "status": {}}
    stream = ipc.subscribe(path=path, conversation=True)
    next(stream)
    time.sleep(0.05)
    closed = time.monotonic()
    stream.close()
    wait_until(lambda: any(isinstance(e, ev.ConversationEnd) for e in daemon.events))
    assert time.monotonic() - closed < 0.5


def test_a_refused_conversation_ends_the_subscription(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": False, "error": "busy", "message": "dictation is running"}
    assert list(ipc.subscribe(path=path, conversation=True)) == [daemon.answer]
    wait_until(lambda: not server.subscribers._subs)
    assert not any(isinstance(e, ev.ConversationEnd) for e in daemon.events)


def test_two_conversation_subscriptions_have_different_owners(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    daemon.answer = {"ok": True, "status": {}}
    first = ipc.subscribe(path=path, conversation=True)
    second = ipc.subscribe(path=path, conversation=True)
    next(first), next(second)
    owners = {e.owner for e in daemon.events if isinstance(e, ev.ConversationStart)}
    assert len(owners) == 2
    first.close(), second.close()


def test_subscribe_rejects_a_non_boolean_conversation_flag(
    server: ipc.IpcServer, path: Path, daemon: Daemon
) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(path))
        s.sendall(b'{"cmd": "subscribe", "conversation": 1}\n')
        assert json.loads(s.makefile("rb").readline())["error"] == "bad_request"
    assert daemon.events == []


def test_subscribe_to_a_stopped_daemon(path: Path) -> None:
    with pytest.raises(ipc.DaemonNotRunning):
        next(ipc.subscribe(path=path))
