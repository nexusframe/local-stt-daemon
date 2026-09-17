import os
from pathlib import Path

import numpy as np

from local_stt.bench import runner


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
    cfg = runner.BenchConfig("small", 4, False)
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
    assert runner.BenchConfig("small-q5_1", 8, True, 5).key == "small-q5_1|t8|actx1|bs5"
