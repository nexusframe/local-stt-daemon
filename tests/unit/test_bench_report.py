import json
from pathlib import Path
from typing import Any

import pytest

from local_stt.bench import report
from local_stt.stt.parakeet import PARAKEET_MODEL


def cfg(model: str, threads: int = 4, ctx: int = 0, beam: int = -1) -> dict[str, Any]:
    return {"model": model, "threads": threads, "audio_ctx": ctx, "beam_size": beam}


def key(c: dict[str, Any]) -> str:
    return f"{c['model']}|t{c['threads']}|ctx{c['audio_ctx']}|bs{c['beam_size']}"


def file_line(
    c: dict[str, Any],
    *,
    stage: int = 1,
    group: str = "medium",
    stem: str = "001",
    rep: int = 1,
    word_errors: int = 0,
    ref_words: int = 10,
    latency: float = 1.0,
    result: str = "ok",
) -> dict[str, Any]:
    return {
        "type": "file",
        "stage": stage,
        "config_key": key(c),
        "config": c,
        "group": group,
        "stem": stem,
        "rep": rep,
        "result": result,
        "reference": "jeden dwa trzy",
        "word_errors": word_errors,
        "ref_words": ref_words,
        "char_errors": word_errors,
        "ref_chars": ref_words * 5,
        "text_ready_s": latency,
        "rtf": 0.5,
        "numeric_mismatch": False,
    }


def config_line(c: dict[str, Any], stage: int = 1, rss: float = 400.0) -> dict[str, Any]:
    return {
        "type": "config",
        "stage": stage,
        "config_key": key(c),
        "config": c,
        "peak_rss_mb": rss,
        "cpu_util": 3.5,
        "temp_max_c": 80.0,
        "freq_min_mhz": 1800.0,
    }


def test_wer_runs_mean_spread_and_latency_percentiles() -> None:
    c = cfg("small-q5_1")
    lines = [config_line(c)]
    # two files x two runs; latencies: file 001 medians 1.0, file 002 median 3.0
    lines += [
        file_line(c, stem="001", rep=1, word_errors=1, latency=0.8),
        file_line(c, stem="001", rep=2, word_errors=3, latency=1.2),
        file_line(c, stem="002", rep=1, word_errors=1, latency=3.0),
        file_line(c, stem="002", rep=2, word_errors=1, latency=3.0),
    ]
    s = report.summarize(lines, stage=1)[key(c)]
    assert s.wer_runs == [pytest.approx(0.1), pytest.approx(0.2)]
    assert s.wer_mean == pytest.approx(0.15) and s.wer_spread == pytest.approx(0.1)
    assert s.p50_text_ready_s == pytest.approx(2.0)
    assert s.p90_text_ready_s == pytest.approx(2.8)
    assert s.peak_rss_mb == 400.0 and s.files == 2


def test_failed_file_counts_all_reference_words_as_errors() -> None:
    c = cfg("small")
    s = report.summarize([file_line(c, result="error")], stage=1)[key(c)]
    assert s.wer_mean == 1.0 and s.errors == 1


def test_elimination_requires_all_configs_slow_and_spares_control() -> None:
    slow, mixed, base = cfg("medium-q5_0"), cfg("small"), cfg("base-q5_1")
    lines = [
        file_line(slow, latency=7.0),
        file_line({**slow, "threads": 8}, latency=6.5),
        file_line(mixed, latency=7.0),
        file_line({**mixed, "audio_ctx": 1000}, latency=5.0),
        file_line(base, latency=9.0),
    ]
    assert report.eliminated_models(report.summarize(lines, 1), "base-q5_1") == {"medium-q5_0"}


def test_full_view_reuses_stage1_medium_only_for_stage2_configs() -> None:
    kept, dropped = cfg("small"), cfg("medium-q5_0")
    lines = [
        file_line(kept, stage=1, group="medium"),
        file_line(kept, stage=2, group="long_utt", word_errors=10),
        file_line(dropped, stage=1),
    ]
    full = report.summarize(lines, stage=None)
    assert set(full) == {key(kept)}
    assert full[key(kept)].group_wer == {"long_utt": 1.0, "medium": 0.0}
    assert full[key(kept)].wer_mean == pytest.approx(0.5)


def _stats(
    model: str,
    wer_mean: float,
    p90: float,
    rss: float = 400.0,
    threads: int = 4,
    ctx: int = 0,
) -> report.ConfigStats:
    s = report.ConfigStats(key(cfg(model, threads, ctx)), model, threads, ctx, -1)
    s.wer_runs, s.p90_text_ready_s, s.peak_rss_mb = [wer_mean], p90, rss
    return s


def test_select_applies_n1_audio_ctx_rule_and_n2_filter() -> None:
    stats = [
        _stats("base-q5_1", 0.30, 1.0),
        _stats("large-v3-turbo-q5_0", 0.05, 4.0, rss=1200),
        _stats("small", 0.10, 6.0),
        _stats("small", 0.125, 4.0, ctx=1000),  # +2.5 pp vs full window -> excluded
        _stats("small-q5_1", 0.12, 4.8),
        _stats("small-q5_1", 0.125, 3.0, ctx=1000),  # +0.5 pp -> allowed
    ]
    selection = report.select({s.key: s for s in stats}, control="base-q5_1")
    assert [(s.model, s.audio_ctx) for s in selection.provisional] == [
        ("small-q5_1", 1000),
        ("small-q5_1", 0),
    ]  # within 1 pp WER and equal RAM -> lower latency wins
    reasons = selection.excluded
    assert "control" in reasons[key(cfg("base-q5_1"))]
    assert "N1" in reasons[key(cfg("large-v3-turbo-q5_0"))]
    assert "audio_ctx" in reasons[key(cfg("small", ctx=1000))]
    assert "> 5.0 s" in reasons[key(cfg("small"))]


def test_select_applies_n1_per_engine() -> None:
    stats = [
        _stats(PARAKEET_MODEL, 0.056, 0.9, rss=2188),  # under 2.2 GB -> allowed
        _stats(PARAKEET_MODEL, 0.056, 1.2, rss=2300, threads=8),
        _stats("small-q8_0", 0.074, 2.8, rss=1100),
    ]
    selection = report.select({s.key: s for s in stats}, control="base-q5_1")
    assert [(s.model, s.threads) for s in selection.provisional] == [(PARAKEET_MODEL, 4)]
    reasons = selection.excluded
    assert "> 2.2 GB (N1)" in reasons[key(cfg(PARAKEET_MODEL, threads=8))]
    assert "> 1 GB (N1)" in reasons[key(cfg("small-q8_0"))]


def test_render_names_the_engine_in_the_provisional_default() -> None:
    lines = []
    for c, rss, errors in ((cfg(PARAKEET_MODEL), 1585.0, 0), (cfg("small-q8_0"), 450.0, 2)):
        lines += [config_line(c, stage=2, rss=rss), file_line(c, stage=2, word_errors=errors)]
    text = report.render(lines, {"repeats": 1})
    assert '`stt.engine = "parakeet"`, `stt.threads = 4`. ' in text

    lines = [config_line(cfg("small-q8_0"), stage=2), file_line(cfg("small-q8_0"), stage=2)]
    text = report.render(lines, {"repeats": 1})
    assert '`stt.engine = "whisper-server"`, `stt.model = "small-q8_0"`' in text


def test_render_says_when_no_configuration_is_left() -> None:
    c = cfg("small-q8_0", ctx=1000)  # no audio_ctx=0 run -> excluded
    text = report.render([config_line(c, stage=2), file_line(c, stage=2)], {"repeats": 1})
    assert "- none: no configuration passes the production filter." in text


def test_rank_tie_breaks_ram_then_latency_then_four_threads() -> None:
    a = _stats("small-q5_1", 0.120, 2.00, rss=300, threads=8)
    b = _stats("small-q5_1", 0.121, 2.05, rss=305, threads=4)  # ties with a on RAM and latency
    c = _stats("small", 0.125, 1.00, rss=500)  # WER tie but more RAM
    d = _stats("medium-q5_0", 0.09, 2.40, rss=900)  # clearly better WER
    assert [s.key for s in report.rank([a, b, c, d])] == [d.key, b.key, a.key, c.key]


def test_render_mentions_unconfirmed_n2_and_interim_corpus() -> None:
    c = cfg("small-q5_1")
    lines = [
        config_line(c),
        file_line(c, latency=6.0),
        config_line(c, stage=2),
        file_line(c, stage=2, group="long_utt", latency=6.0),
        {"type": "whisper_bench", "model": "small-q5_1", "encode_ms": 4200.0},
    ]
    text = report.render(lines, {"dataset_is_public_interim": True, "repeats": 1})
    assert "N2 is not confirmed" in text and "Interim public corpus" in text
    assert "No configuration meets p90 text_ready ≤ 5.0 s" in text
    assert "| `small-q5_1` | 4200 |" in text


def test_legacy_dynamic_audio_ctx_results_still_render() -> None:
    legacy = {"model": "small-q5_1", "threads": 4, "dynamic_audio_ctx": True, "beam_size": -1}
    line = file_line(cfg("small-q5_1")) | {
        "config": legacy,
        "config_key": "small-q5_1|t4|actx1|bs-1",
    }
    stats = report.summarize([line], stage=1)
    assert next(iter(stats.values())).audio_ctx == report.LEGACY_PER_REQUEST
    assert "ctx=per-request" in report.render([line], {})


def test_whole_corpus_uses_stage2_medium_measurements_not_stage1() -> None:
    c = cfg("small-q8_0", ctx=1000)
    lines = [
        file_line(c, latency=9.0, word_errors=5),  # stage 1, measured without interleaving
        file_line(c, stage=2, latency=3.0),
        file_line(c, stage=2, group="long_utt", stem="002", latency=6.0),
    ]
    s = report.summarize(lines, stage=None)[key(c)]
    assert s.p90_text_ready_s == pytest.approx(3.0)
    assert s.wer_mean == 0.0


def test_whole_corpus_falls_back_to_stage1_medium_for_old_runs() -> None:
    c = cfg("small-q8_0")
    lines = [
        file_line(c, latency=5.5),
        file_line(c, stage=2, group="long_utt", stem="002", latency=6.0),
    ]
    s = report.summarize(lines, stage=None)[key(c)]
    assert s.p90_text_ready_s == pytest.approx(5.5)


# --- latest results per model (models list --bench, task 3.1) ------------------------------


def write_run(
    bench_dir: Path, name: str, timestamp: str, lines: list[dict[str, Any]], public: bool = False
) -> None:
    run = bench_dir / name
    run.mkdir(parents=True)
    run.joinpath("results.jsonl").write_text("".join(json.dumps(ln) + "\n" for ln in lines))
    info = {"timestamp": timestamp, "dataset_is_public_interim": public}
    run.joinpath("system.json").write_text(json.dumps(info))


def whole_corpus_lines(c: dict[str, Any], word_errors: int = 0) -> list[dict[str, Any]]:
    return [file_line(c, stage=2, word_errors=word_errors), config_line(c, stage=2)]


def latest(bench_dir: Path) -> dict[str, report.ModelResult]:
    return report.latest_results(bench_dir, threads=4, audio_ctx=1000, beam_size=-1)


def test_latest_results_prefer_the_config_file_configuration(tmp_path: Path) -> None:
    exact, other = cfg("small-q8_0", ctx=1000), cfg("small-q8_0", ctx=0)
    write_run(
        tmp_path,
        "r1",
        "2026-10-03T09:10:49+00:00",
        whole_corpus_lines(other, word_errors=1) + whole_corpus_lines(exact, word_errors=2),
    )
    r = latest(tmp_path)["small-q8_0"]
    assert (r.stats.audio_ctx, r.exact, r.whole_corpus) == (1000, True, True)
    assert (r.date, r.corpus, r.run) == ("2026-10-03", "A", "r1")
    assert r.stats.wer_mean == pytest.approx(0.2)
    assert r.stats.peak_rss_mb == 400.0


def test_latest_results_fall_back_to_the_closest_configuration(tmp_path: Path) -> None:
    # audio_ctx matters more than threads; neither matches here, so threads decide.
    lines = whole_corpus_lines(cfg("m", threads=8, ctx=0)) + whole_corpus_lines(cfg("m", ctx=0))
    lines += whole_corpus_lines(cfg("m", threads=8, ctx=1000, beam=5))
    write_run(tmp_path, "r1", "2026-09-17T05:29:00+00:00", lines, public=True)
    r = latest(tmp_path)["m"]
    assert (r.stats.threads, r.stats.audio_ctx, r.stats.beam_size) == (8, 1000, 5)
    assert not r.exact and r.corpus == "B"


def test_latest_results_newest_run_wins_but_stage1_never_replaces_whole_corpus(
    tmp_path: Path,
) -> None:
    c = cfg("small-q8_0", ctx=1000)
    write_run(tmp_path, "old", "2026-09-17T16:28:19+00:00", whole_corpus_lines(c, 5), public=True)
    write_run(tmp_path, "new", "2026-10-03T09:10:49+00:00", whole_corpus_lines(c, 1))
    write_run(tmp_path, "partial", "2026-10-04T08:00:00+00:00", [file_line(c), config_line(c)])
    m = cfg("medium-q5_0", ctx=1000)
    write_run(tmp_path, "medium", "2026-10-03T10:00:00+00:00", [file_line(m), config_line(m)])
    (tmp_path / "soak-2026-10-04").mkdir()  # no results.jsonl: skipped
    results = latest(tmp_path)
    assert results["small-q8_0"].run == "new"
    assert results["small-q8_0"].whole_corpus
    assert results["medium-q5_0"].run == "medium"
    assert not results["medium-q5_0"].whole_corpus


def test_latest_results_skip_directories_not_written_by_bench(tmp_path: Path) -> None:
    c = cfg("small-q8_0", ctx=1000)
    write_run(tmp_path, "run", "2026-10-03T09:10:49+00:00", whole_corpus_lines(c))
    foreign = tmp_path / "parakeet-2026-10-07"  # an exploration script's output, no system.json
    foreign.mkdir()
    foreign.joinpath("results.jsonl").write_text(json.dumps({"type": "file", "wer": 0.1}) + "\n")
    assert list(latest(tmp_path)) == ["small-q8_0"]


def test_latest_results_without_runs(tmp_path: Path) -> None:
    assert latest(tmp_path / "missing") == {}


def test_cli_models_list_bench_uses_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from local_stt.bench import runner
    from local_stt.cli import main

    c = cfg("small-q5_1", threads=8, ctx=0)
    write_run(tmp_path / "bench", "r1", "2026-10-03T09:10:49+00:00", whole_corpus_lines(c))
    monkeypatch.setattr(runner, "BENCH_DIR", tmp_path / "bench")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[stt]\nengine = "whisper-server"\nmodel = "small-q5_1"\nthreads = 8\naudio_ctx = 0\n'
        f'models_dir = "{tmp_path}"\n'
    )
    assert main(["models", "list", "--bench", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("* small-q5_1"))
    assert row.endswith("t=8 ctx=full greedy    2026-10-03 A")
    assert "config file (t=8 ctx=full greedy)" in out


def test_parakeet_result_matches_any_audio_ctx_and_beam(tmp_path: Path) -> None:
    # task 4.5: Parakeet runs with ctx 0 and greedy only; that is no mismatch with the config file
    p = cfg(PARAKEET_MODEL, ctx=0)
    write_run(tmp_path, "r1", "2026-10-08T09:00:00+00:00", whole_corpus_lines(p))
    r = report.latest_results(tmp_path, threads=4, audio_ctx=1000, beam_size=5)[PARAKEET_MODEL]
    assert r.exact
    assert report.config_label(r.stats) == "t=4"
    other_threads = report.latest_results(tmp_path, threads=8, audio_ctx=1000, beam_size=-1)
    assert not other_threads[PARAKEET_MODEL].exact


def test_cli_models_list_bench_marks_parakeet_when_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from local_stt.bench import runner
    from local_stt.cli import main

    write_run(
        tmp_path / "bench", "r1", "2026-10-08T09:00:00+00:00",
        whole_corpus_lines(cfg(PARAKEET_MODEL)),
    )  # fmt: skip
    monkeypatch.setattr(runner, "BENCH_DIR", tmp_path / "bench")
    config = tmp_path / "config.toml"
    config.write_text(f'[stt]\nmodels_dir = "{tmp_path}"\n')  # engine: parakeet (default)
    assert main(["models", "list", "--bench", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if PARAKEET_MODEL in line)
    assert row.startswith(f"* {PARAKEET_MODEL}")
    assert "missing" in row and row.endswith("t=4                    2026-10-08 A")
    assert any(line.startswith("  small-q8_0") for line in out.splitlines())
