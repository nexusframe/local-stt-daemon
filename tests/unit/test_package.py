import pytest

import local_stt
from local_stt.cli import main


def test_version_is_set() -> None:
    assert local_stt.__version__


def test_cli_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert local_stt.__version__ in capsys.readouterr().out
