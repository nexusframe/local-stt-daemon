"""The whole daemon, continuous mode (docs/14-tests.md §14.3 item 3, task 2.10).

Same setup as the PTT suite (`test_ptt_e2e.py`): `app.Daemon` in this process, a private Xvfb
with a receiving window, a real `whisper-server` with base-q5_1, the real IPC socket. The
microphone is a `FileAudioSource` playing three FLEURS sentences separated by pauses longer
than `vad.min_silence_ms`, then silence.

Run without network (F1/N5, §14.3 item 4):
`unshare -rn sh -c 'ip link set lo up && XDG_RUNTIME_DIR=$(mktemp -d) pytest -m e2e'`
"""

import logging
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from local_stt.audio.wav import float32_to_wav_bytes, wav_bytes_to_float32
from local_stt.stt import whisper_server as ws

from .test_ptt_e2e import RESULT_TIMEOUT_S, Session, running_daemon, whisper_server

pytestmark = [pytest.mark.e2e, pytest.mark.needs_x11, pytest.mark.needs_whisper]

FIXTURES = Path(__file__).parent.parent / "fixtures"
# One word per sentence that base-q5_1 recognizes reliably (checked 2026-10-04).
SENTENCES = (("pl_short.wav", "spacer"), ("pl_piramidy.wav", "pokaz"), ("pl_sezon.wav", "lipca"))
LEAD_S = 1.0  # silence before the first sentence
PAUSE_S = 1.5  # > vad.min_silence_ms + speech_pad_ms: each sentence is its own segment
SR = 16000


@pytest.fixture(scope="module")
def server() -> Iterator[ws.TemporaryWhisperServer]:
    srv = whisper_server()
    yield srv
    srv.stop()


def silence(seconds: float) -> NDArray[np.float32]:
    return np.zeros(int(seconds * SR), dtype=np.float32)


@pytest.fixture(scope="module")
def three_sentences(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parts = [silence(LEAD_S)]
    for name, _ in SENTENCES:
        audio, rate = wav_bytes_to_float32((FIXTURES / name).read_bytes())
        assert rate == SR
        parts += [audio, silence(PAUSE_S)]
    path = tmp_path_factory.mktemp("audio") / "three_sentences.wav"
    path.write_bytes(float32_to_wav_bytes(np.concatenate(parts), SR))
    return path


def wait_for_insertions(session: Session, count: int) -> list[str]:
    deadline = time.monotonic() + RESULT_TIMEOUT_S
    while len(session.receiver.received) < count:
        assert time.monotonic() < deadline, session.receiver.received
        time.sleep(0.1)
    return session.receiver.received


def test_three_sentences_are_inserted_in_order(
    server: ws.TemporaryWhisperServer,
    three_sentences: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="local_stt")
    with running_daemon(server, monkeypatch, audio=three_sentences) as s:
        assert s.ipc("toggle") == {"ok": True}  # answered after the microphone opened
        s.wait_for(lambda st: st["mode"] == "CONTINUOUS" and st["audio"]["open"])

        texts = wait_for_insertions(s, 3)
        assert len(texts) == 3, texts
        for text, (_, word) in zip(texts, SENTENCES, strict=True):
            assert word in text.lower(), texts

        assert s.ipc("toggle") == {"ok": True}
        s.wait_for(lambda st: st["mode"] == "IDLE")
        s.wait_for(lambda st: st["stats"]["jobs_ok"] == 3)
        time.sleep(1.0)
        assert len(s.receiver.received) == 3  # trailing silence adds nothing (no hallucination)

    logged = caplog.text
    for seq in (1, 2, 3):
        assert f"src=continuous seq={seq} cut=silence" in logged
    assert not any(word in logged.lower() for _, word in SENTENCES)  # 12 §12.3


def test_stop_mid_sentence_flushes_the_utterance(
    server: ws.TemporaryWhisperServer,
    three_sentences: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="local_stt.timings")
    with running_daemon(server, monkeypatch, audio=three_sentences) as s:
        assert s.ipc("toggle")["ok"]
        s.wait_for(lambda st: st["speech"], poll_s=0.02)
        time.sleep(2.0)  # in the middle of the first sentence
        assert s.ipc("toggle")["ok"]
        s.wait_for(lambda st: st["mode"] == "IDLE")

        [text] = wait_for_insertions(s, 1)
        assert "warto" in text.lower(), text
        s.wait_for(lambda st: st["stats"]["jobs_ok"] == 1)
    assert "src=continuous seq=1 cut=flush" in caplog.text
