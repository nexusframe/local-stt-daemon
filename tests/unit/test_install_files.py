"""systemd units and install scripts (docs/11-daemon-systemd-installation.md)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = (REPO / "docs/11-daemon-systemd-installation.md").read_text(encoding="utf-8")


def spec_unit(section: str) -> str:
    """The ```ini block that follows the `## <section>` heading."""
    match = re.search(rf"^## {re.escape(section)}.*?```ini\n(.*?)```", SPEC, re.S | re.M)
    assert match is not None, section
    return match.group(1)


@pytest.mark.parametrize(
    ("unit", "section"),
    [("local-stt-whisper.service", "11.4"), ("local-stt.service", "11.5")],
)
def test_unit_matches_spec(unit: str, section: str) -> None:
    assert (REPO / "systemd" / unit).read_text(encoding="utf-8") == spec_unit(section)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_units_verify() -> None:
    units = [str(p) for p in sorted((REPO / "systemd").glob("*.service"))]
    done = subprocess.run(
        ["systemd-analyze", "--user", "verify", *units], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_scripts_parse(script: str) -> None:
    subprocess.run(["bash", "-n", str(REPO / "scripts" / script)], check=True)


def test_install_default_model_is_the_config_default() -> None:
    from local_stt.config import SttConfig

    script = (REPO / "scripts/install.sh").read_text(encoding="utf-8")
    assert f'MODEL="{SttConfig().model}"' in script


def test_example_config_model_substitution(tmp_path: Path) -> None:
    """install.sh step 7 rewrites only stt.model, and the result is a valid config."""
    from local_stt.config import load_config

    script = (REPO / "scripts/install.sh").read_text(encoding="utf-8")
    match = re.search(r'sed -E "(0,[^"\\]*(?:\\.[^"\\]*)*)"', script)
    assert match is not None
    expression = match.group(1).replace('\\"', '"').replace("$MODEL", "medium-q5_0")
    out = subprocess.run(
        ["sed", "-E", expression, str(REPO / "config.example.toml")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    path = tmp_path / "config.toml"
    path.write_text(out, encoding="utf-8")
    config, _ = load_config(path)
    assert config.stt.model == "medium-q5_0"
    assert config.vad.model == "silero_vad.onnx"
