import io
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from local_stt import logging_setup as ls


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """Restores the root logger's handlers and level changed by setup_logging."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_trace_level_is_registered() -> None:
    assert ls.TRACE == 5
    assert logging.getLevelName(5) == "TRACE"


@pytest.mark.parametrize(
    ("cli", "env", "config", "expected"),
    [
        ("TRACE", "DEBUG", "INFO", ls.TRACE),
        (None, "DEBUG", "INFO", logging.DEBUG),
        (None, "", "TRACE", ls.TRACE),
        (None, None, "INFO", logging.INFO),
    ],
)
def test_level_precedence(cli: str | None, env: str | None, config: str, expected: int) -> None:
    environ = {} if env is None else {"LOCAL_STT_LOG_LEVEL": env}
    assert ls.resolve_level(cli, config, environ) == expected


def test_invalid_env_level_is_an_error() -> None:
    with pytest.raises(ValueError, match="LOCAL_STT_LOG_LEVEL: must be INFO, DEBUG or TRACE"):
        ls.resolve_level(None, "INFO", {"LOCAL_STT_LOG_LEVEL": "debug"})


@pytest.mark.parametrize(
    ("levelno", "pri"),
    [(logging.CRITICAL, 3), (logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6),
     (logging.DEBUG, 7), (ls.TRACE, 7)],
)  # fmt: skip
def test_sd_priority(levelno: int, pri: int) -> None:
    assert ls.sd_priority(levelno) == pri


def journal_env(path: Path) -> dict[str, str]:
    st = os.stat(path)
    return {"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino}"}


def test_journal_detected_only_for_the_announced_stream(tmp_path: Path) -> None:
    journal, other = tmp_path / "journal", tmp_path / "other"
    journal.touch()
    other.touch()
    with journal.open("w") as j, other.open("w") as o:
        assert ls.stderr_is_journal(j, journal_env(journal))
        assert not ls.stderr_is_journal(o, journal_env(journal))  # stderr redirected elsewhere
        assert not ls.stderr_is_journal(j, {})
        assert not ls.stderr_is_journal(j, {"JOURNAL_STREAM": "garbage"})
    assert not ls.stderr_is_journal(io.StringIO(), journal_env(journal))  # no file descriptor


def test_journal_format_prefixes_every_line(tmp_path: Path, root_logger: logging.Logger) -> None:
    path = tmp_path / "journal"
    path.touch()
    with path.open("w") as stream:
        ls.setup_logging(logging.INFO, stream=stream, environ=journal_env(path))
        log = logging.getLogger("local_stt.stt")
        log.warning("server down")
        log.debug("hidden")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log.exception("worker crashed")
    lines = path.read_text().splitlines()
    assert lines[0] == "<4>local_stt.stt: server down"
    assert lines[1] == "<3>local_stt.stt: worker crashed"
    assert lines[2] == "<3>Traceback (most recent call last):"
    assert lines[-1] == "<3>RuntimeError: boom"
    assert all(line.startswith("<3>") for line in lines[1:])


def test_console_format(root_logger: logging.Logger) -> None:
    stream = io.StringIO()
    ls.setup_logging(ls.TRACE, stream=stream, environ={})
    logging.getLogger("local_stt.vad").log(ls.TRACE, "p=0.42")
    logging.getLogger("local_stt.ipc").info("listening")
    first, second = stream.getvalue().splitlines()
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} TRACE local_stt\.vad: p=0\.42", first
    )
    assert second.endswith(" INFO  local_stt.ipc: listening")


def test_setup_is_idempotent_and_level_can_change(root_logger: logging.Logger) -> None:
    stream = io.StringIO()
    ls.setup_logging(logging.INFO, stream=stream, environ={})
    ls.setup_logging(logging.INFO, stream=stream, environ={})
    assert len(root_logger.handlers) == 1
    log = logging.getLogger("local_stt.controller")
    log.debug("before")
    ls.set_level(logging.DEBUG)
    log.debug("after")
    assert stream.getvalue().count("\n") == 1
    assert "after" in stream.getvalue()
