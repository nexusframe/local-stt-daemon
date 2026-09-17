from typing import Any

import pytest

from local_stt.bench import report


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
