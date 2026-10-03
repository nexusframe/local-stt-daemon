"""Logging setup: TRACE level, journald or console format (docs/12 §12.1)."""

import logging
import os
import sys
from collections.abc import Mapping
from typing import TextIO

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

LEVELS = {"INFO": logging.INFO, "DEBUG": logging.DEBUG, "TRACE": TRACE}
LOG_LEVEL_ENV_VAR = "LOCAL_STT_LOG_LEVEL"

_CONSOLE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s"
_CONSOLE_DATEFMT = "%Y-%m-%d %H:%M:%S"


def resolve_level(
    cli_level: str | None, config_level: str, environ: Mapping[str, str] = os.environ
) -> int:
    """`--log-level` > `$LOCAL_STT_LOG_LEVEL` > `logging.level`; raises ValueError."""
    for source, name in (
        ("--log-level", cli_level),
        (LOG_LEVEL_ENV_VAR, environ.get(LOG_LEVEL_ENV_VAR) or None),
        ("logging.level", config_level),
    ):
        if name is not None:
            if name not in LEVELS:
                raise ValueError(f"{source}: must be INFO, DEBUG or TRACE (got {name!r})")
            return LEVELS[name]
    raise AssertionError("unreachable: config_level is always set")  # pragma: no cover


def sd_priority(levelno: int) -> int:
    """sd-daemon priority: <3> error, <4> warning, <6> info, <7> debug/trace."""
    if levelno >= logging.ERROR:
        return 3
    if levelno >= logging.WARNING:
        return 4
    if levelno >= logging.INFO:
        return 6
    return 7


class JournalFormatter(logging.Formatter):
    """`<PRI>logger: message`; journald adds the timestamp.

    journald splits a stream entry per line and reads the prefix of each line, so every line of
    a multi-line record (e.g. a traceback) gets the prefix and keeps the record's priority.
    """

    def __init__(self) -> None:
        super().__init__("%(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{sd_priority(record.levelno)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def stderr_is_journal(stream: TextIO, environ: Mapping[str, str] = os.environ) -> bool:
    """True if `stream` is the journald stream announced in `$JOURNAL_STREAM` (dev:inode)."""
    value = environ.get("JOURNAL_STREAM", "")
    try:
        dev, ino = (int(part) for part in value.split(":"))
        st = os.fstat(stream.fileno())
    except (ValueError, OSError):
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


def setup_logging(
    level: int, *, stream: TextIO | None = None, environ: Mapping[str, str] = os.environ
) -> None:
    """Configures the root logger with one stderr handler; safe to call again (reload)."""
    stream = stream if stream is not None else sys.stderr
    handler = logging.StreamHandler(stream)
    if stderr_is_journal(stream, environ):
        handler.setFormatter(JournalFormatter())
    else:
        handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _CONSOLE_DATEFMT))
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)


def set_level(level: int) -> None:
    """Applies a new level on reload (`logging.*` is in the live group, 04 §4.6)."""
    logging.getLogger().setLevel(level)
