import os
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from local_stt.bench import runner
from local_stt.stt import whisper_server as ws
from local_stt.stt.parakeet import PARAKEET_MODEL, TemporaryParakeetServer


def test_rms_gate_windows() -> None:
    silence = np.zeros(16000 * 5, dtype=np.float32)
    assert not runner.has_speech_rms(silence)
    speech_burst = silence.copy()
    speech_burst[40000:41600] = 0.1  # 100 ms at about -20 dBFS inside 5 s of silence
    assert runner.has_speech_rms(speech_burst)
    assert not runner.has_speech_rms(np.zeros(100, dtype=np.float32))


def test_process_measurements_on_own_process() -> None:
    assert runner.process_cpu_seconds(os.getpid()) > 0
    assert runner.process_peak_rss_mb(os.getpid()) > 10


def test_load_dataset_layout(tmp_path: Path) -> None:
    for group, stem in [("medium", "002"), ("medium", "001"), ("short", "005")]:
        (tmp_path / group).mkdir(exist_ok=True)
        (tmp_path / group / f"{stem}.wav").write_bytes(b"")
        (tmp_path / group / f"{stem}.txt").write_text(f" tekst {stem}\n")
    (tmp_path / "medium" / "003.wav").write_bytes(b"")  # no transcript -> skipped
    items = runner.load_dataset(tmp_path)
    assert [(i.group, i.stem, i.text) for i in items] == [
        ("short", "005", "tekst 005"),
        ("medium", "001", "tekst 001"),
        ("medium", "002", "tekst 002"),
    ]


def test_results_resume(tmp_path: Path) -> None:
    cfg = runner.BenchConfig("small", 4, 0)
    item = runner.Item("medium", "001", tmp_path / "001.wav", "tekst")
    results = runner.Results(tmp_path)
    results.append(
        {
            "type": "file",
            "stage": 1,
            "config_key": cfg.key,
            "group": "medium",
            "stem": "001",
            "rep": 1,
        }
    )
    reopened = runner.Results(tmp_path)
    assert reopened.done(1, cfg, item, 1)
    assert not reopened.done(1, cfg, item, 2)
    assert not reopened.done(2, cfg, item, 1)


def test_config_key() -> None:
    assert runner.BenchConfig("small-q5_1", 8, 1000, 5).key == "small-q5_1|t8|ctx1000|bs5"


def _item(group: str, stem: str) -> runner.Item:
    return runner.Item(group, stem, Path(f"{group}/{stem}.wav"), "tekst")


def test_interleave_round_robin_over_groups() -> None:
    items = [
        _item("short", "001"),
        _item("short", "002"),
        _item("medium", "003"),
        _item("medium", "004"),
        _item("medium", "005"),
        _item("long_utt", "006"),
    ]
    assert [(i.group, i.stem) for i in runner.interleave(items)] == [
        ("short", "001"),
        ("medium", "003"),
        ("long_utt", "006"),
        ("short", "002"),
        ("medium", "004"),
        ("medium", "005"),
    ]


def test_run_stage2_measures_whole_corpus_interleaved_on_one_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset, models_dir = tmp_path / "corpus", tmp_path / "models"
    for group, stem in [("medium", "001"), ("medium", "002"), ("long_utt", "003")]:
        (dataset / group).mkdir(parents=True, exist_ok=True)
        (dataset / group / f"{stem}.wav").write_bytes(b"")
        (dataset / group / f"{stem}.txt").write_text("tekst")
    models_dir.mkdir()
    for model in ("base-q5_1", "small-q8_0", "medium-q5_0"):
        (models_dir / f"ggml-{model}.bin").write_bytes(b"")
    stage1_latency = {"base-q5_1": 1.0, "small-q8_0": 4.0, "medium-q5_0": 20.0}
    calls: list[tuple[int, str, list[tuple[str, str]]]] = []

    def fake_run_config(
        stage: int,
        cfg: runner.BenchConfig,
        items: list[runner.Item],
        repeats: int,
        results: runner.Results,
        models_dir: Path,
    ) -> None:
        calls.append((stage, cfg.key, [(it.group, it.stem) for it in items]))
        for it in items:
            results.append(
                {
                    "type": "file",
                    "stage": stage,
                    "config_key": cfg.key,
                    "config": asdict(cfg),
                    "group": it.group,
                    "stem": it.stem,
                    "rep": 1,
                    "result": "ok",
                    "word_errors": 1,
                    "ref_words": 10,
                    "char_errors": 1,
                    "ref_chars": 50,
                    "text_ready_s": stage1_latency[cfg.model],
                    "rtf": 0.5,
                    "reference": "tekst",
                    "numeric_mismatch": False,
                }
            )

    monkeypatch.setattr(runner, "run_config", fake_run_config)
    monkeypatch.setattr(runner, "active_engine_units", lambda: [])
    monkeypatch.setattr(runner, "system_info", lambda dataset: {})
    code = runner.run(
        dataset,
        tmp_path / "out",
        models=["base-q5_1", "small-q8_0", "medium-q5_0"],
        threads=[4],
        audio_ctx=[0, 1000],
        repeats=1,
        quick=False,
        beam=True,
        sanity=False,
        models_dir=models_dir,
    )

    assert code == 0
    medium_only = [("medium", "001"), ("medium", "002")]
    interleaved = [("medium", "001"), ("long_utt", "003"), ("medium", "002")]
    assert [(s, k) for s, k, items in calls if s == 1 and items == medium_only] == [
        (1, f"{m}|t4|ctx{a}|bs-1")
        for m in ("base-q5_1", "small-q8_0", "medium-q5_0")
        for a in (0, 1000)
    ]
    # medium-q5_0 is eliminated (p50 > 6 s everywhere); survivors get the whole corpus,
    # medium included, in one interleaved order; beam search uses the same order.
    assert [(s, k, items) for s, k, items in calls if s == 2] == [
        (2, "base-q5_1|t4|ctx0|bs-1", interleaved),
        (2, "base-q5_1|t4|ctx1000|bs-1", interleaved),
        (2, "small-q8_0|t4|ctx0|bs-1", interleaved),
        (2, "small-q8_0|t4|ctx1000|bs-1", interleaved),
        (2, "small-q8_0|t4|ctx0|bs5", interleaved),
    ]


def test_parakeet_runs_once_per_thread_count() -> None:
    # task 4.5: Parakeet has no audio_ctx or beam search
    keys = [c.key for c in runner._configs(["small", PARAKEET_MODEL], [4, 8], [0, 1000])]
    assert keys == [
        "small|t4|ctx0|bs-1", "small|t4|ctx1000|bs-1", "small|t8|ctx0|bs-1",
        "small|t8|ctx1000|bs-1", f"{PARAKEET_MODEL}|t4|ctx0|bs-1", f"{PARAKEET_MODEL}|t8|ctx0|bs-1",
    ]  # fmt: skip
    assert PARAKEET_MODEL in runner.DEFAULT_MODELS


def test_model_paths_and_servers(tmp_path: Path) -> None:
    assert runner.model_path("small", tmp_path) == tmp_path / "ggml-small.bin"
    assert runner.model_path(PARAKEET_MODEL, tmp_path) == tmp_path / PARAKEET_MODEL
    parakeet = runner._server(runner.BenchConfig(PARAKEET_MODEL, 8, 0), tmp_path)
    assert isinstance(parakeet, TemporaryParakeetServer) and parakeet.threads == 8
    whisper = runner._server(runner.BenchConfig("small", 4, 1000, 5), tmp_path)
    assert type(whisper) is ws.TemporaryWhisperServer
    assert (whisper.audio_ctx, whisper.beam_size) == (1000, 5)


def test_concurrency_error_names_the_running_engine_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "active_engine_units", lambda: [])
    assert runner.concurrency_error() is None
    monkeypatch.setattr(runner, "active_engine_units", lambda: ["local-stt-engine.service"])
    error = runner.concurrency_error()
    assert error is not None
    assert "local-stt-engine.service running" in error
    assert "systemctl --user stop local-stt local-stt-engine)" in error


def test_run_with_parakeet_skips_beam_and_whisper_bench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset, models_dir = tmp_path / "corpus", tmp_path / "models"
    (dataset / "medium").mkdir(parents=True)
    (dataset / "medium/001.wav").write_bytes(b"")
    (dataset / "medium/001.txt").write_text("tekst")
    (models_dir / PARAKEET_MODEL).mkdir(parents=True)  # a directory, not ggml-*.bin
    (models_dir / "ggml-small-q8_0.bin").write_bytes(b"")
    calls: list[tuple[int, str]] = []
    sanity: list[str] = []

    def fake_run_config(
        stage: int,
        cfg: runner.BenchConfig,
        items: list[runner.Item],
        repeats: int,
        results: runner.Results,
        models_dir: Path,
    ) -> None:
        calls.append((stage, cfg.key))
        results.append(
            {"type": "file", "stage": stage, "config_key": cfg.key, "config": asdict(cfg),
             "group": "medium", "stem": "001", "rep": 1, "result": "ok", "word_errors": 0,
             "ref_words": 1, "char_errors": 0, "ref_chars": 5, "text_ready_s": 1.0, "rtf": 0.1,
             "reference": "tekst", "numeric_mismatch": False}
        )  # fmt: skip

    monkeypatch.setattr(runner, "run_config", fake_run_config)
    monkeypatch.setattr(runner, "run_whisper_bench", lambda m, d, r: sanity.append(m))
    monkeypatch.setattr(runner, "active_engine_units", lambda: [])
    monkeypatch.setattr(runner, "system_info", lambda dataset: {})
    code = runner.run(
        dataset,
        tmp_path / "out",
        models=["small-q8_0", PARAKEET_MODEL],
        threads=[4],
        audio_ctx=[0, 1000],
        repeats=1,
        quick=False,
        beam=True,
        sanity=True,
        models_dir=models_dir,
    )

    assert code == 0
    p = f"{PARAKEET_MODEL}|t4|ctx0|bs-1"
    stage1 = ["small-q8_0|t4|ctx0|bs-1", "small-q8_0|t4|ctx1000|bs-1", p]
    assert calls == [(1, k) for k in stage1] + [(2, k) for k in stage1] + [
        (2, "small-q8_0|t4|ctx0|bs5")
    ]
    assert sanity == ["small-q8_0"]
