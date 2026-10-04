"""Microphone capture through PortAudio -> ALSA `pipewire` PCM -> PipeWire (docs/05 §5.2).

Task 0.4 scope: 16 kHz mono float32 frames of 512 samples, source selection through
PIPEWIRE_NODE, and routing verification; task 1.8: device-loss events (the Controller
reconnects, task 2.3b); task 2.7: overflows are flagged on the frame, counted by the consumer.
Not yet implemented: in-process resampling when 16 kHz is rejected, silencing ALSA stderr
messages.
"""

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import sounddevice as sd
from numpy.typing import NDArray

from local_stt.interfaces import AudioOpenError  # the type the Controller catches (E10)

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
DEFAULT_DEVICE = "default"
_PCM = "pipewire"
_FALLBACK_PCM = "default"

log = logging.getLogger("local_stt.audio")


@dataclass(frozen=True)
class AudioFrame:
    recording_id: int
    capture_id: int
    started_at: float  # time.monotonic() of the first sample
    samples: NDArray[np.float32]  # mono, FRAME_SAMPLES long
    overflow: bool = False  # input was lost before this frame (05 §5.6)


@dataclass(frozen=True)
class DeviceInfo:
    name: str  # PipeWire node name, the value for audio.device
    description: str
    is_default: bool


class FrameSink(Protocol):
    """Where frames go: a SimpleQueue, possibly shared with consumer commands."""

    def put_nowait(self, item: AudioFrame) -> None: ...


# (recording_id, capture_id, description) of a stream that stopped without close().
DeviceLostCallback = Callable[[int, int, str], None]


class AudioCapture:
    """Opens the microphone on demand and pushes AudioFrame objects into a queue.

    `open()`/`close()` are called from one thread (the controller). Frames carry the
    identifiers assigned at `open()`; they never change for the lifetime of a stream.
    """

    def __init__(
        self,
        frames: FrameSink,
        device: str = DEFAULT_DEVICE,
        *,
        on_device_lost: DeviceLostCallback | None = None,
    ):
        self._frames = frames
        self.device = device
        self._on_device_lost = on_device_lost
        self._closing = threading.Event()
        self._stream: sd.InputStream | None = None
        self._recording_id = 0
        self._capture_id = 0
        self._clock_offset = 0.0

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def node_name(self) -> str:
        """Unique PipeWire node name of the current stream, used to verify routing."""
        return f"local-stt.capture.{os.getpid()}.{self._capture_id}"

    def open(self, recording_id: int, capture_id: int) -> None:
        if self._stream is not None:
            raise AudioOpenError("stream is already open")
        self._recording_id = recording_id
        self._capture_id = capture_id

        # The pipewire-alsa plugin reads these variables when the PCM is opened.
        if self.device == DEFAULT_DEVICE:
            os.environ.pop("PIPEWIRE_NODE", None)
        else:
            os.environ["PIPEWIRE_NODE"] = self.device
        os.environ["PIPEWIRE_PROPS"] = (
            f'{{ node.name = "{self.node_name}" application.name = "local-stt" }}'
        )

        try:
            stream = sd.InputStream(
                device=_pcm_name(),
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=FRAME_SAMPLES,
                latency="low",
                callback=self._callback,
                finished_callback=self._finished,
            )
            # PortAudio stream time -> time.monotonic() offset, fixed for this stream.
            self._clock_offset = time.monotonic() - stream.time
            stream.start()
        except sd.PortAudioError as e:
            raise AudioOpenError(f"cannot open microphone ({self.device}): {e}") from e
        self._closing.clear()
        self._stream = stream

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        self._closing.set()  # a requested close is not a device loss
        # stop() waits for running callbacks, so no frame from this stream arrives afterwards.
        try:
            stream.stop()
            stream.close()
        except sd.PortAudioError as e:  # the device is already gone
            log.debug("closing the microphone stream: %s", e)

    def _finished(self) -> None:
        # PortAudio thread: the stream ended. Without close() the device was lost (05 §5.2).
        if self._closing.is_set() or self._on_device_lost is None:
            return
        self._on_device_lost(self._recording_id, self._capture_id, "microphone stream stopped")

    def _callback(
        self, indata: NDArray[np.float32], frames: int, time_info: Any, status: Any
    ) -> None:
        # PortAudio thread: must not block or log; the consumer counts overflows.
        adc_time = time_info.inputBufferAdcTime or (time_info.currentTime - frames / SAMPLE_RATE)
        self._frames.put_nowait(
            AudioFrame(
                recording_id=self._recording_id,
                capture_id=self._capture_id,
                started_at=adc_time + self._clock_offset,
                samples=indata[:, 0].copy(),
                overflow=bool(status.input_overflow),
            )
        )

    def routed_source(self, timeout_s: float = 1.0) -> str | None:
        """Name of the PipeWire source the open stream is actually connected to.

        `target.object` echoes PIPEWIRE_NODE even for a nonexistent node, so the source
        index is resolved instead (verified on the reference machine).
        """
        deadline = time.monotonic() + timeout_s
        while True:
            source = resolve_routed_source(
                _pactl_json("list", "source-outputs"),
                _pactl_json("list", "sources"),
                self.node_name,
            )
            if source is not None or time.monotonic() >= deadline:
                return source
            time.sleep(0.05)

    def verify_routing(self) -> str | None:
        """Log a WARNING if the stream is not connected to the configured device."""
        actual = self.routed_source()
        if self.device != DEFAULT_DEVICE and actual != self.device:
            log.warning("device %s not found, using default (%s)", self.device, actual)
        return actual

    @staticmethod
    def list_devices() -> list[DeviceInfo]:
        default = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return parse_sources(_pactl_json("list", "sources"), default)


def parse_sources(sources: list[dict[str, Any]], default: str) -> list[DeviceInfo]:
    return [
        DeviceInfo(s["name"], s.get("description", ""), s["name"] == default)
        for s in sources
        if not s["name"].endswith(".monitor")
    ]


def resolve_routed_source(
    outputs: list[dict[str, Any]], sources: list[dict[str, Any]], node_name: str
) -> str | None:
    names = {s["index"]: s["name"] for s in sources}
    for output in outputs:
        if output.get("properties", {}).get("node.name") == node_name:
            source: str | None = names.get(output.get("source"))
            return source
    return None


def _pcm_name() -> str:
    try:
        sd.query_devices(_PCM, kind="input")
        return _PCM
    except ValueError:
        log.warning(
            "ALSA PCM %r not found (is pipewire-alsa installed?), using %r", _PCM, _FALLBACK_PCM
        )
        return _FALLBACK_PCM


def _pactl_json(*args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["pactl", "-f", "json", *args], capture_output=True, text=True, check=True
    )
    data: list[dict[str, Any]] = json.loads(result.stdout)
    return data
