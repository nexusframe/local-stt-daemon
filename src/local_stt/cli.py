"""`local-stt` command-line entry point (subcommands: docs/10-cli-ipc-status.md §10.1)."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_stt import __version__

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from local_stt.config import Config, SttConfig
    from local_stt.interfaces import Transcript
    from local_stt.stt.whisper_server import WhisperServerEngine

EXIT_CONFIG = 78  # EX_CONFIG (10 §10.1)
EXIT_NOT_RUNNING = 3
EXIT_REJECTED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-stt", description="Offline Polish speech-to-text daemon."
    )
    parser.add_argument("--version", action="version", version=f"local-stt {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    daemon = commands.add_parser("daemon", help="run the daemon in the foreground (systemd)")
    _add_config_option(daemon)
    daemon.add_argument(
        "--log-level", choices=["INFO", "DEBUG", "TRACE"], help="overrides logging.level"
    )

    status = commands.add_parser("status", help="daemon status")
    status.add_argument("--json", action="store_true", help="machine-readable status")
    status.add_argument(
        "--watch", action="store_true", help="follow state changes until interrupted (Ctrl+C)"
    )
    ptt = commands.add_parser("ptt", help="start/stop push-to-talk, like the hotkey")
    ptt.add_argument("action", choices=["start", "stop"])
    commands.add_parser("toggle", help="start/stop continuous dictation, like the hotkey")
    commands.add_parser("cancel", help="cancel the recording and pending jobs")
    commands.add_parser("reload", help="reload the config file")
    commands.add_parser("devices", help="list PipeWire microphones for audio.device")
    _add_config_option(commands.add_parser("doctor", help="environment diagnostics"))

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


# --- daemon commands over IPC (10 §10.1-10.2) -------------------------------------------


def _call(request: dict[str, Any]) -> dict[str, Any] | int:
    """The daemon's response, or an exit code after printing the problem."""
    from local_stt import ipc

    try:
        return ipc.call(request)
    except ipc.DaemonNotRunning:
        print("daemon not running", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except ipc.IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _rejected(response: dict[str, Any]) -> int:
    print(f"rejected: {response.get('message') or response.get('error')}", file=sys.stderr)
    return EXIT_REJECTED


def _run_daemon_command(args: argparse.Namespace) -> int:
    request: dict[str, Any] = {"cmd": args.command}
    if args.command == "ptt":
        request["action"] = args.action
    response = _call(request)
    if isinstance(response, int):
        return response

    if args.command == "reload" and "errors" in response:
        for error in response["errors"]:
            print(f"config error: {error}", file=sys.stderr)
        print("reload rejected; the daemon keeps the current config", file=sys.stderr)
        return EXIT_CONFIG
    if not response.get("ok"):
        return _rejected(response)
    if args.command == "status":
        status = response["status"]
        print(json.dumps(status, indent=2) if args.json else format_status(status))
    elif args.command == "cancel" and response.get("injection_in_flight"):
        print("Remaining jobs cancelled; injection already in progress may finish.")
    elif args.command == "reload":
        print(_format_reload(response))
    return 0


def _run_watch(*, json_lines: bool) -> int:
    """`status --watch` (10 §10.4): one rewritten line on a terminal, a line per change
    otherwise (status bars); `--json` prints the raw stream, job events included."""
    from local_stt import ipc

    tty = sys.stdout.isatty()
    try:
        for message in ipc.subscribe():
            if "event" not in message:  # an error response instead of the stream
                return _rejected(message)
            if json_lines:
                print(json.dumps(message, ensure_ascii=False), flush=True)
            elif message["event"] == "state":
                state = message["status"]["state"]
                if tty:
                    print(f"\r\033[K{state}", end="", flush=True)
                else:
                    print(state, flush=True)
    except KeyboardInterrupt:
        if tty and not json_lines:
            print()
        return 0
    except ipc.DaemonNotRunning:
        print("daemon not running", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except ipc.IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if tty and not json_lines:
        print()
    print("daemon stopped", file=sys.stderr)
    return EXIT_NOT_RUNNING


def _format_reload(response: dict[str, Any]) -> str:
    applied, deferred = response.get("applied", []), response.get("deferred", [])
    if not applied and not deferred:
        return "no changes"
    lines = [f"applied:  {', '.join(applied) or '-'}", f"deferred: {', '.join(deferred) or '-'}"]
    if response.get("server_restart"):
        lines.append("whisper-server restarts with the new settings")
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def format_status(status: dict[str, Any]) -> str:
    """The text form of `status` (10 §10.4)."""
    from local_stt.stt.whisper_server import HOST

    engine, hotkeys = status["engine"], status["hotkeys"]
    audio, pipeline = status["audio"], status["pipeline"]
    lines = [f"local-stt {status['version']} — {status['state']}"]
    lines.append(
        f"  engine     {engine['state']:<7} {engine['name']} {engine['model']} "
        f"@{HOST}:{engine['port']} (t={engine['threads']})"
    )
    if hotkeys["state"] == "disabled":
        lines.append("  hotkeys    disabled")
    else:
        lines.append(
            f"  hotkeys    {hotkeys['state']:<7} PTT={hotkeys['push_to_talk']}  "
            f"continuous={hotkeys['continuous_toggle']}"
        )
        for p in hotkeys["problems"]:
            lines.append(f"             {p['hotkey']} ({p['value']}): {p['reason']}")
    lines.append(f"  audio      {audio['device']} ({'open' if audio['open'] else 'closed'})")
    work = f"{pipeline['queued']} queued"
    if pipeline["busy"]:
        work += ", transcribing"
    if pipeline["paused"]:
        work += ", paused"
    last = pipeline["last"]
    if last is not None:
        rtf = last["stt_s"] / last["audio_s"]
        work += (
            f", last: {last['audio_s']:.1f} s audio → {last['stt_s']:.1f} s "
            f"(RTF {rtf:.2f}) {_duration(last['ago_s'])} ago"
        )
    lines.append(f"  pipeline   {work}")
    lines.append(f"  uptime     {_duration(status['uptime_s'])}")
    return "\n".join(lines)


def _run_devices() -> int:
    import subprocess

    from local_stt.audio.capture import AudioCapture

    try:
        devices = AudioCapture.list_devices()
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        print(f"error: cannot list PipeWire sources (pactl): {e}", file=sys.stderr)
        return 1
    width = max((len(d.name) for d in devices), default=0)
    for d in devices:
        mark = "*" if d.is_default else " "
        print(f"{mark} {d.name:<{width}}  {d.description}")
    print('* = default source (audio.device = "default")')
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status" and args.watch:
        return _run_watch(json_lines=args.json)
    if args.command in ("status", "ptt", "toggle", "cancel", "reload"):
        return _run_daemon_command(args)
    if args.command == "devices":
        return _run_devices()
    if args.command == "daemon":
        from local_stt.app import run_daemon

        return run_daemon(args.config, args.log_level)
    if args.command == "doctor":
        from local_stt.doctor import cmd_doctor

        return cmd_doctor(args.config)
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
