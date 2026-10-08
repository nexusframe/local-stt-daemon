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


def _run_wait_for_engine(tmp_path: Path, states: list[str], daemon: str = "active") -> str:
    """install.sh `wait_for_engine` with a fake systemctl that walks through `states`."""
    script = (REPO / "scripts/install.sh").read_text(encoding="utf-8")
    match = re.search(r"^wait_for_engine\(\) \{\n.*?^\}\n", script, re.S | re.M)
    assert match is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "states").write_text("\n".join(states) + "\n")
    (bin_dir / "systemctl").write_text(
        "#!/bin/bash\n"
        f'if [[ "$*" == *"--quiet local-stt.service"* ]]; then [[ {daemon} == active ]]; exit; fi\n'
        'echo "$*" >> "$LOG"\n'
        'if [[ "$*" == *local-stt-engine.service* ]]; then\n'
        '  state=$(head -1 "$STATES"); sed -i 1d "$STATES"; state=${state:-inactive}\n'
        "else state=inactive; fi\n"
        'echo "$state"; [[ $state == active ]] || exit 3\n'
    )
    (bin_dir / "sleep").write_text("#!/bin/bash\n")
    for name in ("systemctl", "sleep"):
        (bin_dir / name).chmod(0o755)
    program = (  # the script runs with these options; `inactive` exits 3
        "set -euo pipefail\n"
        "ENGINE_UNITS=(local-stt-whisper.service local-stt-engine.service)\nENGINE_WAIT_S=5\n"
        'warn() { echo "WARN: $*"; }\n' + match.group(0) + "wait_for_engine\n"
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STATES": str(tmp_path / "states")}
    env["LOG"] = str(tmp_path / "log")
    done = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_install_waits_for_the_engine_before_doctor(tmp_path: Path) -> None:
    # v0.4 acceptance, finding 1: doctor ran while the engine was `activating`
    out = _run_wait_for_engine(tmp_path, ["inactive", "activating", "activating", "active"])
    assert out == ""
    assert (tmp_path / "log").read_text().count("local-stt-engine.service") == 4


def test_install_engine_wait_gives_up_with_a_warning(tmp_path: Path) -> None:
    out = _run_wait_for_engine(tmp_path, ["activating"] * 10)
    assert "no engine unit became active in 5 s" in out


def test_install_engine_wait_skips_without_the_daemon(tmp_path: Path) -> None:
    assert _run_wait_for_engine(tmp_path, ["activating"], daemon="inactive") == ""
    assert not (tmp_path / "log").exists()
