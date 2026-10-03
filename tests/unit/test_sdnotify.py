import secrets
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from local_stt.sdnotify import SdNotifier


@pytest.fixture
def listener() -> Iterator[tuple[socket.socket, str]]:
    """A datagram socket standing in for systemd's notify socket (short path: 108-byte limit)."""
    with tempfile.TemporaryDirectory() as d, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        path = str(Path(d) / "notify")
        s.bind(path)
        s.settimeout(2)
        yield s, path


def test_ready_status_and_stopping(listener: tuple[socket.socket, str]) -> None:
    sock, path = listener
    notifier = SdNotifier({"NOTIFY_SOCKET": path})
    assert notifier.enabled
    assert notifier.ready()
    assert sock.recv(4096) == b"READY=1"
    assert notifier.status("LISTENING (1 queued)\nsecond line")
    assert sock.recv(4096) == b"STATUS=LISTENING (1 queued) second line"
    assert notifier.stopping()
    assert sock.recv(4096) == b"STOPPING=1"
    assert notifier.notify("READY=1", "STATUS=IDLE")
    assert sock.recv(4096) == b"READY=1\nSTATUS=IDLE"


def test_abstract_namespace_socket() -> None:
    name = "local-stt-test-" + secrets.token_hex(8)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        sock.settimeout(2)
        assert SdNotifier({"NOTIFY_SOCKET": "@" + name}).ready()
        assert sock.recv(4096) == b"READY=1"


def test_noop_outside_systemd() -> None:
    notifier = SdNotifier({})
    assert not notifier.enabled
    assert not notifier.ready()


def test_send_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = SdNotifier({"NOTIFY_SOCKET": str(tmp_path / "missing")})
    assert not notifier.ready()
    assert "sd_notify READY failed" in caplog.text
