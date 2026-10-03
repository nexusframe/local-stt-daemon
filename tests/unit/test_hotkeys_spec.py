# ruff: noqa: E501  (parametrize tables: one case per line, expected messages verbatim)
from typing import Any

import pytest

from local_stt.config import HotkeysConfig
from local_stt.hotkeys.spec import parse_hotkey, validate_hotkeys


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("Control_R", (frozenset(), "Control_R")),
        ("Shift+Control_R", (frozenset({"Shift"}), "Control_R")),
        ("Super+Alt+F9", (frozenset({"Super", "Alt"}), "F9")),
        ("ISO_Level3_Shift", (frozenset(), "ISO_Level3_Shift")),  # xkb keysym group loaded
    ],
)
def test_parse(text: str, parsed: tuple[frozenset[str], str]) -> None:
    assert parse_hotkey(text) == parsed


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"continuous_toggle": "Control_R"}, 'hotkeys.continuous_toggle: must differ from push_to_talk (got "Control_R")'),
        ({"push_to_talk": "Alt_R"}, 'hotkeys.push_to_talk: Alt_R conflicts with AltGr (got "Alt_R")'),
        ({"push_to_talk": "ISO_Level3_Shift"}, 'hotkeys.push_to_talk: ISO_Level3_Shift conflicts with AltGr (got "ISO_Level3_Shift")'),
        ({"push_to_talk": "Super_L"}, 'hotkeys.push_to_talk: Super_L conflicts with Mutter (overview key) (got "Super_L")'),
        ({"push_to_talk": "Control_L"}, 'hotkeys.push_to_talk: Control_L used by XTest when pasting (08 §8.5) (got "Control_L")'),
        ({"continuous_toggle": "Ctrl+Shift_L"}, 'hotkeys.continuous_toggle: Shift_L used by XTest when pasting (08 §8.5) (got "Ctrl+Shift_L")'),
        ({"ptt_cancel_key": "Ctrl+Escape"}, 'hotkeys.ptt_cancel_key: must be a single keysym without modifiers (got "Ctrl+Escape")'),
        ({"ptt_cancel_key": "Control_R"}, 'hotkeys.ptt_cancel_key: must differ from the push_to_talk and continuous_toggle keysyms (got "Control_R")'),
        ({"push_to_talk": "Hyper+F9"}, "hotkeys.push_to_talk: unknown modifier 'Hyper' (use Ctrl, Shift, Alt, Super) (got \"Hyper+F9\")"),
        ({"push_to_talk": "Ctrl+Ctrl+F9"}, 'hotkeys.push_to_talk: repeated modifier (got "Ctrl+Ctrl+F9")'),
        ({"push_to_talk": "Ctrl+"}, "hotkeys.push_to_talk: unknown keysym '' (got \"Ctrl+\")"),
        ({"push_to_talk": "Kontrol_R"}, "hotkeys.push_to_talk: unknown keysym 'Kontrol_R' (got \"Kontrol_R\")"),
    ],
)  # fmt: skip
def test_rules(fields: dict[str, Any], message: str) -> None:
    assert validate_hotkeys(HotkeysConfig(**fields)) == [message]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"push_to_talk": "Pause", "continuous_toggle": "Ctrl+Pause"},
        {"push_to_talk": "Super+F9", "ptt_cancel_key": "space"},
        {"push_to_talk": "Shift+Control_R", "continuous_toggle": "Control_R"},
    ],
)
def test_valid(fields: dict[str, Any]) -> None:
    assert validate_hotkeys(HotkeysConfig(**fields)) == []


def test_all_problems_reported() -> None:
    errors = validate_hotkeys(HotkeysConfig(push_to_talk="Alt_R", ptt_cancel_key="Nope"))
    assert [e.split(":")[0] for e in errors] == ["hotkeys.push_to_talk", "hotkeys.ptt_cancel_key"]
