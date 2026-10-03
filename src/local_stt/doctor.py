"""`local-stt doctor`: environment diagnostics (docs/10-cli-ipc-status.md §10.5).

Each check yields a `Result` (OK / INFO / WARN / FAIL / SKIP) with an optional suggested fix.
Checks that need the config are skipped when it is invalid. The exit code is 1 when any check
FAILs. External commands, the IPC client, the X test grab and the microphone are injected
through `Probes`, so the checks are unit-tested without the real environment.

`local_stt.models` (the only module with Internet code, 12 §12.2) is imported lazily, only by
the model checksum check, so the daemon can reuse `session_type()` without loading it.
"""

import ipaddress
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from local_stt.config import Config, HotkeysConfig
    from local_stt.interfaces import HotkeyProblem

WHISPER_UNIT = "local-stt-whisper"
MIC_TEST_S = 1.0
MIC_WARN_DBFS = -60.0
HEALTH_POLL_S = 0.5
_COMMAND_TIMEOUT_S = 10.0
_GRABBED = ("push_to_talk", "continuous_toggle")  # ptt_cancel_key is never grabbed (07 §7.4)
_CUSTOM_KEYBINDINGS = "org.gnome.settings-daemon.plugins.media-keys"
_CUSTOM_KEYBINDING = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
# GTK accelerator modifier names → hotkey modifiers (07 §7.2); others never match.
_ACCEL_MODIFIERS = {
    "control": "Ctrl",
    "ctrl": "Ctrl",
    "primary": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "mod1": "Alt",
    "super": "Super",
    "mod4": "Super",
}


class Status(Enum):
    OK = "OK"
    INFO = "INFO"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Result:
    name: str
    status: Status
    detail: str
    hint: str = ""
    notes: tuple[str, ...] = ()  # extra lines: validator messages, hotkey problems, matches


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_S, check=False
    )


def _output(run: Runner, args: Sequence[str]) -> str | None:
    """Stripped stdout, or None when the command is missing, fails or times out."""
    try:
        proc = run(args)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


# --- session (07 §7.5; also used by the daemon at startup) -------------------------------


def session_type(run: Runner = run_command, environ: Mapping[str, str] = os.environ) -> str | None:
    """Type of the user's graphical session through loginctl; `XDG_SESSION_TYPE` as fallback.

    `XDG_SESSION_TYPE` in the user manager may be stale after changing sessions (07 §7.5).
    """
    session = _output(run, ["loginctl", "show-user", str(os.getuid()), "-p", "Display", "--value"])
    if session:
        kind = _output(run, ["loginctl", "show-session", session, "-p", "Type", "--value"])
        if kind:
            return kind
    return environ.get("XDG_SESSION_TYPE") or None


def check_session(run: Runner, environ: Mapping[str, str]) -> Result:
    kind = session_type(run, environ)
    if kind == "x11":
        return Result("session", Status.OK, "x11")
    return Result(
        "session",
        Status.FAIL,
        f"{kind or 'unknown'} (only X11 is supported)",
        "Select the Ubuntu on Xorg session on the login screen",
    )


def check_manager_display(run: Runner) -> Result:
    name = "systemd DISPLAY"
    hint = "dbus-update-activation-environment --systemd DISPLAY XAUTHORITY"
    out = _output(run, ["systemctl", "--user", "show-environment"])
    if out is None:
        return Result(name, Status.FAIL, "cannot read systemctl --user show-environment", hint)
    env = dict(line.partition("=")[::2] for line in out.splitlines())
    if env.get("DISPLAY"):
        return Result(name, Status.OK, f"DISPLAY={env['DISPLAY']}")
    return Result(name, Status.FAIL, "DISPLAY missing from the user manager environment", hint)


# --- config and files ----------------------------------------------------------------------


def check_config(
    config_path: Path | None, environ: Mapping[str, str]
) -> "tuple[Result, Config | None]":
    from local_stt.config import ConfigError, load_config, resolve_config_path

    path, _ = resolve_config_path(config_path, environ)
    try:
        config, warnings = load_config(config_path, environ)
    except ConfigError as e:
        return Result("config", Status.FAIL, f"{path}: invalid", notes=tuple(e.errors)), None
    shown = str(path) if path.is_file() else f"{path} (not found, defaults)"
    if warnings:
        return Result("config", Status.WARN, shown, notes=tuple(warnings)), config
    return Result("config", Status.OK, shown), config


def check_model(name: str, path: Path, pull_name: str) -> Result:
    from local_stt import models  # lazily: Internet code (module docstring)

    hint = f"local-stt models pull {pull_name}"
    if not path.is_file():
        return Result(name, Status.FAIL, f"{path}: missing", hint)
    known = {m.filename: m for m in models.load_registry().values()}.get(path.name)
    if known is None:
        return Result(name, Status.OK, f"{path} (no pinned checksum)")
    if models.file_sha256(path) != known.sha256:
        return Result(name, Status.FAIL, f"{path}: checksum mismatch", hint)
    return Result(name, Status.OK, f"{path} (checksum OK)")


def check_whisper_binary(bin_dir: Path, run: Runner) -> Result:
    from local_stt.stt.whisper_server import WHISPER_TAG

    name, hint = "whisper-server", "scripts/install.sh --rebuild-whisper"
    binary = bin_dir / "whisper-server"
    if not os.access(binary, os.X_OK):
        return Result(name, Status.FAIL, f"{binary}: missing", hint)
    try:
        ok = run([str(binary), "--help"]).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        return Result(name, Status.FAIL, f"{binary} --help: {e}", hint)
    if not ok:
        return Result(name, Status.FAIL, f"{binary} --help fails", hint)
    try:
        tag = (bin_dir / ".whisper-tag").read_text(encoding="utf-8").strip()
    except OSError:
        return Result(name, Status.FAIL, "bin/.whisper-tag missing", hint)
    if tag != WHISPER_TAG:
        return Result(name, Status.FAIL, f"built from {tag}, expected {WHISPER_TAG}", hint)
    return Result(name, Status.OK, f"{binary} ({tag})")


def check_private_file(path: Path) -> Result:
    hint = "scripts/install.sh"
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return Result(path.name, Status.FAIL, f"{path}: missing", hint)
    except OSError as e:
        return Result(path.name, Status.FAIL, f"{path}: {e}", hint)
    if mode != 0o600:
        return Result(path.name, Status.FAIL, f"{path}: mode {mode:04o}, expected 0600", hint)
    return Result(path.name, Status.OK, f"{path} (0600)")


def check_secret(path: Path) -> Result:
    from local_stt.stt.whisper_server import read_request_path

    result = check_private_file(path)
    if result.status is not Status.OK:
        return result
    try:
        read_request_path(path)
    except (OSError, ValueError) as e:
        return Result(path.name, Status.FAIL, str(e), "scripts/install.sh")
    return result


# --- whisper-server service --------------------------------------------------------------


def check_service(run: Runner) -> Result:
    try:
        state = run(["systemctl", "--user", "is-active", WHISPER_UNIT]).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        state = str(e)
    if state == "active":
        return Result(WHISPER_UNIT, Status.OK, "active")
    return Result(
        WHISPER_UNIT, Status.FAIL, state or "unknown", f"systemctl --user status {WHISPER_UNIT}"
    )


def _active_for_s(run: Runner, clock: Callable[[], float]) -> float:
    """Seconds since the unit became active (CLOCK_MONOTONIC, as time.monotonic()), else 0."""
    out = _output(
        run,
        ["systemctl", "--user", "show", WHISPER_UNIT, "-p", "ActiveEnterTimestampMonotonic"],
    )
    try:
        since_us = int((out or "").partition("=")[2])
    except ValueError:
        return 0.0
    return max(0.0, clock() - since_us / 1e6) if since_us else 0.0


def check_health(
    config: "Config",
    request_path: str,
    run: Runner,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """`GET /health`; a model still loading is waited for until `startup_timeout_s` since start."""
    from local_stt.interfaces import EngineHealth
    from local_stt.stt.whisper_server import HOST, WhisperServerEngine

    name, hint = "GET /health", f"journalctl --user -u {WHISPER_UNIT}"
    stt = config.stt
    engine = WhisperServerEngine(port=stt.port, request_path=request_path, model=stt.model)
    deadline = clock() + stt.startup_timeout_s - _active_for_s(run, clock)
    while True:
        health = engine.health()
        if health is EngineHealth.READY:
            return Result(name, Status.OK, f"READY @{HOST}:{stt.port}")
        if health is EngineHealth.DOWN:
            return Result(
                name, Status.FAIL, f"no response or wrong request path @{HOST}:{stt.port}", hint
            )
        if clock() >= deadline:
            return Result(
                name,
                Status.FAIL,
                f"model still loading after {stt.startup_timeout_s:g} s (stt.startup_timeout_s)",
                hint,
            )
        sleep(HEALTH_POLL_S)


def parse_listeners(ss_output: str, port: int) -> list[str]:
    """Local addresses listening on `port`, from `ss -ltnH` output."""
    found = []
    for line in ss_output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        address, _, local_port = fields[3].rpartition(":")
        if local_port == str(port):
            found.append(address)
    return found


def _is_loopback(address: str) -> bool:
    host = address.strip("[]").partition("%")[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # "*" = all addresses
        return False


def check_port(port: int, run: Runner) -> Result:
    name = f"port {port}"
    out = _output(run, ["ss", "-ltnH"])
    if out is None:
        return Result(name, Status.WARN, "cannot run ss -ltnH")
    addresses = parse_listeners(out, port)
    exposed = [a for a in addresses if not _is_loopback(a)]
    if exposed:
        return Result(
            name,
            Status.FAIL,
            f"listening on {', '.join(exposed)} (privacy: must be loopback only, N5)",
            "stop the process listening there; whisper-server must use --host 127.0.0.1",
        )
    if not addresses:
        return Result(name, Status.OK, "nothing listening")
    return Result(name, Status.OK, f"loopback only ({', '.join(addresses)})")


# --- hotkeys -------------------------------------------------------------------------------


def parse_gnome_accelerator(text: str) -> tuple[frozenset[str], str] | None:
    """`<Primary><Shift>x` → ({"Ctrl", "Shift"}, "x"); None for anything else."""
    mods: set[str] = set()
    rest = text
    while rest.startswith("<"):
        end = rest.find(">")
        modifier = _ACCEL_MODIFIERS.get(rest[1:end].lower()) if end > 0 else None
        if modifier is None:
            return None
        mods.add(modifier)
        rest = rest[end + 1 :]
    if not rest or any(c in rest for c in " <>"):
        return None
    return frozenset(mods), rest


def _quoted(value: str) -> list[str]:
    return re.findall(r"'((?:[^'\\]|\\.)*)'", value)


def gnome_shortcuts(run: Runner) -> list[tuple[str, str]]:
    """(where, accelerator) pairs: every gsettings string plus custom keybindings.

    Custom keybindings use a relocatable schema, which `list-recursively` does not include.
    """
    entries: list[tuple[str, str]] = []
    for line in (_output(run, ["gsettings", "list-recursively"]) or "").splitlines():
        schema, _, rest = line.partition(" ")
        key, _, value = rest.partition(" ")
        entries += [(f"{schema} {key}", s) for s in _quoted(value)]
    paths = _quoted(
        _output(run, ["gsettings", "get", _CUSTOM_KEYBINDINGS, "custom-keybindings"]) or ""
    )
    for path in paths:
        schema = f"{_CUSTOM_KEYBINDING}:{path}"
        label = _quoted(_output(run, ["gsettings", "get", schema, "name"]) or "")
        where = f"custom shortcut {label[0]!r}" if label else f"custom shortcut {path}"
        entries += [
            (where, s) for s in _quoted(_output(run, ["gsettings", "get", schema, "binding"]) or "")
        ]
    return entries


def find_gnome_conflicts(hotkey: str, shortcuts: list[tuple[str, str]]) -> list[str]:
    from local_stt.hotkeys.spec import parse_hotkey

    try:
        mods, keysym = parse_hotkey(hotkey)
    except ValueError:
        return []
    matches = []
    for where, accelerator in shortcuts:
        parsed = parse_gnome_accelerator(accelerator)
        if parsed is not None and parsed[0] == mods and parsed[1].lower() == keysym.lower():
            matches.append(f"{where} = '{accelerator}'")
    return matches


def grab_hotkeys(hotkeys: "HotkeysConfig") -> "list[HotkeyProblem]":
    """Grabs the configured hotkeys on a separate X connection and releases them."""
    from local_stt.hotkeys.x11 import X11GrabHotkeys

    backend = X11GrabHotkeys()
    backend.start(lambda _event: None)
    try:
        return backend.apply(hotkeys)
    finally:
        backend.stop()


def _hotkey_result(
    source: str, problems: list[dict[str, str]], grabbed: dict[str, str], run: Runner
) -> Result:
    """FAIL for problems; WARN when a GNOME shortcut binds the same keys.

    GNOME (Mutter) shortcuts do not make our core grab fail with BadAccess (tested with
    `<Super>space`), so matches are looked up for every grabbed hotkey, not only on problems.
    """
    shortcuts = gnome_shortcuts(run)
    notes = [f"{p['hotkey']} ({p['value']}): {p['reason']}" for p in problems]
    conflicts = [
        f"{name} ({value}) is also a GNOME shortcut: {match}"
        for name, value in grabbed.items()
        for match in find_gnome_conflicts(value, shortcuts)
    ]
    notes += conflicts
    hint = (
        "change or disable the GNOME shortcut (gsettings / Settings → Keyboard) "
        "or choose another hotkey in [hotkeys]"
        if conflicts
        else "choose another hotkey in [hotkeys]"
    )
    if problems:
        return Result("hotkeys", Status.FAIL, f"{source}: degraded", hint, tuple(notes))
    if conflicts:
        return Result(
            "hotkeys",
            Status.WARN,
            f"{source}: grabbed, but GNOME may take the keys",
            hint,
            tuple(notes),
        )
    return Result("hotkeys", Status.OK, f"{source}: OK")


def check_hotkeys(
    hotkeys: "HotkeysConfig",
    run: Runner,
    ipc_call: Callable[[dict[str, Any]], dict[str, Any]],
    grab: "Callable[[HotkeysConfig], list[HotkeyProblem]]",
) -> Result:
    """Daemon running: its `hotkeys` status; otherwise a test grab (10 §10.5)."""
    from local_stt import ipc
    from local_stt.hotkeys.x11 import HotkeyConnectError

    try:
        response = ipc_call({"cmd": "status"})
    except ipc.DaemonNotRunning:
        response = None
    except ipc.IpcError as e:
        return Result("hotkeys", Status.FAIL, f"daemon not responding: {e}")
    if response is not None:
        state = response.get("status", {}).get("hotkeys", {})
        if state.get("state") == "disabled":
            return Result("hotkeys", Status.INFO, "daemon: disabled (hotkeys.enabled = false)")
        problems = list(state.get("problems", []))
        values = {name: state.get(name, "") for name in _GRABBED}
        source = "daemon"
    else:
        if not hotkeys.enabled:
            return Result("hotkeys", Status.INFO, "disabled (hotkeys.enabled = false)")
        try:
            found = grab(hotkeys)
        except HotkeyConnectError as e:
            return Result("hotkeys", Status.FAIL, str(e), "run doctor inside the X11 session")
        problems = [{"hotkey": p.hotkey, "value": p.value, "reason": p.reason} for p in found]
        values = {name: getattr(hotkeys, name) for name in _GRABBED}
        source = "test grab"
    failed = {p["hotkey"] for p in problems}
    grabbed = {name: value for name, value in values.items() if name not in failed and value}
    return _hotkey_result(source, problems, grabbed, run)


# --- microphone, tools, power ------------------------------------------------------------


def rms_dbfs(samples: NDArray[np.float32]) -> float:
    if samples.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * math.log10(rms) if rms > 0 else -math.inf


def measure_microphone(device: str) -> tuple[NDArray[np.float32], str | None]:
    """Records MIC_TEST_S seconds; returns the samples and the PipeWire source actually used.

    Raises AudioOpenError.
    """
    from local_stt.audio.capture import AudioCapture, AudioFrame

    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    capture = AudioCapture(frames, device)
    capture.open(recording_id=0, capture_id=0)
    try:
        time.sleep(MIC_TEST_S)
        try:
            routed = capture.routed_source()
        except (OSError, subprocess.SubprocessError, ValueError):
            routed = None
    finally:
        capture.close()
    chunks = []
    while not frames.empty():
        chunks.append(frames.get_nowait().samples)
    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return samples, routed


def check_microphone(
    device: str, measure: Callable[[str], tuple[NDArray[np.float32], str | None]]
) -> Result:
    from local_stt.audio.capture import DEFAULT_DEVICE, AudioOpenError

    try:
        samples, routed = measure(device)
    except AudioOpenError as e:
        return Result("microphone", Status.FAIL, str(e), "local-stt devices")
    if samples.size == 0:
        return Result("microphone", Status.FAIL, f"{device}: no audio frames in {MIC_TEST_S:g} s")
    level = rms_dbfs(samples)
    shown = f"{routed or device}, {level:.0f} dBFS over {MIC_TEST_S:g} s"
    if device != DEFAULT_DEVICE and routed is not None and routed != device:
        return Result(
            "microphone",
            Status.WARN,
            f"{device} not found, using default ({routed})",
            "set audio.device to a name from local-stt devices",
        )
    if level < MIC_WARN_DBFS:
        return Result(
            "microphone",
            Status.WARN,
            shown,
            "check mute/input level in sound settings",
            (
                "a very quiet microphone may also benefit from PipeWire's WebRTC echo-cancel "
                "module (noise suppression + gain control), enabled system-wide (05 §5.7)",
            ),
        )
    return Result("microphone", Status.OK, shown)


def check_tools(which: Callable[[str], str | None]) -> list[Result]:
    from local_stt.feedback import PLAYERS

    tools = [
        ("xdotool", ("xdotool",), "missing: `type` backend unavailable", "xdotool"),
        ("pw-play/paplay", PLAYERS, "both missing: no sounds", "pipewire-bin"),
        ("notify-send", ("notify-send",), "missing: no notifications", "libnotify-bin"),
    ]
    results = []
    for name, commands, problem, package in tools:
        found = [path for c in commands if (path := which(c))]
        if found:
            results.append(Result(name, Status.OK, ", ".join(found)))
        else:
            results.append(Result(name, Status.WARN, problem, f"sudo apt install {package}"))
    return results


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def on_battery(power_dir: Path) -> bool:
    """A discharging battery, or external supplies that exist and are all offline."""
    try:
        supplies = list(power_dir.iterdir())
    except OSError:
        return False
    online: list[bool] = []
    for supply in supplies:
        kind = _read(supply / "type")
        if kind == "Battery" and _read(supply / "status") == "Discharging":
            return True
        if kind in ("Mains", "USB") and _read(supply / "scope") != "Device":
            online.append(_read(supply / "online") == "1")
    return bool(online) and not any(online)


def check_power(governor_file: Path, power_dir: Path) -> Result:
    governor = _read(governor_file) or "unknown"
    power = "battery" if on_battery(power_dir) else "AC"
    detail = f"governor {governor}, {power}"
    if governor == "powersave" and power == "battery":
        return Result(
            "CPU / power", Status.INFO, f"{detail}: higher transcription latency on battery"
        )
    return Result("CPU / power", Status.OK, detail)


# --- composition ---------------------------------------------------------------------------


def _default_ipc_call(request: dict[str, Any]) -> dict[str, Any]:
    from local_stt import ipc

    return ipc.call(request)


@dataclass
class Probes:
    """The environment the checks look at; tests replace parts of it."""

    run: Runner = run_command
    which: Callable[[str], str | None] = shutil.which
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    data_dir: Path = Path.home() / ".local/share/local-stt"
    ipc_call: Callable[[dict[str, Any]], dict[str, Any]] = _default_ipc_call
    grab: "Callable[[HotkeysConfig], list[HotkeyProblem]]" = grab_hotkeys
    measure: Callable[[str], tuple[NDArray[np.float32], str | None]] = measure_microphone
    governor_file: Path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    power_dir: Path = Path("/sys/class/power_supply")
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def run_checks(config_path: Path | None, probes: Probes | None = None) -> Iterator[Result]:
    """All §10.5 checks in table order, yielded as each one finishes."""
    from local_stt.config import config_dir
    from local_stt.stt.whisper_server import read_request_path

    p = probes or Probes()
    yield check_session(p.run, p.environ)
    yield check_manager_display(p.run)
    config_result, config = check_config(config_path, p.environ)
    yield config_result

    def skipped(name: str) -> Result:
        return Result(name, Status.SKIP, "config invalid")

    if config is None:
        yield skipped("STT model")
        yield skipped("VAD model")
    else:
        yield check_model("STT model", config.stt.model_path, config.stt.model)
        if config.vad.enabled:
            yield check_model("VAD model", config.vad_model_path, "silero-vad")
        else:
            yield Result("VAD model", Status.SKIP, "vad.enabled = false")
    yield check_whisper_binary(p.data_dir / "bin", p.run)
    cdir = config_dir(p.environ)
    secret = check_secret(cdir / "secret")
    yield secret
    yield check_private_file(cdir / "whisper-server.env")
    service = check_service(p.run)
    yield service

    if config is None:
        yield skipped("GET /health")
    elif service.status is not Status.OK:
        yield Result("GET /health", Status.SKIP, f"{WHISPER_UNIT} not active")
    elif secret.status is not Status.OK:
        yield Result("GET /health", Status.SKIP, "secret unusable")
    else:
        request_path = read_request_path(cdir / "secret")
        yield check_health(config, request_path, p.run, clock=p.clock, sleep=p.sleep)

    if config is None:
        yield skipped("port")
        yield skipped("hotkeys")
        yield skipped("microphone")
    else:
        yield check_port(config.stt.port, p.run)
        yield check_hotkeys(config.hotkeys, p.run, p.ipc_call, p.grab)
        yield check_microphone(config.audio.device, p.measure)
    yield from check_tools(p.which)
    yield check_power(p.governor_file, p.power_dir)


def format_result(result: Result) -> str:
    lines = [f"{result.status.value:<5} {result.name:<20} {result.detail}"]
    lines += [f"{'':26}{note}" for note in result.notes]
    if result.hint:
        lines.append(f"{'':26}→ {result.hint}")
    return "\n".join(lines)


def cmd_doctor(
    config_path: Path | None, probes: Probes | None = None, out: TextIO = sys.stdout
) -> int:
    counts = dict.fromkeys(Status, 0)
    for result in run_checks(config_path, probes):
        counts[result.status] += 1
        print(format_result(result), file=out, flush=True)
    print(
        f"\n{counts[Status.FAIL]} FAIL, {counts[Status.WARN]} WARN, {counts[Status.OK]} OK",
        file=out,
    )
    return 1 if counts[Status.FAIL] else 0
