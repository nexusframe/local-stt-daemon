"""`bench --soak` helpers (docs/13-benchmark.md §13.3-13.5, task 2.9)."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from local_stt import cli
from local_stt.bench import soak
from local_stt.bench.soak import Word


def token(text: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {"text": text, "offsets": {"from": start_ms, "to": end_ms}}


WHISPER_JSON = {
    "transcription": [
        {
            "tokens": [
                token("[_BEG_]", 0, 0),
                token(" Ma", 100, 200),
                token("ria", 200, 400),
                token(" Curie", 450, 900),
                token(",", 900, 920),
                token("[_TT_50]", 1000, 1000),
            ]
        },
        {"tokens": [token(" rad", 1500, 1800), token(" ", 1800, 1800)]},
    ]
}


def test_words_from_whisper_json() -> None:
    assert soak.words_from_whisper_json(WHISPER_JSON) == [
        Word(0.1, 0.4, "Maria"),
        Word(0.45, 0.92, "Curie,"),
        Word(1.5, 1.8, "rad"),
    ]


def test_load_words_verified_and_whisper(tmp_path: Path) -> None:
    verified = tmp_path / "001.words.json"
    verified.write_text(json.dumps([{"word": "rad", "start": 1.5, "end": 1.8}]))
    assert soak.load_words(verified) == ([Word(1.5, 1.8, "rad")], True)
    auto = tmp_path / "001.json"
    auto.write_text(json.dumps(WHISPER_JSON))
    words, is_verified = soak.load_words(auto)
    assert len(words) == 3 and not is_verified


@pytest.mark.parametrize(
    ("cut", "incorrect"),
    [
        (0.25, True),  # inside "Maria"
        (0.13, False),  # within 40 ms of its start
        (0.37, False),  # within 40 ms of its end
        (0.425, False),  # between words
        (1.2, False),  # in a pause
    ],
)
def test_incorrect_cuts(cut: float, incorrect: bool) -> None:
    words = [Word(0.1, 0.4, "Maria"), Word(0.45, 0.92, "Curie")]
    assert soak.incorrect_cuts([cut], words) == ([cut] if incorrect else [])


def test_locate_end_finds_the_segment_in_the_file() -> None:
    rng = np.random.default_rng(0)
    audio = rng.standard_normal(16000 * 3).astype(np.float32)
    segment = audio[8000:20000].copy()
    assert soak.locate_end(segment, audio) == 20000 / 16000
    assert soak.locate_end(np.zeros(1000, dtype=np.float32), audio) is None  # digital silence
    repeated = np.concatenate([audio, audio])
    assert soak.locate_end(segment, repeated) is None  # ambiguous


def test_slope_per_minute() -> None:
    times = [0.0, 60.0, 120.0]
    assert soak.slope_per_minute(times, [1.0, 1.5, 2.0]) == pytest.approx(0.5)
    assert soak.slope_per_minute([0.0], [3.0]) == 0.0


def test_sustained_freq_drop() -> None:
    steady = [2000.0] * 120
    assert soak.sustained_freq_drop(steady, 60) == pytest.approx(0.0)
    throttled = [2000.0] * 60 + [1200.0] * 60  # 40 % lower for a full window
    assert soak.sustained_freq_drop(throttled, 60) == pytest.approx(0.4)
    dip = [2000.0] * 100 + [1000.0] * 5 + [2000.0] * 60  # brief: averaged out
    drop = soak.sustained_freq_drop(dip, 60)
    assert drop is not None and drop < 0.1
    assert soak.sustained_freq_drop([2000.0] * 90, 60) is None  # too short to tell


def jobs(*pairs: tuple[float, float | None]) -> list[dict[str, Any]]:
    return [
        {"audio_s": a, "processing_s": p, "result": "injected" if p else "failed"} for a, p in pairs
    ]


def summarize(recorder: soak._Recorder, timeline: list[dict[str, float]]) -> dict[str, Any]:
    return soak._summarize(
        recorder,
        timeline,
        np.zeros(16000, dtype=np.float32),
        [],
        False,
        wall=600.0,
        server_cpu_s=1200.0,
        vad_cpu_s=12.0,
        freqs=[2000.0] * 600,
        system={"temp_max_c": 90.0},
        peak_rss_mb=500.0,
    )


def test_summary_verdicts() -> None:
    recorder = soak._Recorder(jobs=jobs((10.0, 3.0), (10.0, 2.0), (5.0, None)))
    flat = [{"t": float(t), "queued_audio_s": 2.0, "busy": 1.0} for t in range(600)]
    result = summarize(recorder, flat)
    assert result["rtf"] == pytest.approx(0.25)  # failed jobs are not in the RTF
    assert result["verdict"] == {
        "rtf": True,
        "queue_trend": True,
        "cpu_frequency": True,
        "completed": True,
        "pass": True,
    }
    assert result["cpu"]["vad_core_share"] == pytest.approx(0.02)
    assert result["cpu"]["vad_within_n4"]
    assert result["cuts"]["incorrect"] is None  # no reference

    rising = [{"t": float(t), "queued_audio_s": t / 60, "busy": 1.0} for t in range(600)]
    slow = soak._Recorder(jobs=jobs((10.0, 6.0)), stopped_by=["IDLE"])
    result = summarize(slow, rising)
    assert not result["verdict"]["rtf"]
    assert not result["verdict"]["queue_trend"]  # 1 s/min
    assert not result["verdict"]["completed"]
    assert not result["verdict"]["pass"]
    text = soak.format_summary({**result, "config": CONFIG, "system": {"power_source": "AC"}})
    assert text.startswith("soak small-q8_0 t=4 audio_ctx=1000 on AC: FAIL")
    assert "stopped early" in text


CONFIG = {"model": "small-q8_0", "threads": 4, "audio_ctx": 1000}


def test_cli_soak_uses_the_production_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(long_wav: Path, out_dir: Path, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs, long_wav=long_wav)
        return {"path": "x", "config": CONFIG}

    monkeypatch.setattr(soak, "run_soak", fake_run)
    monkeypatch.setattr(soak, "format_summary", lambda result: "summary")
    assert cli.main(["bench", "--soak", "--dataset", "/data", "--duration", "60"]) == 0
    assert (seen["model"], seen["threads"], seen["audio_ctx"], seen["duration_s"]) == (
        "small-q8_0",
        4,
        1000,
        60.0,
    )
    assert seen["long_wav"] == Path("/data/long/001.wav")
    assert cli.main(["bench", "--soak", "--threads", "8", "--audio-ctx", "0"]) == 0
    assert (seen["threads"], seen["audio_ctx"]) == (8, 0)
