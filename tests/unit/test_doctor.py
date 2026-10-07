"""`local-stt doctor` checks (docs/10-cli-ipc-status.md §10.5) with a faked environment."""

import hashlib
import io
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from local_stt import cli, doctor, ipc, models
from local_stt.config import Config, HotkeysConfig, SttConfig
from local_stt.doctor import Probes, Result, Status
from local_stt.hotkeys.x11 import HotkeyConnectError
from local_stt.interfaces import AudioOpenError, EngineHealth, HotkeyProblem
from local_stt.stt import whisper_server as ws

REPO = Path(__file__).resolve().parents[2]
UID = str(os.getuid())


class Run:
    """Fake command runner: exact argument tuples → (returncode, stdout); others fail."""

    def __init__(self, outputs: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        key = tuple(args)
        self.calls.append(key)
        code, out = self.outputs.get(key, (1, ""))
        return subprocess.CompletedProcess(list(args), code, out, "")


def loginctl(session: str, kind: str) -> dict[tuple[str, ...], tuple[int, str]]:
    return {
        ("loginctl", "show-user", UID, "-p", "Display", "--value"): (0, f"{session}\n"),
        ("loginctl", "show-session", session, "-p", "Type", "--value"): (0, f"{kind}\n"),
    }


GSETTINGS = (
    "org.gnome.desktop.wm.keybindings switch-input-source ['<Super>space', 'XF86Keyboard']\n"
    "org.gnome.desktop.wm.keybindings close @as []\n"
    "org.gnome.settings-daemon.plugins.media-keys terminal ['<Primary><Alt>t']\n"
    "org.gnome.mutter overlay-key 'Super_L'\n"
    "org.gnome.desktop.interface gtk-theme 'Yaru'\n"
)
CUSTOM = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/custom0/"


def gnome(custom_binding: str = "@as []") -> dict[tuple[str, ...], tuple[int, str]]:
    return {
        ("gsettings", "list-recursively"): (0, GSETTINGS),
        (
            "gsettings",
            "get",
            "org.gnome.settings-daemon.plugins.media-keys",
            "custom-keybindings",
        ): (0, "['/custom0/']"),
        ("gsettings", "get", CUSTOM, "name"): (0, "'Dictate'"),
        ("gsettings", "get", CUSTOM, "binding"): (0, custom_binding),
    }


# --- session and environment ---------------------------------------------------------------


def test_session_type_from_loginctl() -> None:
    assert doctor.session_type(Run(loginctl("3", "x11")), {"XDG_SESSION_TYPE": "wayland"}) == "x11"


def test_session_type_falls_back_to_environment() -> None:
    assert doctor.session_type(Run(), {"XDG_SESSION_TYPE": "wayland"}) == "wayland"
    assert doctor.session_type(Run(), {}) is None


def test_check_session() -> None:
    assert doctor.check_session(Run(loginctl("3", "x11")), {}).status is Status.OK
    result = doctor.check_session(Run(loginctl("2", "wayland")), {})
    assert result.status is Status.FAIL
    assert "wayland" in result.detail
    assert "Xorg" in result.hint


def test_check_manager_display() -> None:
    show = ("systemctl", "--user", "show-environment")
    ok = doctor.check_manager_display(Run({show: (0, "HOME=/home/u\nDISPLAY=:1\n")}))
    assert (ok.status, ok.detail) == (Status.OK, "DISPLAY=:1")
    missing = doctor.check_manager_display(Run({show: (0, "HOME=/home/u\n")}))
    assert missing.status is Status.FAIL
    assert missing.hint.startswith("dbus-update-activation-environment")
    assert doctor.check_manager_display(Run()).status is Status.FAIL


# --- config and files ----------------------------------------------------------------------


def test_check_config_invalid_lists_validator_messages(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[stt]\nthreads = 0\n[hotkeys]\npush_to_talk = "Nope+x"\n')
    result, config = doctor.check_config(path, {})
    assert config is None
    assert result.status is Status.FAIL
    assert any(n.startswith("stt.threads") for n in result.notes)
    assert any(n.startswith("hotkeys.push_to_talk") for n in result.notes)


def test_check_config_defaults_when_missing(tmp_path: Path) -> None:
    result, config = doctor.check_config(None, {"XDG_CONFIG_HOME": str(tmp_path)})
    assert config == Config()
    assert result.status is Status.OK
    assert "defaults" in result.detail


def test_check_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ggml-small-q8_0.bin"
    missing = doctor.check_model("STT model", path, "small-q8_0")
    assert missing.status is Status.FAIL
    assert missing.hint == "local-stt models pull small-q8_0"

    path.write_bytes(b"not a model")
    assert doctor.check_model("STT model", path, "small-q8_0").status is Status.FAIL

    part = models.ModelFile(path.name, "", models.file_sha256(path))
    registry = {"small-q8_0": models.Model("small-q8_0", path.name, (part,))}
    monkeypatch.setattr(models, "load_registry", lambda: registry)
    ok = doctor.check_model("STT model", path, "small-q8_0")
    assert (ok.status, ok.detail) == (Status.OK, f"{path} (checksum OK)")


def test_check_model_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "parakeet"
    pull = "local-stt models pull parakeet"
    parts = (
        models.ModelFile("parakeet/a.onnx", "", hashlib.sha256(b"a").hexdigest()),
        models.ModelFile("parakeet/vocab.txt", "", hashlib.sha256(b"v").hexdigest()),
    )
    registry = {"parakeet": models.Model("parakeet", "parakeet", parts)}
    monkeypatch.setattr(models, "load_registry", lambda: registry)

    path.mkdir()  # an empty or partial directory is not the model
    (path / "a.onnx").write_bytes(b"a")
    missing = doctor.check_model("STT model", path, "parakeet")
    assert (missing.status, missing.detail, missing.hint) == (
        Status.FAIL, f"{path}: missing", pull
    )  # fmt: skip
    (path / "vocab.txt").write_bytes(b"other")
    assert doctor.check_model("STT model", path, "parakeet").detail == f"{path}: checksum mismatch"
    (path / "vocab.txt").write_bytes(b"v")
    assert doctor.check_model("STT model", path, "parakeet").status is Status.OK


def test_check_model_without_pinned_checksum(tmp_path: Path) -> None:
    path = tmp_path / "ggml-custom.bin"
    path.write_bytes(b"x")
    result = doctor.check_model("STT model", path, "custom")
    assert result.status is Status.OK
    assert "no pinned checksum" in result.detail


def make_binary(bin_dir: Path, tag: str | None = ws.WHISPER_TAG) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "whisper-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    if tag is not None:
        (bin_dir / ".whisper-tag").write_text(f"{tag}\n")
    return binary


def test_check_whisper_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    assert "missing" in doctor.check_whisper_binary(bin_dir, Run()).detail

    binary = make_binary(bin_dir)
    help_ok = Run({(str(binary), "--help"): (0, "usage")})
    assert doctor.check_whisper_binary(bin_dir, help_ok).status is Status.OK
    failing = doctor.check_whisper_binary(bin_dir, Run())
    assert (failing.status, failing.hint) == (Status.FAIL, "scripts/install.sh --rebuild-whisper")

    make_binary(bin_dir, "v1.0.0")
    old = doctor.check_whisper_binary(bin_dir, help_ok)
    assert old.status is Status.FAIL
    assert old.detail == f"built from v1.0.0, expected {ws.WHISPER_TAG}"

    (bin_dir / ".whisper-tag").unlink()
    assert ".whisper-tag missing" in doctor.check_whisper_binary(bin_dir, help_ok).detail


def test_whisper_tag_matches_install_sh() -> None:
    script = (REPO / "scripts/install.sh").read_text()
    match = re.search(r'^readonly DEFAULT_WHISPER_TAG="([^"]+)"$', script, re.MULTILINE)
    assert match is not None
    assert match.group(1) == ws.WHISPER_TAG


def test_check_private_file(tmp_path: Path) -> None:
    path = tmp_path / "whisper-server.env"
    assert doctor.check_private_file(path).status is Status.FAIL
    path.write_text("x")
    path.chmod(0o644)
    loose = doctor.check_private_file(path)
    assert (loose.status, loose.hint) == (Status.FAIL, "scripts/install.sh")
    assert "mode 0644" in loose.detail
    path.chmod(0o600)
    assert doctor.check_private_file(path).status is Status.OK


def test_check_secret_validates_content(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("short\n")
    path.chmod(0o600)
    assert doctor.check_secret(path).status is Status.FAIL
    path.write_text("0123456789abcdef" * 2 + "\n")
    assert doctor.check_secret(path).status is Status.OK


# --- service, health, port ----------------------------------------------------------------


def test_check_service() -> None:
    args = ("systemctl", "--user", "is-active", "local-stt-engine")
    active = doctor.check_service(Run({args: (0, "active\n")}), "local-stt-engine")
    assert active.status is Status.OK
    inactive = doctor.check_service(Run({args: (3, "inactive\n")}), "local-stt-engine")
    assert (inactive.status, inactive.detail) == (Status.FAIL, "inactive")
    assert inactive.hint == "systemctl --user status local-stt-engine"


def test_engine_unit_follows_stt_engine() -> None:
    assert doctor.engine_unit("parakeet") == "local-stt-engine"
    assert doctor.engine_unit("whisper-server") == "local-stt-whisper"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def active_since(seconds_ago: float, clock: Clock, unit: str = "local-stt-engine") -> Run:
    us = int((clock.now - seconds_ago) * 1e6)
    args = (
        "systemctl",
        "--user",
        "show",
        unit,
        "-p",
        "ActiveEnterTimestampMonotonic",
    )
    return Run({args: (0, f"ActiveEnterTimestampMonotonic={us}\n")})


def fake_health(monkeypatch: pytest.MonkeyPatch, states: list[EngineHealth]) -> None:
    def health(self: ws.WhisperServerEngine) -> EngineHealth:
        return states.pop(0) if len(states) > 1 else states[0]

    monkeypatch.setattr(ws.WhisperServerEngine, "health", health)


def test_check_health_waits_for_model_load(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_health(monkeypatch, [EngineHealth.STARTING, EngineHealth.STARTING, EngineHealth.READY])
    result = doctor.check_health(
        Config(), "/x", active_since(5, clock), clock=clock, sleep=clock.sleep
    )
    assert result.status is Status.OK
    assert clock.now == 1001.0


def test_check_health_loading_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_health(monkeypatch, [EngineHealth.STARTING])
    # active for 58 s of the 60 s startup_timeout_s: only ~2 s are left to wait
    result = doctor.check_health(
        Config(), "/x", active_since(58, clock), clock=clock, sleep=clock.sleep
    )
    assert result.status is Status.FAIL
    assert "still loading" in result.detail
    assert 1002.0 <= clock.now <= 1002.5


def test_check_health_down(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_health(monkeypatch, [EngineHealth.DOWN])
    result = doctor.check_health(Config(), "/x", Run(), clock=clock, sleep=clock.sleep)
    assert result.status is Status.FAIL
    assert result.hint == "journalctl --user -u local-stt-engine"


def test_check_health_of_whisper_server(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_health(monkeypatch, [EngineHealth.STARTING])
    whisper = Config(stt=SttConfig(engine="whisper-server"))
    # the whisper unit's start time counts: 58 of its 60 s are gone
    result = doctor.check_health(
        whisper, "/x", active_since(58, clock, "local-stt-whisper"), clock=clock, sleep=clock.sleep
    )
    assert result.hint == "journalctl --user -u local-stt-whisper"
    assert 1002.0 <= clock.now <= 1002.5


SS = """\
LISTEN 0      4096   127.0.0.53%lo:53    0.0.0.0:*
LISTEN 0      511        127.0.0.1:8178 0.0.0.0:*
LISTEN 0      4096         0.0.0.0:8080  0.0.0.0:*
LISTEN 0      4096            [::]:8080     [::]:*
LISTEN 0      4096           [::1]:631      [::]:*
LISTEN 0      4096               *:9000        *:*
"""


def test_parse_listeners() -> None:
    assert doctor.parse_listeners(SS, 8178) == ["127.0.0.1"]
    assert doctor.parse_listeners(SS, 8080) == ["0.0.0.0", "[::]"]
    assert doctor.parse_listeners(SS, 1) == []


@pytest.mark.parametrize(
    ("port", "status"),
    [
        (8178, Status.OK),
        (631, Status.OK),
        (53, Status.OK),
        (8080, Status.FAIL),
        (9000, Status.FAIL),
        (1, Status.OK),
    ],
)
def test_check_port(port: int, status: Status) -> None:
    result = doctor.check_port(port, Run({("ss", "-ltnH"): (0, SS)}))
    assert result.status is status
    if status is Status.FAIL:
        assert "privacy" in result.detail


# --- hotkeys -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<Super>space", (frozenset({"Super"}), "space")),
        ("<Primary><Shift>x", (frozenset({"Ctrl", "Shift"}), "x")),
        ("<Control><Alt>Delete", (frozenset({"Ctrl", "Alt"}), "Delete")),
        ("Super_L", (frozenset(), "Super_L")),
        ("<Hyper>x", None),
        ("<Super", None),
        ("<Super>", None),
        ("Yaru dark", None),
    ],
)
def test_parse_gnome_accelerator(text: str, expected: tuple[frozenset[str], str] | None) -> None:
    assert doctor.parse_gnome_accelerator(text) == expected


def test_gnome_shortcuts_include_custom_keybindings() -> None:
    entries = doctor.gnome_shortcuts(Run(gnome("'<Super>d'")))
    assert ("org.gnome.desktop.wm.keybindings switch-input-source", "<Super>space") in entries
    assert ("custom shortcut 'Dictate'", "<Super>d") in entries


def test_find_gnome_conflicts() -> None:
    shortcuts = doctor.gnome_shortcuts(Run(gnome("'<Shift>Control_R'")))
    assert doctor.find_gnome_conflicts("Super+space", shortcuts) == [
        "org.gnome.desktop.wm.keybindings switch-input-source = '<Super>space'"
    ]
    assert doctor.find_gnome_conflicts("Alt+Ctrl+T", shortcuts) == [
        "org.gnome.settings-daemon.plugins.media-keys terminal = '<Primary><Alt>t'"
    ]
    assert doctor.find_gnome_conflicts("Shift+Control_R", shortcuts) == [
        "custom shortcut 'Dictate' = '<Shift>Control_R'"
    ]
    assert doctor.find_gnome_conflicts("Control_R", shortcuts) == []


def not_running(request: dict[str, Any]) -> dict[str, Any]:
    raise ipc.DaemonNotRunning("daemon not running")


def no_grab(hotkeys: HotkeysConfig) -> list[HotkeyProblem]:
    raise AssertionError("no test grab while the daemon runs")


def daemon_status(state: str, problems: list[dict[str, str]]) -> Any:
    hotkeys = {
        "state": state,
        "push_to_talk": "Super+space",
        "continuous_toggle": "Shift+Control_R",
        "problems": problems,
    }
    return lambda request: {"ok": True, "status": {"hotkeys": hotkeys}}


def test_hotkeys_daemon_degraded() -> None:
    problem = {
        "hotkey": "continuous_toggle",
        "value": "Shift+Control_R",
        "reason": "already grabbed by another client",
    }
    result = doctor.check_hotkeys(
        HotkeysConfig(), Run(gnome()), daemon_status("degraded", [problem]), no_grab
    )
    assert result.status is Status.FAIL
    assert result.detail == "daemon: degraded"
    assert (
        result.notes[0] == "continuous_toggle (Shift+Control_R): already grabbed by another client"
    )
    # the failed hotkey is not grabbed; the GNOME match for the grabbed one is still shown
    assert result.notes[1].startswith("push_to_talk (Super+space) is also a GNOME shortcut")
    assert "GNOME shortcut" in result.hint


def test_hotkeys_daemon_ok_but_gnome_binds_the_keys() -> None:
    result = doctor.check_hotkeys(HotkeysConfig(), Run(gnome()), daemon_status("OK", []), no_grab)
    assert result.status is Status.WARN
    assert len(result.notes) == 1


def test_hotkeys_daemon_disabled() -> None:
    result = doctor.check_hotkeys(HotkeysConfig(), Run(), daemon_status("disabled", []), no_grab)
    assert result.status is Status.INFO


def test_hotkeys_daemon_not_responding() -> None:
    def broken(request: dict[str, Any]) -> dict[str, Any]:
        raise ipc.IpcError("no response from the daemon: timed out")

    result = doctor.check_hotkeys(HotkeysConfig(), Run(), broken, no_grab)
    assert result.status is Status.FAIL


def test_hotkeys_test_grab() -> None:
    grabbed: list[HotkeysConfig] = []

    def grab(hotkeys: HotkeysConfig) -> list[HotkeyProblem]:
        grabbed.append(hotkeys)
        return []

    result = doctor.check_hotkeys(HotkeysConfig(), Run(gnome()), not_running, grab)
    assert grabbed == [HotkeysConfig()]
    assert (result.status, result.detail) == (Status.OK, "test grab: OK")


def test_hotkeys_test_grab_bad_access() -> None:
    def grab(hotkeys: HotkeysConfig) -> list[HotkeyProblem]:
        return [HotkeyProblem("push_to_talk", "Control_R", "already grabbed by another client")]

    result = doctor.check_hotkeys(HotkeysConfig(), Run(gnome()), not_running, grab)
    assert result.status is Status.FAIL
    assert result.notes == ("push_to_talk (Control_R): already grabbed by another client",)
    assert result.hint == "choose another hotkey in [hotkeys]"


def test_hotkeys_test_grab_without_display() -> None:
    def grab(hotkeys: HotkeysConfig) -> list[HotkeyProblem]:
        raise HotkeyConnectError("cannot connect to X display None: no DISPLAY")

    assert doctor.check_hotkeys(HotkeysConfig(), Run(), not_running, grab).status is Status.FAIL


def test_hotkeys_disabled_in_config() -> None:
    result = doctor.check_hotkeys(HotkeysConfig(enabled=False), Run(), not_running, no_grab)
    assert result.status is Status.INFO


# --- microphone, tools, power -------------------------------------------------------------


def test_rms_dbfs() -> None:
    assert doctor.rms_dbfs(np.full(100, 0.1, dtype=np.float32)) == pytest.approx(-20.0)
    assert doctor.rms_dbfs(np.zeros(100, dtype=np.float32)) == -np.inf
    assert doctor.rms_dbfs(np.zeros(0, dtype=np.float32)) == -np.inf


def measured(level: float, routed: str | None = "alsa_input.mic") -> Any:
    return lambda device: (np.full(16000, level, dtype=np.float32), routed)


def test_check_microphone() -> None:
    ok = doctor.check_microphone("default", measured(0.1))
    assert (ok.status, ok.detail) == (Status.OK, "alsa_input.mic, -20 dBFS over 1 s")

    quiet = doctor.check_microphone("default", measured(0.0005))
    assert quiet.status is Status.WARN
    assert quiet.hint == "check mute/input level in sound settings"
    assert "echo-cancel" in quiet.notes[0]

    assert doctor.check_microphone("default", measured(0.0)).status is Status.WARN


def test_check_microphone_device_not_found() -> None:
    result = doctor.check_microphone("alsa_input.usb", measured(0.1, "alsa_input.mic"))
    assert result.status is Status.WARN
    assert result.detail == "alsa_input.usb not found, using default (alsa_input.mic)"


def test_check_microphone_errors() -> None:
    def cannot_open(device: str) -> Any:
        raise AudioOpenError("cannot open microphone (default): busy")

    assert doctor.check_microphone("default", cannot_open).status is Status.FAIL
    empty = lambda device: (np.zeros(0, dtype=np.float32), None)  # noqa: E731
    assert doctor.check_microphone("default", empty).status is Status.FAIL


def test_check_tools() -> None:
    def which(available: set[str]) -> Any:
        return lambda name: f"/usr/bin/{name}" if name in available else None

    every = doctor.check_tools(which({"xdotool", "pw-play", "paplay", "notify-send"}))
    assert [r.status for r in every] == [Status.OK] * 3
    assert every[1].detail == "/usr/bin/pw-play, /usr/bin/paplay"

    some = doctor.check_tools(which({"paplay"}))
    assert [r.status for r in some] == [Status.WARN, Status.OK, Status.WARN]
    assert "type" in some[0].detail
    assert "no notifications" in some[2].detail
    assert doctor.check_tools(which(set()))[1].detail == "both missing: no sounds"


def supply(root: Path, name: str, **files: str) -> None:
    (root / name).mkdir(parents=True)
    for key, value in files.items():
        (root / name / key).write_text(value + "\n")


def test_on_battery(tmp_path: Path) -> None:
    assert not doctor.on_battery(tmp_path / "none")  # desktop without power_supply

    supply(tmp_path, "AC", type="Mains", online="0")
    supply(tmp_path, "ucsi", type="USB", online="0")
    supply(tmp_path, "hid", type="USB", online="1", scope="Device")  # a mouse, ignored
    assert doctor.on_battery(tmp_path)

    (tmp_path / "ucsi/online").write_text("1\n")  # charging over USB-C
    assert not doctor.on_battery(tmp_path)

    supply(tmp_path, "BAT0", type="Battery", status="Discharging")
    assert doctor.on_battery(tmp_path)


def test_check_power(tmp_path: Path) -> None:
    governor = tmp_path / "scaling_governor"
    governor.write_text("powersave\n")
    power = tmp_path / "power"
    supply(power, "AC", type="Mains", online="1")
    assert doctor.check_power(governor, power).status is Status.OK

    (power / "AC/online").write_text("0\n")
    result = doctor.check_power(governor, power)
    assert result.status is Status.INFO
    assert "latency" in result.detail

    governor.write_text("performance\n")
    assert doctor.check_power(governor, power).status is Status.OK
    assert "unknown" in doctor.check_power(tmp_path / "missing", power).detail


# --- composition and CLI -------------------------------------------------------------------


def probes(tmp_path: Path, run: Run) -> Probes:
    return Probes(
        run=run,
        which=lambda name: None,
        environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        data_dir=tmp_path / "data",
        ipc_call=not_running,
        grab=lambda hotkeys: [],
        measure=measured(0.1),
        governor_file=tmp_path / "governor",
        power_dir=tmp_path / "power",
    )


def test_invalid_config_skips_dependent_checks(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[stt]\nthreads = 0\n")
    results = {r.name: r for r in doctor.run_checks(config, probes(tmp_path, Run()))}
    assert results["config"].status is Status.FAIL
    skipped = ["STT model", "VAD model", "GET /health", "port", "hotkeys", "microphone"]
    for name in skipped:
        assert (results[name].status, results[name].detail) == (Status.SKIP, "config invalid")
    # independent checks still run
    assert results["session"].status is Status.FAIL
    assert results["xdotool"].status is Status.WARN


def test_run_checks_order_and_health_skip(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[stt]\nmodels_dir = "{tmp_path / "models"}"\n')
    results = list(doctor.run_checks(config, probes(tmp_path, Run())))
    assert [r.name for r in results] == [
        "session", "systemd DISPLAY", "config", "STT model", "VAD model", "whisper-server",
        "secret", "whisper-server.env", "local-stt-engine", "GET /health", "port 8178",
        "hotkeys", "microphone", "xdotool", "pw-play/paplay", "notify-send", "CPU / power",
    ]  # fmt: skip
    health = results[9]
    assert (health.status, health.detail) == (Status.SKIP, "local-stt-engine not active")
    assert results[3].detail == f"{tmp_path / 'models/parakeet-tdt-0.6b-v3-int8'}: missing"


def test_run_checks_follow_the_whisper_engine(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[stt]\nengine = "whisper-server"\nmodels_dir = "{tmp_path}"\n')
    results = {r.name: r for r in doctor.run_checks(config, probes(tmp_path, Run()))}
    assert results["STT model"].detail == f"{tmp_path / 'ggml-small-q8_0.bin'}: missing"
    assert results["local-stt-whisper"].status is Status.FAIL


def test_cmd_doctor_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def checks(results: list[Result]) -> Any:
        return lambda config_path, probes=None: iter(results)

    warn = Result(
        "xdotool", Status.WARN, "missing: `type` backend unavailable", "sudo apt install xdotool"
    )
    fail = Result("config", Status.FAIL, "/c.toml: invalid", notes=("stt.threads: must be >= 1",))

    out = io.StringIO()
    monkeypatch.setattr(doctor, "run_checks", checks([warn]))
    assert doctor.cmd_doctor(None, out=out) == 0
    assert out.getvalue() == (
        "WARN  xdotool              missing: `type` backend unavailable\n"
        "                          → sudo apt install xdotool\n"
        "\n0 FAIL, 1 WARN, 0 OK\n"
    )

    out = io.StringIO()
    monkeypatch.setattr(doctor, "run_checks", checks([fail, warn]))
    assert doctor.cmd_doctor(None, out=out) == 1
    assert "                          stt.threads: must be >= 1\n" in out.getvalue()


def test_cli_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path | None] = []

    def fake(config_path: Path | None) -> int:
        seen.append(config_path)
        return 1

    monkeypatch.setattr(doctor, "cmd_doctor", fake)
    assert cli.main(["doctor", "--config", "/tmp/c.toml"]) == 1
    assert seen == [Path("/tmp/c.toml")]
