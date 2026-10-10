# ruff: noqa: E501  (parametrize tables: one case per line, expected messages verbatim)
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from local_stt import config as cfg
from local_stt.cli import main
from local_stt.config import Config, ConfigError, parse_config

REPO = Path(__file__).resolve().parents[2]
SECRET_PATH = "/" + "ab" * 16


def errors_of(data: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    return exc.value.errors


def single_error(data: dict[str, Any]) -> str:
    errors = errors_of(data)
    assert len(errors) == 1, errors
    return errors[0]


# --- defaults and loading ----------------------------------------------------------------


def test_empty_config_gives_defaults_from_benchmark() -> None:
    config, warnings = parse_config({})
    assert config == Config()
    assert warnings == []
    assert (config.stt.model, config.stt.threads, config.stt.audio_ctx) == ("small-q8_0", 4, 1000)


def test_example_file_matches_defaults() -> None:
    data = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
    assert parse_config(data) == (Config(), [])


def test_values_are_converted() -> None:
    config, _ = parse_config(
        {
            "stt": {"models_dir": "~/m", "extra_server_args": ["-nf"], "startup_timeout_s": 5},
            "text": {"replacements": [{"pattern": "kropka", "replace": "."}]},
            "injection": {"paste_shortcut_overrides": {"emacs": "Ctrl+Y"}},
        }
    )
    assert config.stt.models_dir == Path.home() / "m"
    assert config.stt.model_path == Path.home() / "m/ggml-small-q8_0.bin"
    assert config.stt.extra_server_args == ("-nf",)
    assert config.stt.startup_timeout_s == 5.0
    assert config.text.replacements == (cfg.Replacement("kropka", ".", regex=False),)
    assert config.injection.paste_shortcut_overrides["emacs"] == "Ctrl+Y"


def test_unknown_section_and_key_are_errors() -> None:
    errors = errors_of({"sst": {}, "stt": {"modle": "x"}})
    assert errors == ["sst: unknown section", "stt.modle: unknown key"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"stt": {"port": "8178"}}, 'stt.port: must be an integer (got "8178")'),
        ({"stt": {"port": True}}, "stt.port: must be an integer (got true)"),
        ({"stt": {"no_speech_threshold": "x"}}, 'stt.no_speech_threshold: must be a number (got "x")'),
        ({"vad": {"enabled": 1}}, "vad.enabled: must be true or false (got 1)"),
        ({"audio": {"device": 3}}, "audio.device: must be a string (got 3)"),
        ({"stt": {"models_dir": ""}}, 'stt.models_dir: must be a non-empty path string (got "")'),
        ({"stt": {"extra_server_args": "-nf"}}, 'stt.extra_server_args: must be a list of strings (got "-nf")'),
        ({"stt": 5}, "stt: must be a table (got 5)"),
        ({"text": {"replacements": [{"pattern": "a"}]}}, "text.replacements[0].replace: required key missing"),
        ({"text": {"replacements": [{"pattern": "a", "replace": "b", "x": 1}]}}, "text.replacements[0].x: unknown key"),
        ({"injection": {"paste_shortcut_overrides": {"emacs": 1}}}, 'injection.paste_shortcut_overrides: must be a table of strings (got {"emacs": 1})'),
    ],
)  # fmt: skip
def test_type_errors(data: dict[str, Any], message: str) -> None:
    assert single_error(data) == message


def test_all_errors_are_collected() -> None:
    errors = errors_of({"stt": {"port": 0, "threads": 0}, "vad": {"max_segment_s": 40}})
    assert [e.split(":")[0] for e in errors] == ["stt.port", "stt.threads", "vad.max_segment_s"]


# --- cross-field rules (09 §9.3) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"vad": {"end_threshold": 0.6}}, "vad.end_threshold: must be < start_threshold (got 0.6)"),
        ({"vad": {"start_threshold": 1.0, "end_threshold": 0.5}}, "vad.start_threshold: must be in (0, 1) (got 1.0)"),
        ({"vad": {"end_threshold": 0.0}}, "vad.end_threshold: must be in (0, 1) (got 0.0)"),
        ({"vad": {"split_search_s": 15}}, "vad.split_search_s: must be > 0 and < max_segment_s (got 15.0)"),
        ({"vad": {"max_segment_s": 29}}, "vad.max_segment_s: must be in (0, 28] (margin for Whisper's 30 s window) (got 29.0)"),
        ({"stt": {"audio_ctx": 499}}, "stt.audio_ctx: must be 0 or 500-1499 (got 499)"),
        ({"stt": {"audio_ctx": 1500}}, "stt.audio_ctx: must be 0 or 500-1499 (got 1500)"),
        ({"stt": {"audio_ctx_margin": 1000}}, "stt.audio_ctx_margin: must be >= 0 and < audio_ctx (got 1000)"),
        ({"stt": {"audio_ctx_margin": -1}}, "stt.audio_ctx_margin: must be >= 0 and < audio_ctx (got -1)"),
        ({"stt": {"vocabulary_prompt": "x" * 301}}, 'stt.vocabulary_prompt: must be at most 300 characters (got "301 characters")'),
        ({"hotkeys": {"continuous_toggle": "Control_R"}}, 'hotkeys.continuous_toggle: must differ from push_to_talk (got "Control_R")'),
        ({"stt": {"extra_server_args": ["--port", "1"]}}, "stt.extra_server_args: flag --port is managed by local-stt"),
        ({"stt": {"extra_server_args": ["--host=0.0.0.0"]}}, "stt.extra_server_args: flag --host is managed by local-stt"),
        ({"stt": {"extra_server_args": ["-pr"]}}, "stt.extra_server_args: flag -pr would log transcribed content"),
        ({"stt": {"extra_server_args": ["--debug-mode"]}}, "stt.extra_server_args: flag --debug-mode would log transcribed content"),
        ({"stt": {"extra_server_args": ["-nf x"]}}, 'stt.extra_server_args[0]: must be non-empty without whitespace, quotes, backslashes or $ (got "-nf x")'),
        ({"stt": {"models_dir": "/tmp/my models"}}, 'stt.models_dir: must not contain whitespace, quotes, backslashes or $ (whisper-server.env) (got "/tmp/my models")'),
        ({"text": {"hallucination_patterns": ["ok", "(unclosed"]}}, 'text.hallucination_patterns[1]: invalid regex: missing ), unterminated subpattern at position 0 (got "(unclosed")'),
        ({"text": {"replacements": [{"pattern": "(", "replace": "", "regex": True}]}}, 'text.replacements[0].pattern: invalid regex: missing ), unterminated subpattern at position 0 (got "(")'),
        ({"text": {"replacements": [{"pattern": "", "replace": "x"}]}}, 'text.replacements[0].pattern: must not be empty (got "")'),
        ({"text": {"replacements": [{"pattern": "(a)", "replace": "\\2", "regex": True}]}}, 'text.replacements[0].replace: invalid replacement template: invalid group reference 2 at position 1 (got "\\\\2")'),
        ({"text": {"replacements": [{"pattern": "a", "replace": "\\q", "regex": True}]}}, 'text.replacements[0].replace: invalid replacement template: bad escape \\q at position 0 (got "\\\\q")'),
        ({"text": {"replacements": [{"pattern": "(?P<n>a)", "replace": "\\g<m>", "regex": True}]}}, """text.replacements[0].replace: invalid replacement template: unknown group name 'm' (got "\\\\g<m>")"""),
        ({"injection": {"paste_shortcut_overrides": {"emacs": "Ctrl+Nope"}}}, "injection.paste_shortcut_overrides.emacs: unknown keysym 'Nope' (got \"Ctrl+Nope\")"),
        ({"stt": {"engine": "vosk"}}, 'stt.engine: must be one of "whisper-server", "parakeet" (got "vosk")'),
        ({"stt": {"languages": ["pl", "en", "pl"]}}, 'stt.languages: must not repeat a language (got ["pl", "en", "pl"])'),
        ({"stt": {"languages": []}}, "stt.languages: must name at least one language (got [])"),
        ({"stt": {"port": 70000}}, "stt.port: must be in 1-65535 (got 70000)"),
        ({"stt": {"model": "../x"}}, 'stt.model: must be a model name such as small-q8_0 (got "../x")'),
        ({"stt": {"languages": ["pl", "Polish"]}}, 'stt.languages[1]: must be a language code such as "pl" (got "Polish")'),
        ({"stt": {"language": "pl"}}, 'stt.language: replaced by stt.languages, the first is the startup language: languages = ["pl", "en"]'),
        ({"stt": {"beam_size": 0}}, "stt.beam_size: must be -1 or >= 1 (got 0)"),
        ({"ptt": {"max_duration_s": 0.2}}, "ptt.max_duration_s: must be > min_duration_ms (got 0.2)"),
        ({"injection": {"backend": "xdotool"}}, 'injection.backend: must be auto, clipboard, type or clipboard-only (got "xdotool")'),
        ({"feedback": {"sound_volume": 1.5}}, "feedback.sound_volume: must be in [0, 1] (got 1.5)"),
        ({"feedback": {"notifications": "some"}}, 'feedback.notifications: must be none, errors or all (got "some")'),
        ({"logging": {"level": "debug"}}, 'logging.level: must be INFO, DEBUG or TRACE (got "debug")'),
    ],
)  # fmt: skip
def test_validation_rules(data: dict[str, Any], message: str) -> None:
    assert single_error(data) == message


@pytest.mark.parametrize(
    "data",
    [
        {
            "stt": {"audio_ctx": 0, "audio_ctx_margin": 5000}
        },  # margin is unused with the full window
        {"stt": {"audio_ctx": 500}},
        {"stt": {"audio_ctx": 1499}},
        {"hotkeys": {"push_to_talk": "Pause", "continuous_toggle": "Ctrl+Pause"}},
        {"hotkeys": {"push_to_talk": "Super+F9", "ptt_cancel_key": "space"}},
        {"text": {"replacements": [{"pattern": "(", "replace": ")"}]}},  # plain text, not a regex
    ],
)
def test_valid_edge_cases(data: dict[str, Any]) -> None:
    parse_config(data)


def test_warning_when_segments_exceed_audio_ctx() -> None:
    _, warnings = parse_config({"vad": {"max_segment_s": 20}})
    assert warnings == [
        "vad.max_segment_s: continuous segments longer than 17.44 s use the full window"
    ]


def test_no_segment_warning_with_full_window() -> None:
    assert parse_config({"stt": {"audio_ctx": 0}, "vad": {"max_segment_s": 28}})[1] == []


def test_warning_when_vad_disabled() -> None:
    _, warnings = parse_config({"vad": {"enabled": False}})
    assert warnings == ["vad.enabled=false: continuous dictation unavailable"]


# --- model files -------------------------------------------------------------------------


def test_check_model_files(tmp_path: Path) -> None:
    config, _ = parse_config({"stt": {"models_dir": str(tmp_path), "engine": "whisper-server"}})
    assert cfg.check_model_files(config) == [
        f"stt.model: file not found: {tmp_path}/ggml-small-q8_0.bin "
        "(run: local-stt models pull small-q8_0)",
        f"vad.model: file not found: {tmp_path}/silero_vad.onnx "
        "(run: local-stt models pull silero-vad)",
    ]
    (tmp_path / "ggml-small-q8_0.bin").touch()
    (tmp_path / "silero_vad.onnx").touch()
    assert cfg.check_model_files(config) == []


def test_parakeet_needs_its_model_directory_not_the_whisper_model(tmp_path: Path) -> None:
    (tmp_path / "silero_vad.onnx").touch()
    config, _ = parse_config({"stt": {"models_dir": str(tmp_path), "engine": "parakeet"}})
    assert cfg.check_model_files(config) == [
        f"stt.engine: parakeet model not found: {tmp_path}/parakeet-tdt-0.6b-v3-int8 "
        "(run: local-stt models pull parakeet-tdt-0.6b-v3-int8)"
    ]
    (tmp_path / "parakeet-tdt-0.6b-v3-int8").mkdir()
    assert cfg.check_model_files(config) == []


def test_vad_model_not_required_when_vad_disabled(tmp_path: Path) -> None:
    (tmp_path / "parakeet-tdt-0.6b-v3-int8").mkdir()
    config, _ = parse_config({"stt": {"models_dir": str(tmp_path)}, "vad": {"enabled": False}})
    assert cfg.check_model_files(config) == []


# --- paths -------------------------------------------------------------------------------


def test_resolve_config_path_precedence(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "LOCAL_STT_CONFIG": str(tmp_path / "env.toml")}
    assert cfg.resolve_config_path(tmp_path / "cli.toml", env) == (tmp_path / "cli.toml", True)
    assert cfg.resolve_config_path(None, env) == (tmp_path / "env.toml", True)
    del env["LOCAL_STT_CONFIG"]
    assert cfg.resolve_config_path(None, env) == (tmp_path / "xdg/local-stt/config.toml", False)
    assert cfg.resolve_config_path(None, {})[0] == Path.home() / ".config/local-stt/config.toml"


def test_load_missing_default_file_gives_defaults(tmp_path: Path) -> None:
    assert cfg.load_config(None, {"XDG_CONFIG_HOME": str(tmp_path)}) == (Config(), [])


def test_load_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="file not found"):
        cfg.load_config(tmp_path / "nope.toml", {})


def test_load_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[stt]\nmodel = "small-q5_1"\n', encoding="utf-8")
    config, _ = cfg.load_config(path, {})
    assert config.stt.model == "small-q5_1"


def test_load_reports_toml_syntax_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[stt\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        cfg.load_config(path, {})
    assert exc.value.errors[0].startswith(f"{path}: ")


# --- whisper-server.env (09 §9.4) --------------------------------------------------------


def test_render_whisper_env() -> None:
    config, _ = parse_config(
        {"stt": {"models_dir": "/m", "extra_server_args": ["-nf"], "beam_size": 5}}
    )
    assert cfg.render_whisper_env(config.stt, SECRET_PATH) == (
        "# generated by local-stt — do not edit manually\n"
        f'LOCAL_STT_WHISPER_ARGS="--host 127.0.0.1 --port 8178 --request-path {SECRET_PATH} '
        '-m /m/ggml-small-q8_0.bin -l pl -t 4 -bs 5 -sns -nf"\n'
    )


def test_render_whisper_env_rejects_unsafe_request_path() -> None:
    with pytest.raises(ValueError, match="not representable"):
        cfg.render_whisper_env(Config().stt, "/a b")


def test_write_whisper_env_is_private_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sub/whisper-server.env"
    cfg.write_whisper_env(path, "A=1\n")
    path.chmod(0o644)
    cfg.write_whisper_env(path, "A=2\n")
    assert path.read_text(encoding="utf-8") == "A=2\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.listdir(path.parent) == ["whisper-server.env"]


# --- CLI ---------------------------------------------------------------------------------


def test_cli_reports_every_config_error_with_exit_78(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[stt]\nport = 0\nthreads = 0\n", encoding="utf-8")
    assert main(["models", "list", "--config", str(path)]) == 78
    assert capsys.readouterr().err.splitlines() == [
        "config error: stt.port: must be in 1-65535 (got 0)",
        "config error: stt.threads: must be >= 1 (got 0)",
    ]


def test_cli_models_use_configured_models_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ggml-small-q8_0.bin").write_bytes(b"\0" * (1 << 20))
    path = tmp_path / "config.toml"
    path.write_text(f'[stt]\nmodels_dir = "{tmp_path}"\n', encoding="utf-8")
    monkeypatch.setenv("LOCAL_STT_CONFIG", str(path))
    assert main(["models", "list"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("small-q8_0 "))
    assert "1.0 MiB" in line


def test_cli_prints_config_warnings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[vad]\nenabled = false\n", encoding="utf-8")
    assert main(["models", "list", "--config", str(path)]) == 0
    assert "config warning: vad.enabled=false" in capsys.readouterr().err


# --- conversation (task 6.4) ----------------------------------------------------------------


def test_speculative_ms_default_and_range() -> None:
    assert Config().conversation.speculative_ms == 250
    assert parse_config({"conversation": {"speculative_ms": 0}})[0].conversation.speculative_ms == 0
    assert errors_of({"conversation": {"speculative_ms": -1}})
    (error,) = errors_of({"conversation": {"speculative_ms": 700}})  # = vad.min_silence_ms
    assert "conversation.speculative_ms" in error
