import pytest

from local_stt.text.commands import DASH, apply_commands


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Parakeet outputs from the 2026-10-08/09 recordings (task 5.1)
        (
            "Lista zakupów dwóch kropek: mleko, chleb i masło.",
            "Lista zakupów: mleko, chleb i masło.",
        ),
        ("Uwaga, dwukropek. Jutro nie ma spotkania.", "Uwaga: jutro nie ma spotkania."),
        ("Warszawa, myślnik, stolice Polski.", f"Warszawa {DASH} stolice Polski."),
        (
            "jutro myślnik albo pojutrze myślnik zadzwonię.",
            f"jutro {DASH} albo pojutrze {DASH} zadzwonię.",
        ),
        ("Zrobiłem to, średnik. Teraz kolej na ciebie.", "Zrobiłem to; teraz kolej na ciebie."),
        ("Czekaj. Trzy kropki. Dobrze.", "Czekaj... Dobrze."),
        ("Czekaj 3 kropki dobrze.", "Czekaj... Dobrze."),
        (
            "This is the first sentence new line. This is the second.",
            "This is the first sentence\nThis is the second.",
        ),
        # a command alone
        ("Dwukropek.", ":"),
        ("Myślnik.", f"{DASH}"),
        ("Nowa linia.", "\n"),
    ],
)
def test_parakeet_outputs(text: str, expected: str) -> None:
    assert apply_commands(text) == expected


def test_lowercase_only_after_an_engine_sentence_end() -> None:
    assert apply_commands("Uwaga dwukropek. Jutro") == "Uwaga: jutro"
    assert apply_commands("Goście dwukropek Anna i Piotr") == "Goście: Anna i Piotr"


def test_uppercase_after_three_dots() -> None:
    assert apply_commands("czekaj trzy kropki dobrze") == "czekaj... Dobrze"


def test_new_line_keeps_the_sentence_end_before_it() -> None:
    assert apply_commands("Koniec? Nowa linia. Dalej") == "Koniec?\nDalej"
    assert apply_commands("Punkt, nowa linia, punkt") == "Punkt\npunkt"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Koniec trzy kropki nowa linia Dalej", "Koniec...\nDalej"),
        ("Czekaj trzy kropki dwukropek", "Czekaj...:"),
        ("Czekaj trzy kropki myślnik dalej", f"Czekaj... {DASH} dalej"),
    ],
)
def test_commands_in_a_row(text: str, expected: str) -> None:
    assert apply_commands(text) == expected


def test_engine_dash_next_to_the_command_is_removed() -> None:
    assert apply_commands("Warszawa - myślnik - stolica") == f"Warszawa {DASH} stolica"


def test_case_and_whitespace_variants() -> None:
    assert apply_commands("a TRZY  KROPKI b") == "a... B"
    assert apply_commands("a New Line b") == "a\nb"


@pytest.mark.parametrize(
    "text",
    [
        "Pies Kropka, przecinek.",  # removed from the set (user decision 2026-10-09)
        "Znak zapytania? Question mark.",
        "dwukropka i myślniki",  # inflected forms are not commands
        "Hello, Comma. Period. Colon.",
        "nowalinia",
        "",
    ],
)
def test_no_command(text: str) -> None:
    assert apply_commands(text) == text
