"""`local-stt` command-line entry point (subcommands: docs/10-cli-ipc-status.md §10.1)."""

import argparse
from collections.abc import Sequence

from local_stt import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-stt", description=__doc__)
    parser.add_argument("--version", action="version", version=f"local-stt {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
