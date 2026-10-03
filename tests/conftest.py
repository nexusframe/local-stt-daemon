from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI tests never read the user's ~/.config/local-stt/config.toml."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("LOCAL_STT_CONFIG", raising=False)
