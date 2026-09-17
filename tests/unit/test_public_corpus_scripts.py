import importlib.util
import struct
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

SCRIPTS = Path(__file__).parent.parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleurs = _load("fleurs_to_corpus")
wolnelektury = _load("wolnelektury_to_corpus")


def _row(sid: str, fname: str, text: str, seconds: float) -> str:
    return "\t".join(
        [sid, fname, text, text.lower(), "w o r d s", str(int(seconds * 16000)), "MALE"]
    )


@pytest.mark.parametrize(
    ("text", "seconds", "group"),
    [
        ("Krótkie zdanie.", 2.0, "short"),
        ("Średnie zdanie bez liczb.", 6.0, "medium"),
        ("W 2024 roku było 5 osób.", 6.0, "difficult"),
        ("W 2024 roku było 5 osób.", 4.5, "medium"),  # digits but outside 5-10 s
        ("Długie zdanie.", 15.0, "long_utt"),
        ("Pomiędzy grupami.", 11.0, None),
    ],
)
def test_group_of(text: str, seconds: float, group: str | None) -> None:
    assert fleurs.group_of(fleurs.Sample("1", "a.wav", text, seconds)) == group


def test_select_one_recording_per_sentence_and_caps_counts() -> None:
    rows = [_row("s1", f"s1_{i}.wav", "To samo zdanie.", 6.0) for i in range(5)]
    rows += [_row(f"m{i}", f"m{i}.wav", "Średnie zdanie.", 6.0) for i in range(30)]
    samples = fleurs.parse_tsv("\n".join(rows) + "\n")

    selected = fleurs.select(samples, seed=0)
    assert [g for g, _ in selected] == ["medium"] * 16
    assert len({s.sentence_id for _, s in selected}) == 16
    assert fleurs.select(samples, seed=0) == selected  # deterministic


def test_parse_tsv_keeps_quotes_in_text() -> None:
    samples = fleurs.parse_tsv(_row("1", "a.wav", 'Powiedział "tak".', 4.2) + "\n")
    assert samples[0].text == 'Powiedział "tak".' and samples[0].duration_s == 4.2


def _float_wav(samples: np.ndarray, rate: int = 16000, tag: int = 3) -> bytes:
    data = samples.astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", tag, 1, rate, rate * 4, 4, 32)
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_float_wav_decoding() -> None:
    x = np.array([0.0, 0.5, -0.25], dtype=np.float32)
    np.testing.assert_array_equal(fleurs.float_wav_to_float32(_float_wav(x)), x)
    with pytest.raises(ValueError, match="expected float32"):
        fleurs.float_wav_to_float32(_float_wav(x, rate=44100))


def test_reference_text_strips_author_footer_and_crlf() -> None:
    book = (
        "Tadeusz Borowski\r\n\r\nLato w miasteczku\r\n\r\n\r\n\r\nWojtkowi Żukrowskiemu\r\n\r\n"
        "Pierwszy   akapit.\r\n\r\nDrugi akapit.\r\n\r\n\r\n"
        "-----\r\nTa lektura jest w domenie publicznej.\r\n"
    )
    assert wolnelektury.reference_text(book) == (
        "Lato w miasteczku\n\nWojtkowi Żukrowskiemu\n\nPierwszy akapit.\n\nDrugi akapit.\n"
    )
    with pytest.raises(ValueError, match="separator"):
        wolnelektury.reference_text("Autor\n\nTytuł\n\nTekst bez stopki.\n")
