# 05. Audio capture and VAD

## 5.1 Internal format

Throughout the daemon, audio has a single format: **`numpy.float32`, mono, 16,000 Hz, range [-1, 1]**, divided into **frames of 512 samples (32 ms)**. 512 samples is exactly the window size required by Silero VAD at 16 kHz, so a microphone frame is immediately usable as a VAD frame. Conversion to s16le WAV occurs only when sending audio to the engine.

## 5.2 `AudioCapture` (`local_stt/audio/capture.py` module)

Library: **`sounddevice`** (PortAudio 19.6, `libportaudio2`). On Ubuntu 24.04, the stream goes through ALSA → `pipewire-alsa` → PipeWire.

The choice is justified in [03](03-decisions.md), ADR-005. In short: a numpy callback, overflow flags, and no subprocesses in the audio path. The alternative, piping from `pw-record`, is simpler but provides no stream status and ties us to PipeWire.

```python
class AudioCapture:
    def open(self, *, recording_id: int, capture_id: int) -> None  # AudioOpenError; frames → SimpleQueue
    def close(self) -> None
    @property
    def is_open(self) -> bool
    @staticmethod
    def list_devices() -> list[DeviceInfo]    # pactl -f json list sources (excluding .monitor)
```

### Device selection and opening

PortAudio 19.6 sees devices only through ALSA and only those present at `Pa_Initialize`. `hw:` devices cannot be opened because PipeWire holds them, and USB/BT sources connected later would not appear in the list. Therefore, **we always open the ALSA `pipewire` PCM** (from `pipewire-alsa`) and select the specific source on the PipeWire side:

1. `audio.device = "default"` → nothing is set. PipeWire connects the stream to the default source selected in GNOME sound settings and switches automatically when the default device changes or a device is hot-plugged.
2. Any other value is a **PipeWire node name** (for example `alsa_input.pci-0000_00_1f.3.analog-stereo`, `bluez_input.…`). Before each `open()`, we set `os.environ["PIPEWIRE_NODE"] = <name>`, because the ALSA plugin reads this variable when opening the PCM. `local-stt devices` lists available names through `pactl -f json list sources` (excluding `.monitor` sources). With a nonexistent name, PipeWire connects the default source, so after opening we use `pactl -f json list source-outputs` to verify that our stream was routed to the correct node. Otherwise, we log WARNING `device <X> not found, using default`. Verification details (verified in task 0.4): the stream's `target.object` property echoes `PIPEWIRE_NODE` **even when that node does not exist**, so it cannot be trusted; we resolve the source-output's `source` index to a node name through `pactl -f json list sources`. `pactl` reports no process ID for pipewire-alsa streams, so our stream is identified by a unique node name set through `PIPEWIRE_PROPS='{ node.name = "local-stt.capture.<pid>.<capture_id>" application.name = "local-stt" }'` before opening.
   - **Verified** on the reference machine (`pipewire-alsa` 1.0.5): `PIPEWIRE_NODE=alsa_input.pci-0000_00_1f.3.analog-stereo arecord -D pipewire …` creates a source output whose `target.object` equals that name. **Verified for PortAudio (`sounddevice` 0.5.6) in task 0.4** (`tests/integration/test_capture_audio.py`, `needs_audio`): the default device routes to the default source, a named node routes to that node, and a nonexistent node falls back to the default source with the WARNING; 16 kHz is accepted by the `pipewire` PCM, frames are 512 samples, and PortAudio stream time equals `time.monotonic()` on this machine (the offset is still computed at open).
3. `InputStream(device="pipewire", samplerate=16000, channels=1, dtype="float32", blocksize=512, latency="low", callback=...)`. PipeWire (48 kHz clock) resamples transparently.
4. If the `pipewire` PCM does not exist (`pipewire-alsa` is missing), we use `default` with a WARNING. If 16 kHz is rejected (`-9997 Invalid sample rate`), we open the stream at `default_samplerate` and resample in-process: `soxr.ResampleStream(in_rate, 16000, 1, dtype="float32")` plus assembly into 512-sample frames.
5. We execute `import sounddevice` (and therefore `Pa_Initialize`) **once at daemon startup**. ALSA messages written by the C library to fd 2 are silenced through `snd_lib_error_set_handler` (ctypes, `libasound.so.2`) with a handler that forwards them at TRACE. stderr cannot be redirected because it is the same fd used for journald logs.

**Fixed parameters.** The initial design included configurable `sample_rate` and `channels`. We deliberately omit them: Whisper and Silero require 16 kHz mono, and conversion is provided by PipeWire or `soxr`. A config option could only break the pipeline.

### Callback

The callback runs in the PortAudio thread and **must not block**:

```python
def _callback(indata, frames, time_info, status):
    if status.input_overflow:
        self._overflows += 1          # counted, logged every 5 s by the consumer thread
    self._q.put_nowait(AudioFrame(
        recording_id=self._recording_id,
        capture_id=self._capture_id,
        started_at=self._adc_to_monotonic(time_info.inputBufferAdcTime),
        samples=indata[:, 0].copy(),
    ))   # queue.SimpleQueue; identifiers remain unchanged for this stream's lifetime
```

The `audio-consumer` thread receives frames from `SimpleQueue` and passes them to the receiver matching `recording_id` and `capture_id`: `Recorder` in PTT or `Segmenter` in continuous mode. `AudioFrame.started_at` is the time of the first sample in the `time.monotonic()` domain; the PortAudio clock offset is established on opening, and resampling preserves the timeline. This timestamp allows the final frame to be trimmed to the release/limit time. Frames from an old stream are discarded and never enter a new recording. Commands that prepare a receiver are processed before its first frame; `finish_ptt` and `flush` drain frames from the specified stream before emitting confirmation (04 §4.3).

`finished_callback` without a requested `close()`, or an `sd.PortAudioError` exception, sends `AudioError(device_lost, recording_id, capture_id)` to the Controller. A normal close is not reported as device loss.

### When the stream is open

| Mode | Stream |
|---|---|
| IDLE | **closed** — GNOME does not show the microphone indicator, and Bluetooth headsets do not switch to the HFP profile |
| PTT | opened on `PttPressed`, closed on `PttReleased` |
| CONTINUOUS | open for the entire duration of the mode |

**Clipped beginning and PTT start-sound leakage.** Opening the stream usually takes 30–150 ms. We play the `start` sound only after the first microphone frame (`RecordingStarted`); the user begins speaking after the signal and an 80 ms margin, when masking ends. However, sound from laptop speakers enters the built-in microphone. Therefore, Recorder **discards samples from `RecordingStarted` until the end of the sound + 80 ms** (~210 ms). A brief PTT tap may therefore play the start sound but causes neither transcription nor the stop sound. With `feedback.sounds=false`, or when playback is unavailable, there is no masking window. In continuous mode, the `start` sound plays *before* the stream is opened ([04](04-state-machine.md) §4.3).

The “keep the stream open for another N seconds after PTT” option was rejected in v0.1–v0.3: it complicates the state machine, while the gain (~100 ms) is smaller than the start-sound masking interval.

## 5.3 `Recorder` (PTT)

- Recorder is used exclusively in the audio-consumer thread. It collects frames in a list and joins them with `np.concatenate` in `end()`; an empty buffer yields an empty array without an exception. It discards samples from the start-sound window (5.2).
- The `ptt.max_duration_s` limit (default 120): once exceeded, it emits `RecordingLimitReached`.
- `end()` returns an `AudioClip` ([02](02-architecture.md) §2.6) only after capture has closed, its queue has drained, and the entire buffer has been trimmed to the release/limit boundary. Audio-consumer sends `RecordingFinished` with matching identifiers. The Controller never calls `end()` directly.
- The `ptt.min_duration_ms` threshold is measured from press to release (also for IPC), independently of masking and microphone-open latency. `AudioClip.duration_s`, by contrast, is the actual duration of samples sent to the pipeline. Empty audio after masking ends as `JobDiscarded(no_speech)`, without an HTTP request.
- `discard(ids)` removes only the buffer and frames for the specified recording; it creates neither an `AudioClip` nor an STT job. Audio-consumer executes the command before starting the next recording.

**The silence gate and trimming run in PipelineWorker**, not in the Controller ([04](04-state-machine.md) §4.4):

- **RMS gate** (v0.1 design; since the 2026-10-03 acceptance used only with `vad.enabled = false` or when the VAD model fails to load). We calculate RMS in 100 ms windows. If no window exceeds `ptt.silence_rms_dbfs` (default -50 dBFS), the job ends with `JobDiscarded(no_speech)` and the `cancel` sound. We calculate over windows rather than the whole recording because 1 s of speech in 20 s of silence must not be lost. The reason for the gate: Whisper hallucinates on silence alone (for example, “Napisy stworzone przez społeczność Amara.org”).
- **VAD trimming** (when `vad.enabled`, the default). Planned for v0.2 (task 2.6), brought forward to v0.1 together with task 2.1 (user decision 2026-10-03): on the reference machine room noise measured −40 dBFS (−29 dBFS with an air purifier) in every 100 ms window, so the −50 dBFS RMS gate never rejected silence and Whisper inserted hallucinations, and a key click reached −8 dBFS ([acceptance](acceptance-v0.1.md#finding-the-rms-gate-cannot-reject-silence)). No fixed RMS threshold separates that noise from speech. The recording passes through Silero offline (`VadTrimmer`, `audio/vad.py`):
  - no frame with `p ≥ start_threshold` → `JobDiscarded(no_speech)`,
  - otherwise, keep the frames from the first one with `p ≥ start_threshold` to the last one with `p ≥ end_threshold` (the Segmenter's hysteresis, 5.5) plus `vad.speech_pad_ms` on each side, clipped to the recording. The last partial frame is zero-padded for the model.

  The Silero session is reset before each recording. `vad.*` and `stt.models_dir` reload at IDLE (04 §4.6); the model is reloaded only when its path changes. If it cannot be loaded, the error is logged and the RMS gate is used.

  This shortens the audio and therefore also lets more recordings fit the fixed `stt.audio_ctx` window (06 §6.7). The worker uses **its own instance** of `SileroVad`, because an ONNX session is stateful and cannot be shared with audio-consumer.

## 5.4 Silero VAD (`local_stt/audio/vad.py` module)

- Model: `silero_vad.onnx` from the `snakers4/silero-vad` repository, tag **v6.2.1**, file `src/silero_vad/data/silero_vad.onnx` (2,327,524 B, MIT license). Downloaded by `install.sh` to `~/.local/share/local-stt/models/`; its SHA256 is pinned in `src/local_stt/models.sha256`.
- Runtime: `onnxruntime` (CPU), **without torch**. The PyPI `silero-vad` package pulls in torch, so we do not use it.
- Session: `SessionOptions.intra_op_num_threads = 1`, `inter_op_num_threads = 1`, so VAD does not compete with whisper.cpp for cores.

Model contract (16 kHz):

| Tensor | Shape | Type | Contents |
|---|---|---|---|
| `input` input | `[1, 576]` | float32 | final 64 samples of the previous frame (context) + 512 new samples |
| `state` input | `[2, 1, 128]` | float32 | RNN state; zeros after `reset()` |
| `sr` input | scalar | int64 | `16000` |
| output 0 | `[1, 1]` | float32 | speech probability |
| output 1 | `[2, 1, 128]` | float32 | new state |

```python
class SileroVad:
    def reset(self) -> None                     # state = 0, context = 0
    def __call__(self, frame512: np.ndarray) -> float
```

The cost is on the order of 1 ms of CPU time per frame, or ~31 calls/s, which uses about 3% of one core in continuous mode. The N4 budget is 5% (leaving capacity for the rest of the daemon). If measurements exceed it, frames with RMS < -60 dBFS skip Silero (p = 0, and the RNN state is reset after 1 s of such silence). `bench --soak` verifies N4.

## 5.5 `Segmenter` (continuous)

It divides the frame stream into utterances using **hysteresis**: speech begins above `start_threshold` and ends only after `min_silence_ms` with probability below `end_threshold`. Values between the thresholds do not change the state. This eliminates SPEECH/SILENCE oscillation with a weak microphone.

### Parameters (`[vad]` in the config)

| Parameter | Default | Meaning |
|---|---:|---|
| `start_threshold` | 0.50 | `p ≥` → “speech” frame while detecting the start |
| `end_threshold` | 0.35 | `p <` → “silence” frame while detecting the end |
| `min_speech_ms` | 250 | minimum continuous speech required to accept a start (filters out taps and coughs) |
| `min_silence_ms` | 700 | silence that ends an utterance (shorter = faster, but may cut a sentence in half) |
| `speech_pad_ms` | 300 | audio included before the start and after the end of an utterance |
| `max_segment_s` | 15 | hard segment-duration limit |
| `split_search_s` | 3 | window at the end of a segment in which to search for a split point |

### State machine

```text
            p ≥ start                       run of (p ≥ end) ≥ min_speech_ms
 SILENCE ─────────────► CANDIDATE ───────────────────────────────────────► SPEECH ──► emit SpeechStarted
    ▲                      │ p < end                                        │  ▲
    │                      ▼                                                │  │ p ≥ start: silence_ms = 0
    └───────────── (frames return to the pre-roll buffer)                    │  │
                                                                            ▼  │
                                              p < end: silence_ms += 32 ─► TRAILING
                                                                            │
                          silence_ms ≥ min_silence_ms: emit Segment, SpeechEnded ──► SILENCE
```

Detailed rules:

1. **Pre-roll.** In SILENCE, we keep a ring buffer containing the last `speech_pad_ms` of frames. On transition to SPEECH, the segment begins with this buffer's contents so the beginning of the first syllable is not lost.
2. **CANDIDATE → SPEECH** requires every frame over `min_speech_ms` to have `p ≥ end_threshold`. A single frame below it returns the state machine to SILENCE.
3. **SPEECH/TRAILING.** A frame with `p < end_threshold` starts or increments the silence counter. A frame with `p ≥ start_threshold` resets it. A frame between the thresholds leaves the counter unchanged (our silence-accumulation rule; it is not identical to Silero's `VADIterator`, which measures from the start of potential silence).
4. **End of utterance.** The segment contains audio through the end of speech (the last frame with `p ≥ end_threshold`) plus `speech_pad_ms` of silence from the TRAILING buffer, clipped to the collected audio. The remaining silence is discarded; its final `speech_pad_ms` becomes the next pre-roll, so consecutive segments never share samples.
5. **Duration limit.** When a segment reaches `max_segment_s`:
   - in the final `split_search_s` seconds, find the longest run of frames with `p < end_threshold` (at least 96 ms) and split in its middle,
   - if no such run exists, split at the frame with the lowest `p` in that window,
   - emit the first part and retain the second as the beginning of a new segment (the SPEECH state continues).

   This minimizes splits in the middle of a word.
6. **`flush()`** (disabling continuous mode): if the state is SPEECH/TRAILING and at least `min_speech_ms` of speech has been collected, emit the segment immediately.
7. **Too little speech.** On every cut (silence, flush, and the first part of a `max_length` split), a segment with less than `min_speech_ms` of speech is dropped without consuming a `seq` number. After a `max_length` split the remainder can hold only silence or a fragment of a word, on which Whisper hallucinates (user decision 2026-10-04).
8. **`reset()`**: clears buffers and calls `SileroVad.reset()`. Called in audio-consumer at the start of a continuous session. On reconnect, buffers/VAD are reset after flush completes, but the session identifier and next `seq` number are preserved.

Emitted object:

```python
@dataclass(frozen=True)
class AudioSegment:
    samples: np.ndarray        # float32, 16 kHz
    session_id: int
    seq: int                   # number within the session; also increases after reconnect
    ended_at: float            # monotonic: silence detection / limit / flush request
    speech_ms: int             # speech duration without padding
    cut: Literal["silence", "max_length", "flush"]
```

Meaning of `cut`:

- `max_length` tells TextProcessor that the segment ends in the middle of a sentence. We then remove the final period and lowercase the first letter of the next segment ([08](08-text-injection.md) §8.2).
- The PTT counterpart is `cut="max_duration"` (recording interrupted by the limit).

## 5.6 Audio error handling

PTT: v0.1 (error → recording discarded). Continuous: v0.2.

| Situation | Detection | Response |
|---|---|---|
| No device when opening | `AudioOpenError` | PTT: `error` sound + notification naming the device; continuous: the same, and the mode is not enabled |
| Stream interrupted (for example, PipeWire restart) | `finished_callback` without `close()` / `PortAudioError` / no frame for 2 s (watchdog) | PTT: recording discarded. Continuous: `flush()`, followed by up to 3 reopen attempts at 1 s intervals; after 3 failures, the mode is disabled with a notification ([04](04-state-machine.md) §4.3). Disconnecting a USB/BT microphone usually **does not** interrupt the stream — PipeWire reroutes it to the default source (INFO log with the new node name) |
| Overflow | `status.input_overflow` | counter, WARNING every 5 s with the count; at > 20 overflows/min, a WARNING about CPU overload |
| Digital silence (microphone muted in the system) | RMS < -80 dBFS for 5 s in continuous mode | one “Microphone appears to be muted” notification per session (`errors` level) |

*Stalled stream (v0.2 acceptance, 2026-10-04, tested on the reference machine with PortAudio 19.6 + `pipewire-alsa` 1.0.5).* After `systemctl --user restart pipewire` an open stream stops calling back but stays `active`: no `finished_callback`, no error. About 70 s later PortAudio's ALSA xrun recovery loops (`alsa_snd_pcm_prepare … failed`, ~70 000 stderr lines/s) and leaks native memory (35 → 416 MB in 5 s in a bare `sounddevice` script, 11.4 GB in the daemon before the kernel OOM killer). On such a stream `stop()` blocks indefinitely (> 40 s observed), while `abort()` returns at once and calls `finished_callback`. Therefore:

- `AudioCapture` runs a watchdog thread per open stream: no frame for `STALL_S = 2 s` (checked every 0.5 s, measured from `open()` until the first frame) → one WARNING and the same device-loss event as `finished_callback` (`"microphone stream stalled"`), so the existing response above applies.
- `close()` calls `abort()` when the last frame is older than 0.5 s, otherwise `stop()`. `stop()` keeps the last buffered frame: on a healthy stream it delivered ~30 ms more audio than `abort()` (8 runs each), which matters at the end of a PTT recording. Residual risk: a stream that dies less than 0.5 s before `close()` can still block `stop()`.
- `local-stt.service` has `MemoryMax=1G` as a backstop ([11](11-daemon-systemd-installation.md) §11.5).

*Implementation (task 2.7).* The PortAudio callback must not block or log, so it only flags the frame (`AudioFrame.overflow`); the audio consumer counts overflows of every stream, logs at most one WARNING per 5 s with the number of frames lost since the previous one, and a separate WARNING (at most once a minute) when more than 20 fall within the last 60 s. The total since startup is `status` → `audio.overflows`. Digital silence is measured per 32 ms frame of the continuous stream; the consumer posts `MicrophoneSilent` once per session (a reconnect keeps the session) and the Controller shows the notification unless the session is stopping. Dictation continues.

## 5.7 What we do not do

- We do not write audio to disk (except `record-corpus` for benchmarking, when explicitly requested).
- We do not apply noise reduction or AGC. PipeWire/WebRTC echo cancellation can be enabled system-wide, outside this project. `doctor` describes this option when the signal level is very low.
