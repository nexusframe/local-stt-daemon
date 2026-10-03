import pytest

from local_stt.inject.x11util import Shortcut, parse_shortcut


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ctrl+V", Shortcut(("Control_L",), "v")),
        ("Ctrl+Shift+V", Shortcut(("Control_L", "Shift_L"), "v")),
        ("Ctrl+Y", Shortcut(("Control_L",), "y")),
        ("Shift+Insert", Shortcut(("Shift_L",), "Insert")),
        ("Alt+Super+F5", Shortcut(("Alt_L", "Super_L"), "F5")),
    ],
)
def test_parse_shortcut_uses_left_modifiers(text: str, expected: Shortcut) -> None:
    assert parse_shortcut(text) == expected
