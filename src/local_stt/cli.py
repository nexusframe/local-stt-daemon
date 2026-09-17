"""`local-stt` command-line entry point (subcommands: docs/10-cli-ipc-status.md §10.1)."""

import argparse
from collections.abc import Sequence

from local_stt import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-stt", description="Offline Polish speech-to-text daemon."
    )
    parser.add_argument("--version", action="version", version=f"local-stt {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    models = commands.add_parser("models", help="list, download and verify models")
    models_commands = models.add_subparsers(dest="models_command", metavar="ACTION", required=True)
    models_commands.add_parser("list", help="models in models_dir")
    pull = models_commands.add_parser("pull", help="download a model and verify its SHA256")
    pull.add_argument("name")
    models_commands.add_parser("verify", help="verify checksums of downloaded models")
    return parser


def _run_models(args: argparse.Namespace) -> int:
    # Imported lazily: local_stt.models is the only module with Internet networking code (12 §12.2).
    from local_stt import models

    # TODO(1.1): take stt.models_dir from the config.
    models_dir = models.DEFAULT_MODELS_DIR
    if args.models_command == "list":
        return models.cmd_list(models_dir)
    if args.models_command == "pull":
        return models.cmd_pull(args.name, models_dir)
    return models.cmd_verify(models_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "models":
        return _run_models(args)
    parser.print_help()
    return 2
