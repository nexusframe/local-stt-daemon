"""`local-stt` command-line entry point (subcommands: docs/10-cli-ipc-status.md §10.1)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from local_stt import __version__

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from local_stt.config import Config, SttConfig
    from local_stt.interfaces import Transcript
    from local_stt.stt.whisper_server import WhisperServerEngine

EXIT_CONFIG = 78  # EX_CONFIG (10 §10.1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-stt", description="Offline Polish speech-to-text daemon."
    )
    parser.add_argument("--version", action="version", version=f"local-stt {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    models = commands.add_parser("models", help="list, download and verify models")
    models_commands = models.add_subparsers(dest="models_command", metavar="ACTION", required=True)
    _add_config_option(models_commands.add_parser("list", help="models in models_dir"))
    pull = models_commands.add_parser("pull", help="download a model and verify its SHA256")
    _add_config_option(pull)
    pull.add_argument("name")
    verify = models_commands.add_parser("verify", help="verify checksums of downloaded models")
    _add_config_option(verify)

    transcribe = commands.add_parser(
        "transcribe", help="transcribe a 16 kHz mono 16-bit WAV file (no microphone or hotkeys)"
    )
    _add_config_option(transcribe)
    transcribe.add_argument("file", type=Path, metavar="FILE.wav")
    transcribe.add_argument(
        "--model",
        help="use a temporary server with this model instead of the running service",
    )

    bench = commands.add_parser("bench", help="benchmark models on temporary servers (docs/13)")
    bench.add_argument("--quick", action="store_true", help="stage 1 only (medium group)")
    bench.add_argument("--dataset", type=Path, default=Path.home() / "stt-corpus")
    bench.add_argument("--models", help="comma-separated (default: all six benchmark models)")
    bench.add_argument("--threads", default="4,8", help="comma-separated (default: 4,8)")
    bench.add_argument(
        "--audio-ctx", default="0,1000", help="fixed audio_ctx values, 0 = full window (06 §6.7)"
    )
    bench.add_argument("--repeats", type=int, default=3)
    bench.add_argument("--resume", type=Path, metavar="RUN_DIR", help="continue an interrupted run")
    bench.add_argument("--no-beam", action="store_true", help="skip -bs 5 for the top two models")
    bench.add_argument("--no-sanity", action="store_true", help="skip whisper-bench")
    bench.add_argument("--allow-concurrent", action="store_true")
    bench_commands = bench.add_subparsers(dest="bench_command", metavar="ACTION")
    bench_report = bench_commands.add_parser("report", help="Markdown report of a run directory")
    bench_report.add_argument("run_dir", type=Path, metavar="DIR")
    bench_report.add_argument("--output", type=Path, help="write to a file instead of stdout")

    record = commands.add_parser("record-corpus", help="record the benchmark corpus (docs/13)")
    record.add_argument("dir", type=Path, metavar="DIR")
    record.add_argument(
        "--long", action="store_true", help="record the ~5 min continuous text into DIR/long/"
    )
    return parser


def _add_config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="config file (default: $LOCAL_STT_CONFIG or ~/.config/local-stt/config.toml)",
    )


def _load_config(args: argparse.Namespace) -> "Config | None":
    """The validated config, or None after printing every error (exit code 78)."""
    from local_stt.config import ConfigError, load_config

    try:
        config, warnings = load_config(args.config)
    except ConfigError as e:
        for error in e.errors:
            print(f"config error: {error}", file=sys.stderr)
        return None
    for warning in warnings:
        print(f"config warning: {warning}", file=sys.stderr)
    return config


def _run_models(args: argparse.Namespace) -> int:
    # Imported lazily: local_stt.models is the only module with Internet networking code (12 §12.2).
    from local_stt import models

    config = _load_config(args)
    if config is None:
        return EXIT_CONFIG
    models_dir = config.stt.models_dir
    if args.models_command == "list":
        return models.cmd_list(models_dir)
    if args.models_command == "pull":
        return models.cmd_pull(args.name, models_dir)
    return models.cmd_verify(models_dir)


def _run_transcribe(args: argparse.Namespace) -> int:
    from local_stt.audio.wav import SAMPLE_RATE, wav_bytes_to_float32
    from local_stt.config import config_dir
    from local_stt.stt import whisper_server as ws

    config = _load_config(args)
    if config is None:
        return EXIT_CONFIG
    stt = config.stt
    try:
        audio, rate = wav_bytes_to_float32(args.file.read_bytes())
    except (OSError, ValueError, EOFError) as e:
        print(f"error: {args.file}: {e}", file=sys.stderr)
        return 1
    if rate != SAMPLE_RATE:
        print(f"error: {args.file}: expected {SAMPLE_RATE} Hz, got {rate} Hz", file=sys.stderr)
        return 1

    try:
        if args.model:
            server = ws.TemporaryWhisperServer(
                stt.models_dir / f"ggml-{args.model}.bin",
                model=args.model,
                threads=stt.threads,
                beam_size=stt.beam_size,
                audio_ctx=stt.audio_ctx,
                audio_ctx_margin=stt.audio_ctx_margin,
                language=stt.language,
                startup_timeout_s=stt.startup_timeout_s,
            )
            with server as engine:
                transcript = _transcribe(engine, audio, stt)
        else:
            engine = ws.WhisperServerEngine(
                port=stt.port,
                request_path=ws.read_request_path(config_dir() / "secret"),
                model=stt.model,
                audio_ctx=stt.audio_ctx,
                audio_ctx_margin=stt.audio_ctx_margin,
            )
            transcript = _transcribe(engine, audio, stt)
    except ws.EngineConnectionError as e:
        print(f"error: whisper-server is not running ({e}); use --model M", file=sys.stderr)
        return 1
    except (ws.EngineError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(transcript.text.strip())
    rtf = (
        transcript.processing_s / transcript.audio_duration_s if transcript.audio_duration_s else 0
    )
    print(
        f"audio={transcript.audio_duration_s:.2f}s stt={transcript.processing_s:.2f}s "
        f"rtf={rtf:.2f} model={transcript.model}",
        file=sys.stderr,
    )
    return 0


def _transcribe(
    engine: "WhisperServerEngine", audio: "NDArray[np.float32]", stt: "SttConfig"
) -> "Transcript":
    return engine.transcribe(
        audio,
        sample_rate=16000,
        language=stt.language,
        prompt=stt.vocabulary_prompt or None,
        timeout_s=stt.request_timeout_max_s,
    )


def _run_bench(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from local_stt.bench import report, runner

    if args.bench_command == "report":
        try:
            lines, info = report.load_results(args.run_dir)
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        markdown = report.render(lines, info)
        if args.output:
            args.output.write_text(markdown, encoding="utf-8")
        else:
            print(markdown, end="")
        return 0

    try:
        threads = [int(t) for t in args.threads.split(",")]
        audio_ctx = [int(v) for v in args.audio_ctx.split(",")]
    except ValueError:
        parser.error("--threads and --audio-ctx take comma-separated integers")
    models = args.models.split(",") if args.models else list(runner.DEFAULT_MODELS)
    return runner.run(
        args.dataset.expanduser(),
        args.resume or runner.default_out_dir(),
        models=models,
        threads=threads,
        audio_ctx=audio_ctx,
        repeats=args.repeats,
        quick=args.quick,
        beam=not args.no_beam,
        sanity=not args.no_sanity,
        allow_concurrent=args.allow_concurrent,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "models":
        return _run_models(args)
    if args.command == "transcribe":
        return _run_transcribe(args)
    if args.command == "bench":
        return _run_bench(args, parser)
    if args.command == "record-corpus":
        from local_stt.bench.corpus import cmd_record_corpus

        return cmd_record_corpus(args.dir, long=args.long)
    parser.print_help()
    return 2
