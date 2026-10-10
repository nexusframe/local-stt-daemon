"""The whole daemon, conversation mode (v0.6 acceptance K6, tasks 6.2 to 6.4).

Same setup as the continuous suite (`test_continuous_e2e.py`): `app.Daemon` in this process,
a private Xvfb with a focused receiving window, a real `whisper-server` with base-q5_1, the
real IPC socket, and three FLEURS sentences from a `FileAudioSource`. A conversation client
subscribes with `"transcripts": true, "conversation": true`. A second client subscribes
without `"transcripts"`.

Run without network (F1/N5, §14.3 item 4):
`unshare -rn sh -c 'ip link set lo up && XDG_RUNTIME_DIR=$(mktemp -d) pytest -m e2e'`
"""

import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from Xlib import display as xdisplay

from local_stt.stt import whisper_server as ws

from .test_continuous_e2e import SENTENCES, server, three_sentences, wait_for_insertions
from .test_ptt_e2e import RESULT_TIMEOUT_S, running_daemon

pytestmark = [pytest.mark.e2e, pytest.mark.needs_x11, pytest.mark.needs_whisper]

__all__ = ["server", "three_sentences"]  # module fixtures used by the tests below

DICTATION_BACK_S = 1.0  # K6: dictation works again within 1 s of the disconnect


class Subscriber:
    """One `subscribe` stream on a thread; keeps every message."""

    def __init__(self, path: Path, **flags: bool) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.sock.sendall(json.dumps({"cmd": "subscribe", **flags}).encode() + b"\n")
        self.stream = self.sock.makefile("r", encoding="utf-8")
        self.first: dict[str, Any] = json.loads(self.stream.readline())
        self.messages: list[dict[str, Any]] = []
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        try:
            for line in self.stream:
                self.messages.append(json.loads(line))
        except (OSError, ValueError):
            pass  # closed by close()

    def events(self, name: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("event") == name]

    def close(self) -> None:
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


def wait_until(condition: Any, what: Any) -> None:
    deadline = time.monotonic() + RESULT_TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, what()
        time.sleep(0.05)


def clipboard_owner(name: str) -> int:
    d = xdisplay.Display(name)
    try:
        owner = d.get_selection_owner(d.intern_atom("CLIPBOARD"))  # X.NONE (0) or a Window
        return int(getattr(owner, "id", owner))
    finally:
        d.close()


def test_conversation_sends_text_only_to_the_transcript_subscriber(
    server: ws.TemporaryWhisperServer,
    three_sentences: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="local_stt")
    with running_daemon(server, monkeypatch, audio=three_sentences) as s:
        path = s.daemon.ipc.path
        plain = Subscriber(path)
        client = Subscriber(path, transcripts=True, conversation=True)
        assert client.first["event"] == "state", client.first  # answered after the open
        assert client.first["status"]["conversation"]

        finals = lambda: [m for m in client.events("transcript") if m["final"]]  # noqa: E731
        wait_until(lambda: len(finals()) == 3, lambda: client.messages)
        for message, (_, word) in zip(finals(), SENTENCES, strict=True):
            assert word in message["text"].lower(), finals()
        assert {j["result"] for j in client.events("job")} == {"sent"}

        # K6: no text to the window, the clipboard, the history or the plain subscriber
        assert s.receiver.received == [] and s.receiver.keys == []
        assert clipboard_owner(s.display) == 0
        assert s.ipc("history") == {"ok": True, "texts": []}
        assert plain.events("speech_start")  # it sees the speech events ...
        assert not plain.events("transcript")  # ... but no text
        assert not any(word in json.dumps(plain.messages).lower() for _, word in SENTENCES)

        # K6: after the disconnect, dictation works again within 1 s
        client.close()
        t_closed = time.monotonic()
        s.wait_for(lambda st: st["mode"] == "IDLE" and not st["conversation"], poll_s=0.02)
        assert time.monotonic() - t_closed <= DICTATION_BACK_S
        assert s.ipc("toggle") == {"ok": True}
        texts = wait_for_insertions(s, 1)
        assert SENTENCES[0][1] in texts[0].lower(), texts
        assert s.ipc("toggle") == {"ok": True}
        plain.close()

    assert not any(word in caplog.text.lower() for _, word in SENTENCES)  # 12 §12.3
