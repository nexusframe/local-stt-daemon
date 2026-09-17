"""Real microphone through PipeWire (docs/14-tests.md, needs_audio). Opens the mic briefly."""

import queue
import subprocess
import time

import numpy as np
import pytest

from local_stt.audio.capture import (
    FRAME_SAMPLES,
    AudioCapture,
    AudioFrame,
)

pytestmark = pytest.mark.needs_audio


def _default_source() -> str:
    return subprocess.run(
        ["pactl", "get-default-source"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _capture(device: str, seconds: float = 0.5) -> tuple[list[AudioFrame], str | None, float]:
    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    capture = AudioCapture(frames, device=device)
    opened_at = time.monotonic()
    capture.open(recording_id=7, capture_id=11)
    try:
        time.sleep(seconds)
        routed = capture.verify_routing()
    finally:
        capture.close()
    assert not capture.is_open
    collected = []
    while not frames.empty():
        collected.append(frames.get_nowait())
    return collected, routed, opened_at


def test_frames_format_ids_and_timestamps() -> None:
    frames, _, opened_at = _capture("default")

    assert len(frames) >= 10  # ~15 frames in 0.5 s
    for f in frames:
        assert (f.recording_id, f.capture_id) == (7, 11)
        assert f.samples.dtype == np.float32 and f.samples.shape == (FRAME_SAMPLES,)
    starts = np.array([f.started_at for f in frames])
    assert abs(starts[0] - opened_at) < 0.5
    assert np.allclose(np.diff(starts), FRAME_SAMPLES / 16000, atol=0.004)


def test_default_device_routes_to_default_source() -> None:
    _, routed, _ = _capture("default", seconds=0.2)
    assert routed == _default_source()


def test_pipewire_node_selects_named_source() -> None:
    node = _default_source()
    _, routed, _ = _capture(node, seconds=0.2)
    assert routed == node


def test_nonexistent_node_falls_back_to_default_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, routed, _ = _capture("alsa_input.does-not-exist", seconds=0.2)
    assert routed == _default_source()
    assert "device alsa_input.does-not-exist not found" in caplog.text
