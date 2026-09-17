from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.bench import corpus


class FakeRecorder:
    def __init__(self, durations: list[float]):
        self._durations: Iterator[float] = iter(durations)
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> NDArray[np.float32]:
        return np.full(int(next(self._durations) * 16000), 0.1, dtype=np.float32)


def _session(
    tmp_path: Path, answers: list[str], durations: list[float]
) -> tuple[corpus.CorpusSession, list[str], FakeRecorder]:
    replies = iter(answers)
    output: list[str] = []

    def ask(message: str) -> str:
        output.append(message)
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    recorder = FakeRecorder(durations)
    return corpus.CorpusSession(tmp_path, recorder, ask=ask, say=output.append), output, recorder


PROMPTS = [
    corpus.Prompt(1, "short", "Otwórz nowy dokument."),
    corpus.Prompt(2, "medium", "Rano pojechałem rowerem nad jezioro."),
    corpus.Prompt(3, "difficult", "Źdźbło trawy łaskotało Grześka."),
]


def test_packaged_prompts_match_spec_groups() -> None:
    prompts = corpus.load_prompts()
    counts = {g: sum(p.group == g for p in prompts) for g in corpus.GROUPS}
    assert counts == {"short": 12, "medium": 16, "long_utt": 8, "difficult": 4}
    assert [p.number for p in prompts] == list(range(1, 41))
    assert len({p.text for p in prompts}) == 40


def test_packaged_long_text() -> None:
    text = corpus.load_long_text()
    assert 650 <= len(text.split()) <= 900  # ~5 min of reading
    assert "(" not in text


@pytest.mark.parametrize(
    ("text", "match"),
    [("zdanie bez grupy", "before the first"), ("[nope]\nzdanie", "unknown group")],
)
def test_parse_prompts_errors(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        corpus.parse_prompts(text)


def test_records_into_group_directories(tmp_path: Path) -> None:
    # start, stop, save for each of the three prompts
    session, _, _ = _session(tmp_path, ["", "", ""] * 3, [2.0, 5.0, 6.0])
    assert session.record_prompts(PROMPTS) == 3

    audio, rate = wav_bytes_to_float32((tmp_path / "medium" / "002.wav").read_bytes())
    assert rate == 16000 and len(audio) == 5 * 16000
    assert (tmp_path / "difficult" / "003.txt").read_text(encoding="utf-8") == (
        "Źdźbło trawy łaskotało Grześka.\n"
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["difficult", "medium", "short"]


def test_skip_repeat_and_quit(tmp_path: Path) -> None:
    answers = [
        "s",  # prompt 1: skip before recording
        "",
        "",
        "r",  # prompt 2: record, repeat
        "",
        "",
        "",  # prompt 2: record again, save
        "q",  # prompt 3: quit
    ]
    session, _, recorder = _session(tmp_path, answers, [1.0, 7.0])
    assert session.record_prompts(PROMPTS) == 1
    assert recorder.started == 2
    assert not (tmp_path / "short").exists()
    _, rate = wav_bytes_to_float32((tmp_path / "medium" / "002.wav").read_bytes())
    assert len(wav_bytes_to_float32((tmp_path / "medium" / "002.wav").read_bytes())[0]) == 7 * rate
    assert not (tmp_path / "difficult").exists()


def test_resume_skips_existing_recordings(tmp_path: Path) -> None:
    first, _, _ = _session(tmp_path, ["", "", ""], [2.0])
    first.record_prompts(PROMPTS)  # saves 001, then input ends (EOF = quit)

    second, output, _ = _session(tmp_path, ["", "", ""], [5.0])
    assert second.record_prompts(PROMPTS) == 1
    assert not any("001/003" in line for line in output)
    assert (tmp_path / "medium" / "002.wav").is_file()


def test_duration_outside_group_range_is_reported(tmp_path: Path) -> None:
    session, output, _ = _session(tmp_path, ["", "", ""], [9.0])
    session.record_prompts(PROMPTS[:1])
    assert any("9.0 s (expected 1-3 s)" in line for line in output)


def test_eof_stops_recording_without_saving_partial_take(tmp_path: Path) -> None:
    session, _, recorder = _session(tmp_path, [""], [3.0])  # EOF while recording
    assert session.record_prompts(PROMPTS) == 0
    assert recorder.started == 1
    assert not any(tmp_path.iterdir())


def test_record_long_uses_next_free_number(tmp_path: Path) -> None:
    (tmp_path / "long").mkdir()
    (tmp_path / "long" / "001.wav").write_bytes(b"existing")
    session, _, _ = _session(tmp_path, ["", "", ""], [300.0])
    assert session.record_long("Długi tekst.") is True
    assert (tmp_path / "long" / "002.txt").read_text(encoding="utf-8") == "Długi tekst.\n"
    assert len(wav_bytes_to_float32((tmp_path / "long" / "002.wav").read_bytes())[0]) == 300 * 16000
