"""Hotkey syntax and validation (docs/07-hotkeys-x11.md §7.2), without an X connection.

`hotkey := (modifier "+")* keysym`. Whether the keysym has a keycode in the current keyboard
map is checked when grabbing (hotkeys/x11.py).
"""

import json
from typing import TYPE_CHECKING

from Xlib import XK

if TYPE_CHECKING:
    from local_stt.config import HotkeysConfig

# python-xlib loads only the latin1 and miscellany keysyms by default; ISO_Level3_Shift is in xkb.
XK.load_keysym_group("xkb")

HOTKEY_MODIFIERS = frozenset({"Ctrl", "Shift", "Alt", "Super"})
# Keysyms rejected in daemon shortcuts: AltGr/Mutter conflicts, keys used by XTest paste.
CONFLICTING_KEYSYMS = {
    "ISO_Level3_Shift": "conflicts with AltGr",
    "Alt_R": "conflicts with AltGr",
    "Super_L": "conflicts with Mutter (overview key)",
    "Control_L": "used by XTest when pasting (08 §8.5)",
    "Shift_L": "used by XTest when pasting (08 §8.5)",
}
HOTKEY_NAMES = ("push_to_talk", "continuous_toggle", "ptt_cancel_key", "language_toggle")
# Keys that may be "" (no hotkey).
OPTIONAL_HOTKEYS = frozenset({"language_toggle"})


def parse_hotkey(text: str) -> tuple[frozenset[str], str]:
    """`(modifier "+")* keysym` → (modifiers, keysym); raises ValueError.

    Checks only that the keysym name exists in python-xlib's tables.
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


def validate_hotkeys(hk: "HotkeysConfig") -> list[str]:
    """All §7.2 problems as `hotkeys.<key>: <problem> (got "<value>")`."""
    errors: list[str] = []
    parsed: dict[str, tuple[frozenset[str], str]] = {}
    for name in HOTKEY_NAMES:
        value = getattr(hk, name)
        key = f"hotkeys.{name}"
        if value == "" and name in OPTIONAL_HOTKEYS:
            continue
        try:
            mods, keysym = parse_hotkey(value)
        except ValueError as e:
            errors.append(f"{key}: {e} (got {_show(value)})")
            continue
        if keysym in CONFLICTING_KEYSYMS:
            errors.append(f"{key}: {keysym} {CONFLICTING_KEYSYMS[keysym]} (got {_show(value)})")
            continue
        parsed[name] = (mods, keysym)

    ptt, toggle = parsed.get("push_to_talk"), parsed.get("continuous_toggle")
    language = parsed.get("language_toggle")
    if ptt is not None and ptt == toggle:
        errors.append(
            f"hotkeys.continuous_toggle: must differ from push_to_talk "
            f"(got {_show(hk.continuous_toggle)})"
        )
    if language is not None and language in (ptt, toggle):
        errors.append(
            f"hotkeys.language_toggle: must differ from push_to_talk and continuous_toggle "
            f"(got {_show(hk.language_toggle)})"
        )
    cancel = parsed.get("ptt_cancel_key")
    if cancel is not None:
        if cancel[0]:
            errors.append(
                f"hotkeys.ptt_cancel_key: must be a single keysym without modifiers "
                f"(got {_show(hk.ptt_cancel_key)})"
            )
        elif cancel[1] in {k for _, k in (s for s in (ptt, toggle, language) if s is not None)}:
            errors.append(
                f"hotkeys.ptt_cancel_key: must differ from the push_to_talk, continuous_toggle "
                f"and language_toggle keysyms (got {_show(hk.ptt_cancel_key)})"
            )
    return errors


def _show(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
