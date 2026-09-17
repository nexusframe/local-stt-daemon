"""Benchmark aggregation, selection rule and Markdown report (docs/13-benchmark.md §13.3-13.6).

Conventions:
- WER/CER per run are corpus-level (total edits / total reference length); mean and spread
  (max - min) are taken over runs.
- Latency percentiles: per file, the median over runs; then p50/p90 over the medium-group files
  (linear interpolation, numpy default).
- Ties in the selection rule: WER within 1 pp, then peak RAM within 5 %, then p90 latency within 5 %
  (prefer 4 threads), each time falling back to the lowest value.
"""

import json
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from local_stt.bench import wer

MEDIUM = "medium"
ELIMINATION_P50_S = 6.0
N1_SERVER_RSS_MB = 1024.0
N2_P90_S = 2.5
AUDIO_CTX_MAX_WER_DELTA = 0.01  # 1.0 pp
WER_TIE = 0.01
RELATIVE_TIE = 0.05


@dataclass
class ConfigStats:
    key: str
    model: str
    threads: int
    dynamic_audio_ctx: bool
    beam_size: int
    files: int = 0
    wer_runs: list[float] = field(default_factory=list)
    cer_runs: list[float] = field(default_factory=list)
    group_wer: dict[str, float] = field(default_factory=dict)
    p50_text_ready_s: float | None = None  # medium group
    p90_text_ready_s: float | None = None
    rtf_mean: float | None = None
    numeric_mismatch_files: int = 0
    errors: int = 0
    no_speech: int = 0
    peak_rss_mb: float | None = None
    cpu_util: float | None = None
    temp_max_c: float | None = None
    freq_min_mhz: float | None = None

    @property
    def wer_mean(self) -> float:
        return statistics.fmean(self.wer_runs) if self.wer_runs else float("nan")

    @property
    def wer_spread(self) -> float:
        return max(self.wer_runs) - min(self.wer_runs) if self.wer_runs else float("nan")

    @property
    def cer_mean(self) -> float:
        return statistics.fmean(self.cer_runs) if self.cer_runs else float("nan")


def load_results(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with (run_dir / "results.jsonl").open(encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    info_path = run_dir / "system.json"
    info = json.loads(info_path.read_text()) if info_path.is_file() else {}
    return lines, info


def summarize(lines: list[dict[str, Any]], stage: int | None) -> dict[str, ConfigStats]:
    """Per-configuration statistics.

    stage=1: stage-1 measurements only (medium group).
    stage=None: the whole corpus — all measurements of configurations that reached stage 2
    (stage-2 groups plus the medium results reused from stage 1).
    """
    file_lines = [ln for ln in lines if ln.get("type") == "file"]
    if stage == 1:
        selected = [ln for ln in file_lines if ln["stage"] == 1]
        config_lines = [ln for ln in lines if ln.get("type") == "config" and ln["stage"] == 1]
    else:
        in_stage2 = {ln["config_key"] for ln in file_lines if ln["stage"] == 2}
        selected = [ln for ln in file_lines if ln["config_key"] in in_stage2]
        config_lines = [
            ln for ln in lines if ln.get("type") == "config" and ln["config_key"] in in_stage2
        ]

    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ln in selected:
        by_config[ln["config_key"]].append(ln)

    stats: dict[str, ConfigStats] = {}
    for key, rows in by_config.items():
        cfg = rows[0]["config"]
        s = ConfigStats(
            key, cfg["model"], cfg["threads"], cfg["dynamic_audio_ctx"], cfg["beam_size"]
        )
        _fill_quality(s, rows)
        _fill_latency(s, rows)
        runs = [c for c in config_lines if c["config_key"] == key]
        rss = [c["peak_rss_mb"] for c in runs if c.get("peak_rss_mb") is not None]
        cpu = [c["cpu_util"] for c in runs if c.get("cpu_util") is not None]
        temps = [c["temp_max_c"] for c in runs if c.get("temp_max_c") is not None]
        freqs = [c["freq_min_mhz"] for c in runs if c.get("freq_min_mhz") is not None]
        s.peak_rss_mb = max(rss) if rss else None
        s.cpu_util = statistics.fmean(cpu) if cpu else None
        s.temp_max_c = max(temps) if temps else None
        s.freq_min_mhz = min(freqs) if freqs else None
        stats[key] = s
    return stats


def _counts(row: dict[str, Any]) -> wer.ErrorCounts:
    if row["result"] == "ok":
        return wer.ErrorCounts(
            row["word_errors"], row["ref_words"], row["char_errors"], row["ref_chars"]
        )
    # no speech detected or request failed: every reference word is a deletion
    reference = wer.normalize(row["reference"])
    return wer.ErrorCounts(
        len(reference.split()), len(reference.split()), len(reference), len(reference)
    )


def _fill_quality(s: ConfigStats, rows: list[dict[str, Any]]) -> None:
    s.files = len({(r["group"], r["stem"]) for r in rows})
    s.errors = sum(r["result"] == "error" for r in rows)
    s.no_speech = sum(r["result"] == "no_speech" for r in rows)
    s.numeric_mismatch_files = len(
        {(r["group"], r["stem"]) for r in rows if r.get("numeric_mismatch")}
    )
    for rep in sorted({r["rep"] for r in rows}):
        rep_rows = [r for r in rows if r["rep"] == rep]
        run_wer, run_cer = wer.corpus_error_rates([_counts(r) for r in rep_rows])
        s.wer_runs.append(run_wer)
        s.cer_runs.append(run_cer)
    for group in sorted({r["group"] for r in rows}):
        group_rows = [r for r in rows if r["group"] == group]
        reps = sorted({r["rep"] for r in group_rows})
        s.group_wer[group] = statistics.fmean(
            wer.corpus_error_rates([_counts(r) for r in group_rows if r["rep"] == rep])[0]
            for rep in reps
        )
    rtfs = [r["rtf"] for r in rows if r.get("rtf") is not None]
    s.rtf_mean = statistics.fmean(rtfs) if rtfs else None


def _fill_latency(s: ConfigStats, rows: list[dict[str, Any]]) -> None:
    per_file: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["group"] == MEDIUM and r["result"] == "ok":
            per_file[r["stem"]].append(r["text_ready_s"])
    medians = [statistics.median(v) for v in per_file.values()]
    if medians:
        s.p50_text_ready_s = float(np.percentile(medians, 50))
        s.p90_text_ready_s = float(np.percentile(medians, 90))


def eliminated_models(stage1: dict[str, ConfigStats], control: str) -> set[str]:
    """Models whose p50 text_ready_s exceeds 6 s in every stage-1 configuration (13.4 stage 1)."""
    by_model: dict[str, list[ConfigStats]] = defaultdict(list)
    for s in stage1.values():
        by_model[s.model].append(s)
    return {
        model
        for model, configs in by_model.items()
        if model != control
        and all(
            c.p50_text_ready_s is None or c.p50_text_ready_s > ELIMINATION_P50_S for c in configs
        )
    }


def top_models_by_wer(
    stage1: dict[str, ConfigStats], models: Iterable[str], n: int, exclude: set[str]
) -> list[str]:
    best: dict[str, float] = {}
    for s in stage1.values():
        if s.model in models and s.model not in exclude:
            best[s.model] = min(best.get(s.model, float("inf")), s.wer_mean)
    return sorted(best, key=lambda m: best[m])[:n]


@dataclass
class Selection:
    excluded: dict[str, str]  # config key -> reason
    production: list[ConfigStats]
    provisional: list[ConfigStats]  # ranked, best first


def select(full: dict[str, ConfigStats], control: str) -> Selection:
    """13.5 stage-0 rule: production filter, provisional N2 filter on text_ready, then ranking."""
    excluded: dict[str, str] = {}
    production = []
    for s in full.values():
        if s.model == control:
            excluded[s.key] = "control model (not a production candidate)"
        elif s.peak_rss_mb is None or s.peak_rss_mb > N1_SERVER_RSS_MB:
            excluded[s.key] = f"peak server RSS {_fmt(s.peak_rss_mb, '.0f')} MB > 1 GB (N1)"
        elif s.dynamic_audio_ctx and not _audio_ctx_allowed(s, full):
            excluded[s.key] = (
                "dynamic_audio_ctx costs > 1.0 pp WER (or no audio_ctx=false counterpart)"
            )
        else:
            production.append(s)
    provisional = []
    for s in production:
        if s.p90_text_ready_s is not None and s.p90_text_ready_s <= N2_P90_S:
            provisional.append(s)
        else:
            excluded[s.key] = f"p90 text_ready {_fmt(s.p90_text_ready_s, '.2f')} s > 2.5 s"
    return Selection(excluded, production, rank(provisional))


def _audio_ctx_allowed(s: ConfigStats, full: dict[str, ConfigStats]) -> bool:
    counterpart = next(
        (
            c
            for c in full.values()
            if (c.model, c.threads, c.beam_size, c.dynamic_audio_ctx)
            == (s.model, s.threads, s.beam_size, False)
        ),
        None,
    )
    return counterpart is not None and s.wer_mean - counterpart.wer_mean <= AUDIO_CTX_MAX_WER_DELTA


def rank(candidates: list[ConfigStats]) -> list[ConfigStats]:
    remaining, ranked = list(candidates), []
    while remaining:
        best_wer = min(c.wer_mean for c in remaining)
        tier = [c for c in remaining if c.wer_mean - best_wer < WER_TIE]
        min_ram = min(c.peak_rss_mb or 0.0 for c in tier)
        tier = [c for c in tier if (c.peak_rss_mb or 0.0) <= min_ram * (1 + RELATIVE_TIE)]
        min_lat = min(c.p90_text_ready_s or float("inf") for c in tier)
        tier = [
            c for c in tier if (c.p90_text_ready_s or float("inf")) <= min_lat * (1 + RELATIVE_TIE)
        ]
        winner = min(
            tier, key=lambda c: (c.threads != 4, c.p90_text_ready_s or float("inf"), c.wer_mean)
        )
        ranked.append(winner)
        remaining.remove(winner)
    return ranked


# --- Markdown ---


def _fmt(value: float | None, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def _pct(value: float) -> str:
    return f"{100 * value:.1f}"


def _config_label(s: ConfigStats) -> str:
    beam = "greedy" if s.beam_size < 0 else f"beam {s.beam_size}"
    return f"`{s.model}` t={s.threads} actx={'on' if s.dynamic_audio_ctx else 'off'} {beam}"


_COLUMNS = (
    "Configuration",
    "WER % (mean ± spread)",
    "CER %",
    "p50 / p90 text_ready s (medium)",
    "RTF",
    "peak RSS MB",
    "CPU",
    "max °C / min MHz",
    "numeric mismatch",
    "errors",
)
_GROUPS = ("short", "medium", "long_utt", "difficult")


def _row(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(stats: Iterable[ConfigStats], with_groups: bool) -> list[str]:
    columns = list(_COLUMNS) + ([f"WER {g} %" for g in _GROUPS] if with_groups else [])
    rows = [_row(columns), _row("---" for _ in columns)]
    for s in sorted(stats, key=lambda s: (s.model, s.beam_size, s.threads, s.dynamic_audio_ctx)):
        cells = [
            _config_label(s),
            f"{_pct(s.wer_mean)} ± {_pct(s.wer_spread)}",
            _pct(s.cer_mean),
            f"{_fmt(s.p50_text_ready_s, '.2f')} / {_fmt(s.p90_text_ready_s, '.2f')}",
            _fmt(s.rtf_mean, ".2f"),
            _fmt(s.peak_rss_mb, ".0f"),
            _fmt(s.cpu_util, ".1f"),
            f"{_fmt(s.temp_max_c, '.0f')} / {_fmt(s.freq_min_mhz, '.0f')}",
            str(s.numeric_mismatch_files),
            str(s.errors + s.no_speech),
        ]
        if with_groups:
            cells += [_pct(s.group_wer[g]) if g in s.group_wer else "—" for g in _GROUPS]
        rows.append(_row(cells))
    return rows


def _summary_line(s: ConfigStats) -> str:
    return (
        f"{_config_label(s)} — WER {_pct(s.wer_mean)} %, "
        f"p90 {_fmt(s.p90_text_ready_s, '.2f')} s, RSS {_fmt(s.peak_rss_mb, '.0f')} MB"
    )


def _header(info: dict[str, Any]) -> list[str]:
    def get(key: str) -> Any:
        return info.get(key, "?")

    decoding = json.dumps(info.get("decoding", {}), ensure_ascii=False)
    lines = [
        "# Benchmark results",
        "",
        f"- Run: {get('timestamp')}; CPU: {get('cpu')}; governor: {get('governor')}; "
        f"power: {get('power_source')}; kernel: {get('kernel')}",
        f"- whisper.cpp: {get('whisper_cpp')}; dataset: `{get('dataset')}`; "
        f"repeats: {get('repeats')}; decoding: `{decoding}`",
        "- text_ready_s = RMS gate + HTTP transcription + text normalization (excludes injection, "
        "capture finalization and queueing). **N2 is not confirmed by this report**: it requires "
        "`total` from the full daemon in v0.1 (13 §13.5).",
    ]
    if info.get("dataset_is_public_interim"):
        lines.append(
            "- ⚠️ **Interim public corpus** (13.2B): other speakers and studio-like audio, not the "
            "user's microphone. Results are provisional; rerun on corpus A before fixing defaults."
        )
    return lines


def _selection_section(selection: Selection) -> list[str]:
    out = ["", "## Provisional selection (13 §13.5, stage 0)", ""]
    if selection.provisional:
        out += ["Ranked candidates with p90 text_ready ≤ 2.5 s (best first):", ""]
        out += [f"{i}. {_summary_line(s)}" for i, s in enumerate(selection.provisional, 1)]
        best = selection.provisional[0]
        settings = [
            f'`stt.model = "{best.model}"`',
            f"`stt.threads = {best.threads}`",
            f"`stt.dynamic_audio_ctx = {str(best.dynamic_audio_ctx).lower()}`",
        ]
        if best.beam_size >= 0:
            settings.append(f"`stt.beam_size = {best.beam_size}`")
        out += [
            "",
            f"**Provisional default:** {', '.join(settings)}. "
            "N2 remains unconfirmed until the v0.1 measurement of `total`.",
        ]
    else:
        out.append("**No configuration meets p90 text_ready ≤ 2.5 s.** Best available compromises:")
        if selection.production:
            fastest = min(selection.production, key=lambda s: s.p90_text_ready_s or float("inf"))
            accurate = min(selection.production, key=lambda s: s.wer_mean)
            out += [
                f"- fastest: {_summary_line(fastest)}",
                f"- most accurate: {_summary_line(accurate)}",
                "",
                "Recommendation: N2 must not be changed silently; present these options and ask "
                "for an explicit decision on the latency target (13 §13.5).",
            ]
    if selection.excluded:
        out += ["", "Excluded configurations:", ""]
        out += [f"- `{key}`: {reason}" for key, reason in sorted(selection.excluded.items())]
    return out


def render(lines: list[dict[str, Any]], info: dict[str, Any], control: str = "base-q5_1") -> str:
    stage1 = summarize(lines, stage=1)
    full = summarize(lines, stage=None)
    eliminated = ", ".join(f"`{m}`" for m in sorted(eliminated_models(stage1, control))) or "none"
    out = [
        *_header(info),
        "",
        "## Stage 1 — medium group",
        "",
        *_table(stage1.values(), with_groups=False),
        "",
        f"Eliminated for speed (p50 > {ELIMINATION_P50_S:g} s in every configuration): "
        f"{eliminated}.",
    ]
    if full:
        out += ["", "## Stage 2 — whole corpus", "", *_table(full.values(), with_groups=True)]
        out += _selection_section(select(full, control))

    bench = [ln for ln in lines if ln.get("type") == "whisper_bench"]
    if bench:
        out += ["", "## whisper-bench (raw encoder, 4 threads)", "", "| Model | encode ms |"]
        out += ["|---|---|"] + [
            f"| `{b['model']}` | {_fmt(b.get('encode_ms'), '.0f')} |" for b in bench
        ]
    return "\n".join(out) + "\n"
