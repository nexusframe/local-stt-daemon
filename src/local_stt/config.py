"""Configuration: TOML loading, validation and `whisper-server.env` generation (docs/09).

Every problem is reported as `<section>.<key>: <problem> (got <value>)`; all problems are
collected before raising, so `reload` can return the full list (04 §4.6). The daemon never
writes the user's config, only `whisper-server.env` (ADR-008).
"""

import json
import os
import re
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

from Xlib import XK

from local_stt.stt.whisper_server import FULL_AUDIO_CTX, HOST

# python-xlib loads only the latin1 and miscellany keysyms by default; ISO_Level3_Shift is in xkb.
XK.load_keysym_group("xkb")

CONFIG_ENV_VAR = "LOCAL_STT_CONFIG"
DEFAULT_MODELS_DIR = Path.home() / ".local/share/local-stt/models"

# Frames per second of the Whisper encoder window (06 §6.7).
_FRAMES_PER_S = 50
# Lower bound of a fixed audio_ctx: only 750 and 1000 were measured (09 §9.3).
_MIN_AUDIO_CTX = 500
_MAX_SEGMENT_S = 28.0  # margin for Whisper's 30 s window
_MAX_VOCABULARY_CHARS = 300

# whisper-server flags written by the env generator itself (09 §9.3), short and long forms.
_MANAGED_FLAGS = frozenset(
    {
        "--host", "--port", "--request-path", "--inference-path", "--public", "--convert",
        "-m", "--model", "-l", "--language", "-t", "--threads", "-bs", "--beam-size",
    }
)  # fmt: skip
# Flags that make whisper-server log transcribed content to journald (12 §12.1).
_CONTENT_LOGGING_FLAGS = frozenset({"-pr", "--print-realtime", "-debug", "--debug-mode"})
# systemd splits an unbraced $VAR on whitespace and interprets quotes and backslashes.
_UNSAFE_ARG = re.compile(r"[\s\"'\\$]")

HOTKEY_MODIFIERS = frozenset({"Ctrl", "Shift", "Alt", "Super"})
# Keysyms rejected in daemon shortcuts (07 §7.2): AltGr/Mutter conflicts, keys used by XTest paste.
_CONFLICTING_KEYSYMS = {
    "ISO_Level3_Shift": "conflicts with AltGr",
    "Alt_R": "conflicts with AltGr",
    "Super_L": "conflicts with Mutter (overview key)",
    "Control_L": "used by XTest when pasting (08 §8.5)",
    "Shift_L": "used by XTest when pasting (08 §8.5)",
}


@dataclass(frozen=True)
class SttConfig:
    engine: str = "whisper-server"
    port: int = 8178
    model: str = "small-q8_0"
    models_dir: Path = DEFAULT_MODELS_DIR
    language: str = "pl"
    threads: int = 4
    beam_size: int = -1
    vocabulary_prompt: str = ""
    continuous_context: bool = True
    audio_ctx: int = 1000
    audio_ctx_margin: int = 128
    no_speech_threshold: float = 0.6
    logprob_threshold: float = -1.0
    startup_timeout_s: float = 60.0
    request_timeout_max_s: float = 120.0
    extra_server_args: tuple[str, ...] = ()

    @property
    def model_path(self) -> Path:
        return self.models_dir / f"ggml-{self.model}.bin"


@dataclass(frozen=True)
class AudioConfig:
    device: str = "default"


@dataclass(frozen=True)
class PttConfig:
    min_duration_ms: int = 300
    max_duration_s: float = 120.0
    silence_rms_dbfs: float = -50.0


@dataclass(frozen=True)
class ContinuousConfig:
    max_backlog_s: float = 60.0


@dataclass(frozen=True)
class VadConfig:
    enabled: bool = True
    model: str = "silero_vad.onnx"
    start_threshold: float = 0.50
    end_threshold: float = 0.35
    min_speech_ms: int = 250
    min_silence_ms: int = 700
    speech_pad_ms: int = 300
    max_segment_s: float = 15.0
    split_search_s: float = 3.0


@dataclass(frozen=True)
class HotkeysConfig:
    enabled: bool = True
    push_to_talk: str = "Control_R"
    continuous_toggle: str = "Shift+Control_R"
    ptt_cancel_key: str = "Escape"


@dataclass(frozen=True)
class Replacement:
    pattern: str
    replace: str
    regex: bool = False


DEFAULT_HALLUCINATION_PATTERNS = (
    r"napisy (stworzone|wykonane) przez społeczność amara\.org",
    r"(zdjęcia|tłumaczenie) i napisy stworzone przez społeczność amara\.org",
    r"^\s*dzięk(i|uję) za (uwagę|obejrzenie|oglądanie)[.!]?\s*$",
    r"^\s*(za)?subskrybuj[^.]*[.!]?\s*$",
)


@dataclass(frozen=True)
class TextConfig:
    append_space: bool = True
    hallucination_patterns: tuple[str, ...] = DEFAULT_HALLUCINATION_PATTERNS
    replacements: tuple[Replacement, ...] = ()


@dataclass(frozen=True)
class InjectionConfig:
    backend: str = "auto"
    restore_clipboard: bool = True
    modifier_wait_ms: int = 1000
    paste_timeout_ms: int = 1000
    type_delay_ms: int = 12
    type_window_classes: tuple[str, ...] = ("xterm", "URxvt")
    terminal_window_classes: tuple[str, ...] = (
        "gnome-terminal-server", "kitty", "alacritty", "konsole", "tilix",
        "org.wezfurlong.wezterm", "terminator", "xfce4-terminal", "guake", "tabby", "ghostty",
    )  # fmt: skip
    paste_shortcut_overrides: Mapping[str, str] = field(
        default_factory=lambda: types.MappingProxyType({})
    )


@dataclass(frozen=True)
class FeedbackConfig:
    sounds: bool = True
    sound_volume: float = 0.4
    notifications: str = "errors"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    log_text: bool = False
    timings: bool = True


@dataclass(frozen=True)
class Config:
    stt: SttConfig = field(default_factory=SttConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    ptt: PttConfig = field(default_factory=PttConfig)
    continuous: ContinuousConfig = field(default_factory=ContinuousConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)
    text: TextConfig = field(default_factory=TextConfig)
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def vad_model_path(self) -> Path:
        return self.stt.models_dir / self.vad.model


class ConfigError(Exception):
    """Invalid configuration; `errors` holds every problem found (exit code 78, 10 §10.1)."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


# --- paths -------------------------------------------------------------------------------


def config_dir(environ: Mapping[str, str] = os.environ) -> Path:
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "local-stt"


def resolve_config_path(
    cli_path: Path | None = None, environ: Mapping[str, str] = os.environ
) -> tuple[Path, bool]:
    """`--config` > `$LOCAL_STT_CONFIG` > the XDG default; the flag says whether it was explicit."""
    if cli_path is not None:
        return cli_path.expanduser(), True
    if environ.get(CONFIG_ENV_VAR):
        return Path(environ[CONFIG_ENV_VAR]).expanduser(), True
    return config_dir(environ) / "config.toml", False


# --- loading -----------------------------------------------------------------------------


def load_config(
    cli_path: Path | None = None, environ: Mapping[str, str] = os.environ
) -> tuple[Config, list[str]]:
    """Returns the validated config and its warnings; raises ConfigError.

    A missing file at the default location means defaults (09 §9.1); an explicitly given path
    must exist.
    """
    path, explicit = resolve_config_path(cli_path, environ)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ConfigError([f"{path}: file not found"]) from None
        return parse_config({})
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError([f"{path}: {e}"]) from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{path}: {e}"]) from e
    return parse_config(data)


def parse_config(data: Mapping[str, Any]) -> tuple[Config, list[str]]:
    """Builds and validates a Config from parsed TOML; raises ConfigError."""
    errors: list[str] = []
    sections: dict[str, Any] = {}
    section_types = get_type_hints(Config)
    for name, value in data.items():
        if name not in section_types:
            errors.append(f"{name}: unknown section")
        elif not isinstance(value, dict):
            errors.append(f"{name}: must be a table (got {_show(value)})")
        else:
            sections[name] = _build_section(name, section_types[name], value, errors)
    if errors:
        raise ConfigError(errors)
    config = Config(**sections)
    warnings = _validate(config, errors)
    if errors:
        raise ConfigError(errors)
    return config, warnings


def _build_section(name: str, cls: type[Any], table: dict[str, Any], errors: list[str]) -> Any:
    hints = get_type_hints(cls)
    values: dict[str, Any] = {}
    for key, raw in table.items():
        if key not in hints:
            errors.append(f"{name}.{key}: unknown key")
            continue
        try:
            values[key] = _convert(hints[key], raw, f"{name}.{key}")
        except _TypeProblem as e:
            errors.append(str(e))
    return cls(**values)


class _TypeProblem(Exception):
    pass


def _convert(hint: Any, raw: Any, where: str) -> Any:
    def fail(expected: str) -> _TypeProblem:
        return _TypeProblem(f"{where}: must be {expected} (got {_show(raw)})")

    if hint is bool:
        if not isinstance(raw, bool):
            raise fail("true or false")
        return raw
    if hint is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise fail("an integer")
        return raw
    if hint is float:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise fail("a number")
        return float(raw)
    if hint is str:
        if not isinstance(raw, str):
            raise fail("a string")
        return raw
    if hint is Path:
        if not isinstance(raw, str) or not raw:
            raise fail("a non-empty path string")
        return Path(raw).expanduser()
    if hint == tuple[str, ...]:
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise fail("a list of strings")
        return tuple(raw)
    if hint == Mapping[str, str]:
        if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
            raise fail("a table of strings")
        return types.MappingProxyType(dict(raw))
    if hint == tuple[Replacement, ...]:
        if not isinstance(raw, list):
            raise fail("an array of tables")
        return tuple(_replacement(item, f"{where}[{i}]") for i, item in enumerate(raw))
    raise AssertionError(f"{where}: unsupported field type {hint!r}")  # pragma: no cover


def _replacement(raw: Any, where: str) -> Replacement:
    if not isinstance(raw, dict):
        raise _TypeProblem(f"{where}: must be a table (got {_show(raw)})")
    hints = get_type_hints(Replacement)
    unknown = sorted(set(raw) - set(hints))
    if unknown:
        raise _TypeProblem(f"{where}.{unknown[0]}: unknown key")
    missing = [f.name for f in fields(Replacement) if f.name not in raw and f.name != "regex"]
    if missing:
        raise _TypeProblem(f"{where}.{missing[0]}: required key missing")
    return Replacement(**{k: _convert(hints[k], v, f"{where}.{k}") for k, v in raw.items()})


def _show(value: Any) -> str:
    """A value as it would appear in TOML, for error messages."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Path):
        return json.dumps(str(value), ensure_ascii=False)
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return repr(value)


# --- validation (09 §9.3) ----------------------------------------------------------------


def _validate(config: Config, errors: list[str]) -> list[str]:
    """Appends errors in place and returns warnings."""
    warnings: list[str] = []

    def check(ok: bool, key: str, problem: str, value: Any) -> None:
        if not ok:
            errors.append(f"{key}: {problem} (got {_show(value)})")

    stt = config.stt
    check(stt.engine == "whisper-server", "stt.engine", 'must be "whisper-server"', stt.engine)
    check(1 <= stt.port <= 65535, "stt.port", "must be in 1-65535", stt.port)
    check(
        re.fullmatch(r"[A-Za-z0-9._-]+", stt.model) is not None,
        "stt.model",
        "must be a model name such as small-q8_0",
        stt.model,
    )
    check(
        re.fullmatch(r"[a-z]{2,3}|auto", stt.language) is not None,
        "stt.language",
        'must be a language code such as "pl"',
        stt.language,
    )
    check(stt.threads >= 1, "stt.threads", "must be >= 1", stt.threads)
    check(
        stt.beam_size == -1 or stt.beam_size >= 1,
        "stt.beam_size",
        "must be -1 or >= 1",
        stt.beam_size,
    )
    check(
        len(stt.vocabulary_prompt) <= _MAX_VOCABULARY_CHARS,
        "stt.vocabulary_prompt",
        f"must be at most {_MAX_VOCABULARY_CHARS} characters",
        f"{len(stt.vocabulary_prompt)} characters",
    )
    check(
        stt.audio_ctx == 0 or _MIN_AUDIO_CTX <= stt.audio_ctx < FULL_AUDIO_CTX,
        "stt.audio_ctx",
        f"must be 0 or {_MIN_AUDIO_CTX}-{FULL_AUDIO_CTX - 1}",
        stt.audio_ctx,
    )
    check(
        stt.audio_ctx_margin >= 0 and (stt.audio_ctx == 0 or stt.audio_ctx_margin < stt.audio_ctx),
        "stt.audio_ctx_margin",
        "must be >= 0 and < audio_ctx",
        stt.audio_ctx_margin,
    )
    check(
        0.0 <= stt.no_speech_threshold <= 1.0,
        "stt.no_speech_threshold",
        "must be in [0, 1]",
        stt.no_speech_threshold,
    )
    check(stt.startup_timeout_s > 0, "stt.startup_timeout_s", "must be > 0", stt.startup_timeout_s)
    check(
        stt.request_timeout_max_s > 0,
        "stt.request_timeout_max_s",
        "must be > 0",
        stt.request_timeout_max_s,
    )
    check(
        _UNSAFE_ARG.search(str(stt.model_path)) is None,
        "stt.models_dir",
        "must not contain whitespace, quotes, backslashes or $ (whisper-server.env)",
        stt.models_dir,
    )
    for i, arg in enumerate(stt.extra_server_args):
        flag = arg.split("=", 1)[0]
        key = f"stt.extra_server_args[{i}]"
        if flag in _MANAGED_FLAGS:
            errors.append(f"stt.extra_server_args: flag {flag} is managed by local-stt")
        elif flag in _CONTENT_LOGGING_FLAGS:
            errors.append(f"stt.extra_server_args: flag {flag} would log transcribed content")
        else:
            check(
                arg != "" and _UNSAFE_ARG.search(arg) is None,
                key,
                "must be non-empty without whitespace, quotes, backslashes or $",
                arg,
            )

    check(
        config.audio.device != "",
        "audio.device",
        'must be "default" or a PipeWire node name',
        config.audio.device,
    )

    ptt = config.ptt
    check(ptt.min_duration_ms >= 0, "ptt.min_duration_ms", "must be >= 0", ptt.min_duration_ms)
    check(
        ptt.max_duration_s > ptt.min_duration_ms / 1000,
        "ptt.max_duration_s",
        "must be > min_duration_ms",
        ptt.max_duration_s,
    )
    check(ptt.silence_rms_dbfs < 0, "ptt.silence_rms_dbfs", "must be < 0", ptt.silence_rms_dbfs)
    check(
        config.continuous.max_backlog_s > 0,
        "continuous.max_backlog_s",
        "must be > 0",
        config.continuous.max_backlog_s,
    )

    vad = config.vad
    check(vad.model != "", "vad.model", "must be a file name in models_dir", vad.model)
    for name in ("start_threshold", "end_threshold"):
        value = getattr(vad, name)
        check(0.0 < value < 1.0, f"vad.{name}", "must be in (0, 1)", value)
    check(
        vad.end_threshold < vad.start_threshold,
        "vad.end_threshold",
        "must be < start_threshold",
        vad.end_threshold,
    )
    for name in ("min_speech_ms", "min_silence_ms", "speech_pad_ms"):
        value = getattr(vad, name)
        check(value >= 0, f"vad.{name}", "must be >= 0", value)
    check(
        0 < vad.max_segment_s <= _MAX_SEGMENT_S,
        "vad.max_segment_s",
        f"must be in (0, {_MAX_SEGMENT_S:g}] (margin for Whisper's 30 s window)",
        vad.max_segment_s,
    )
    check(
        0 < vad.split_search_s < vad.max_segment_s,
        "vad.split_search_s",
        "must be > 0 and < max_segment_s",
        vad.split_search_s,
    )
    if (
        stt.audio_ctx > 0
        and vad.max_segment_s * _FRAMES_PER_S + stt.audio_ctx_margin > stt.audio_ctx
    ):
        limit = (stt.audio_ctx - stt.audio_ctx_margin) / _FRAMES_PER_S
        warnings.append(
            f"vad.max_segment_s: continuous segments longer than {limit:g} s use the full window"
        )
    if not vad.enabled:
        warnings.append("vad.enabled=false: continuous dictation unavailable")

    _validate_hotkeys(config.hotkeys, errors)

    for i, pattern in enumerate(config.text.hallucination_patterns):
        _check_regex(pattern, f"text.hallucination_patterns[{i}]", errors)
    for i, rule in enumerate(config.text.replacements):
        if rule.regex:
            _check_regex(rule.pattern, f"text.replacements[{i}].pattern", errors)
        else:
            check(
                rule.pattern != "",
                f"text.replacements[{i}].pattern",
                "must not be empty",
                rule.pattern,
            )

    inj = config.injection
    check(
        inj.backend in ("auto", "clipboard", "type"),
        "injection.backend",
        "must be auto, clipboard or type",
        inj.backend,
    )
    for name in ("modifier_wait_ms", "paste_timeout_ms", "type_delay_ms"):
        value = getattr(inj, name)
        check(value >= 0, f"injection.{name}", "must be >= 0", value)
    for window_class, shortcut in inj.paste_shortcut_overrides.items():
        try:
            parse_hotkey(shortcut)
        except ValueError as e:
            errors.append(
                f"injection.paste_shortcut_overrides.{window_class}: {e} (got {_show(shortcut)})"
            )

    fb = config.feedback
    check(
        0.0 <= fb.sound_volume <= 1.0, "feedback.sound_volume", "must be in [0, 1]", fb.sound_volume
    )
    check(
        fb.notifications in ("none", "errors", "all"),
        "feedback.notifications",
        "must be none, errors or all",
        fb.notifications,
    )
    check(
        config.logging.level in ("INFO", "DEBUG", "TRACE"),
        "logging.level",
        "must be INFO, DEBUG or TRACE",
        config.logging.level,
    )
    return warnings


def _check_regex(pattern: str, key: str, errors: list[str]) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        errors.append(f"{key}: invalid regex: {e} (got {_show(pattern)})")


# --- hotkeys (07 §7.2) -------------------------------------------------------------------


def parse_hotkey(text: str) -> tuple[frozenset[str], str]:
    """`(modifier "+")* keysym` → (modifiers, keysym); raises ValueError.

    Checks only that the keysym name exists; whether it has a keycode in the current keyboard
    map is checked when grabbing (hotkeys/x11.py).
    """
    *mods, keysym = text.split("+")
    unknown = [m for m in mods if m not in HOTKEY_MODIFIERS]
    if unknown:
        raise ValueError(f"unknown modifier {unknown[0]!r} (use Ctrl, Shift, Alt, Super)")
    if len(set(mods)) != len(mods):
        raise ValueError("repeated modifier")
    if not keysym or XK.string_to_keysym(keysym) == XK.NoSymbol:
        raise ValueError(f"unknown keysym {keysym!r}")
    return frozenset(mods), keysym


def _validate_hotkeys(hk: HotkeysConfig, errors: list[str]) -> None:
    parsed: dict[str, tuple[frozenset[str], str]] = {}
    for name in ("push_to_talk", "continuous_toggle", "ptt_cancel_key"):
        value = getattr(hk, name)
        key = f"hotkeys.{name}"
        try:
            mods, keysym = parse_hotkey(value)
        except ValueError as e:
            errors.append(f"{key}: {e} (got {_show(value)})")
            continue
        if keysym in _CONFLICTING_KEYSYMS:
            errors.append(f"{key}: {keysym} {_CONFLICTING_KEYSYMS[keysym]} (got {_show(value)})")
            continue
        parsed[name] = (mods, keysym)

    ptt, toggle = parsed.get("push_to_talk"), parsed.get("continuous_toggle")
    if ptt is not None and ptt == toggle:
        errors.append(
            f"hotkeys.continuous_toggle: must differ from push_to_talk "
            f"(got {_show(hk.continuous_toggle)})"
        )
    cancel = parsed.get("ptt_cancel_key")
    if cancel is not None:
        if cancel[0]:
            errors.append(
                f"hotkeys.ptt_cancel_key: must be a single keysym without modifiers "
                f"(got {_show(hk.ptt_cancel_key)})"
            )
        elif cancel[1] in {k for _, k in (s for s in (ptt, toggle) if s is not None)}:
            errors.append(
                f"hotkeys.ptt_cancel_key: must differ from the push_to_talk and "
                f"continuous_toggle keysyms (got {_show(hk.ptt_cancel_key)})"
            )


# --- files the daemon needs --------------------------------------------------------------


def check_model_files(config: Config) -> list[str]:
    """Errors for missing model files (09 §9.3). Separate from parse_config so that
    `models pull` and `transcribe --model` work before the configured model is downloaded."""
    errors = []
    if not config.stt.model_path.is_file():
        errors.append(
            f"stt.model: file not found: {config.stt.model_path} "
            f"(run: local-stt models pull {config.stt.model})"
        )
    if config.vad.enabled and not config.vad_model_path.is_file():
        errors.append(
            f"vad.model: file not found: {config.vad_model_path} "
            "(run: local-stt models pull silero-vad)"
        )
    return errors


# --- whisper-server.env (09 §9.4) --------------------------------------------------------


def whisper_server_args(stt: SttConfig, request_path: str) -> list[str]:
    # fmt: off
    return [
        "--host", HOST, "--port", str(stt.port), "--request-path", request_path,
        "-m", str(stt.model_path), "-l", stt.language, "-t", str(stt.threads),
        "-bs", str(stt.beam_size), "-sns", *stt.extra_server_args,
    ]
    # fmt: on


def render_whisper_env(stt: SttConfig, request_path: str) -> str:
    args = whisper_server_args(stt, request_path)
    # parse_config rejects unsafe characters in config values; this guards the request path.
    unsafe = [a for a in args if _UNSAFE_ARG.search(a)]
    if unsafe:
        raise ValueError(f"whisper-server argument not representable in an env file: {unsafe[0]}")
    return (
        "# generated by local-stt — do not edit manually\n"
        f'LOCAL_STT_WHISPER_ARGS="{" ".join(args)}"\n'
    )


def write_whisper_env(path: Path, content: str) -> None:
    """Atomically replaces `path` with mode 0600 (the file contains the request-path secret)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o600)  # O_CREAT keeps the mode of an existing leftover file
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
