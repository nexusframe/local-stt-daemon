import queue
from types import SimpleNamespace

import numpy as np
import pytest

from local_stt import interfaces
from local_stt.audio import capture as capture_mod
from local_stt.audio.capture import AudioCapture, parse_sources, resolve_routed_source

SOURCES = [
    {"index": 51, "name": "alsa_output.pci.analog-stereo.monitor", "description": "Monitor"},
    {"index": 52, "name": "alsa_input.pci.analog-stereo", "description": "Built-in"},
    {"index": 60, "name": "bluez_input.headset", "description": "Headset"},
]


def test_parse_sources_skips_monitors_and_marks_default() -> None:
    devices = parse_sources(SOURCES, default="bluez_input.headset")
    assert [(d.name, d.is_default) for d in devices] == [
        ("alsa_input.pci.analog-stereo", False),
        ("bluez_input.headset", True),
    ]


def test_resolve_routed_source_uses_source_index_not_target_object() -> None:
    outputs = [
        {"source": 60, "properties": {"node.name": "other-app"}},
        # PipeWire echoes the requested target even when it does not exist.
        {
            "source": 52,
            "properties": {"node.name": "local-stt.capture.1.3", "target.object": "nope"},
        },
    ]
    assert resolve_routed_source(outputs, SOURCES, "local-stt.capture.1.3") == (
        "alsa_input.pci.analog-stereo"
    )
    assert resolve_routed_source(outputs, SOURCES, "local-stt.capture.1.4") is None


class FakeStream:
    def __init__(self, **kwargs: object) -> None:
        self.finished = kwargs["finished_callback"]
        self.time = 0.0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.finished()  # type: ignore[operator]  # PortAudio calls it when the stream stops

    def close(self) -> None:
        pass


def test_device_loss_reported_only_without_close(monkeypatch: pytest.MonkeyPatch) -> None:
    lost: list[tuple[int, int, str]] = []
    monkeypatch.setattr(capture_mod, "_pcm_name", lambda: "pipewire")
    monkeypatch.setattr(capture_mod.sd, "InputStream", FakeStream)
    capture = AudioCapture(queue.SimpleQueue(), on_device_lost=lambda *a: lost.append(a))

    capture.open(recording_id=1, capture_id=1)
    capture.close()  # requested close: not a loss
    assert lost == []

    capture.open(recording_id=2, capture_id=5)
    stream = capture._stream
    assert isinstance(stream, FakeStream)
    stream.finished()  # type: ignore[operator]  # the device disappeared
    assert lost == [(2, 5, "microphone stream stopped")]


def test_open_matches_the_controller_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Controller calls open(rid, cid) positionally and catches interfaces.AudioOpenError."""

    def failing(**kwargs: object) -> None:
        raise capture_mod.sd.PortAudioError("Device unavailable")

    monkeypatch.setattr(capture_mod, "_pcm_name", lambda: "pipewire")
    monkeypatch.setattr(capture_mod.sd, "InputStream", failing)
    capture = AudioCapture(queue.SimpleQueue())
    with pytest.raises(interfaces.AudioOpenError, match="Device unavailable"):
        capture.open(3, 4)


def test_callback_flags_overflows_on_the_frame() -> None:
    frames: queue.SimpleQueue[capture_mod.AudioFrame] = queue.SimpleQueue()
    capture = AudioCapture(frames)
    data = np.zeros((capture_mod.FRAME_SAMPLES, 1), dtype=np.float32)
    time_info = SimpleNamespace(inputBufferAdcTime=1.0, currentTime=1.1)
    capture._callback(
        data, capture_mod.FRAME_SAMPLES, time_info, SimpleNamespace(input_overflow=False)
    )
    capture._callback(
        data, capture_mod.FRAME_SAMPLES, time_info, SimpleNamespace(input_overflow=True)
    )
    assert [frames.get().overflow, frames.get().overflow] == [False, True]
