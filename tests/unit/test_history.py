from local_stt.history import TranscriptHistory


def test_newest_text_is_number_one() -> None:
    history = TranscriptHistory(10)
    history.add("pierwszy ")
    history.add("drugi ")
    assert history.get(1) == "drugi "
    assert history.get(2) == "pierwszy "


def test_oldest_text_drops_out_and_out_of_range_is_none() -> None:
    history = TranscriptHistory(2)
    for text in ("a ", "b ", "c "):
        history.add(text)
    assert history.items() == ["c ", "b "]  # newest first
    assert history.get(3) is None
    assert history.get(0) is None


def test_resize_keeps_the_newest_and_zero_disables() -> None:
    history = TranscriptHistory(3)
    for text in ("a ", "b ", "c "):
        history.add(text)
    history.resize(2)
    assert history.items() == ["c ", "b "]
    history.resize(0)
    history.add("d ")
    assert history.items() == []
