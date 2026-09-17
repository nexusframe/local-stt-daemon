import pytest

from local_stt.bench import wer


def test_normalize_keeps_polish_letters_and_removes_punctuation() -> None:
    assert wer.normalize("  Źdźbło, „trawy” — łąka!  Pierre’em (test)… ") == (  # noqa: RUF001
        "źdźbło trawy łąka pierre em test"
    )


@pytest.mark.parametrize(
    ("ref", "hyp", "expected"),
    [
        ("ala ma kota", "ala ma kota", 0),
        ("ala ma kota", "ala ma psa", 1),  # substitution
        ("ala ma kota", "ala kota", 1),  # deletion
        ("ala ma kota", "ala ma tego kota", 1),  # insertion
        ("", "ala", 1),
        ("ala", "", 1),
    ],
)
def test_word_edit_distance(ref: str, hyp: str, expected: int) -> None:
    assert wer.edit_distance(ref.split(), hyp.split()) == expected


def test_missing_diacritic_is_an_error() -> None:
    c = wer.error_counts("Zażółć gęślą jaźń.", "zazolc gesla jazn")
    assert (c.word_errors, c.ref_words) == (3, 3)
    assert c.char_errors == 9 and c.cer == pytest.approx(9 / len("zażółć gęślą jaźń"))


def test_case_and_punctuation_do_not_count() -> None:
    c = wer.error_counts("Dzisiaj pada deszcz.", " dzisiaj Pada deszcz")
    assert c.wer == 0.0 and c.cer == 0.0


def test_corpus_rates_weight_by_reference_length() -> None:
    short = wer.error_counts("ala", "ola")  # 1/1 words
    long = wer.error_counts("jeden dwa trzy cztery", "jeden dwa trzy cztery")  # 0/4
    assert wer.corpus_error_rates([short, long])[0] == pytest.approx(1 / 5)
    assert wer.corpus_error_rates([]) == (0.0, 0.0)


def test_numeric_mismatch() -> None:
    assert wer.numeric_mismatch("W 2024 roku", "W dwa tysiące dwudziestym czwartym roku")
    assert not wer.numeric_mismatch("Peron 3, 89 złotych", "peron 3 i 89 złotych")
    assert not wer.numeric_mismatch("bez liczb", "bez liczb")
