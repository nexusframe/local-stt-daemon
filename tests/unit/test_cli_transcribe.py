"""`local-stt transcribe` and `bench --soak` choose the engine (task 4.5)."""

from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar

import pytest

from local_stt import cli
from local_stt.interfaces import Transcript
from local_stt.stt import parakeet
from local_stt.stt import whisper_server as ws

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pl_short.wav"


class FakeEngine:
    def __init__(self, label: str):
        self.label = label

    def transcribe(self, audio: Any, **kwargs: Any) -> Transcript:
        return Transcript(
            text=f" {self.label}", segments=[], audio_duration_s=1.0, processing_s=0.5,
            engine=self.label, model=self.label,
        )  # fmt: skip


class FakeTemporaryServer:
    started: ClassVar[list[tuple[str, Path, dict[str, Any]]]] = []

    def __init__(self, model_path: Path, **kwargs: Any):
        self.started.append((type(self).__name__, model_path, kwargs))

    def __enter__(self) -> FakeEngine:
        return FakeEngine(type(self).__name__)

    def __exit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        pass


class FakeParakeet(FakeTemporaryServer):
    pass


class FakeWhisper(FakeTemporaryServer):
    pass


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    FakeTemporaryServer.started = []
    monkeypatch.setattr(parakeet, "TemporaryParakeetServer", FakeParakeet)
    monkeypatch.setattr(ws, "TemporaryWhisperServer", FakeWhisper)
    path = tmp_path / "config.toml"
    path.write_text(f'[stt]\nmodels_dir = "{tmp_path}"\nthreads = 3\n')
    return path


def test_model_parakeet_starts_a_temporary_engine_server(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["transcribe", "--config", str(config), "--model", parakeet.PARAKEET_MODEL]
    assert cli.main([*args, str(FIXTURE)]) == 0
    assert FakeTemporaryServer.started == [
        ("FakeParakeet", tmp_path / parakeet.PARAKEET_MODEL,
         {"threads": 3, "startup_timeout_s": 60.0}),
    ]  # fmt: skip
    assert capsys.readouterr().out == "FakeParakeet\n"


def test_whisper_model_still_starts_whisper_server(config: Path, tmp_path: Path) -> None:
    assert cli.main(["transcribe", "--config", str(config), "--model", "small", str(FIXTURE)]) == 0
    assert [(n, p) for n, p, _ in FakeTemporaryServer.started] == [
        ("FakeWhisper", tmp_path / "ggml-small.bin")
    ]


def test_without_model_uses_the_selected_engine_service(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "xdg/local-stt/secret"
    secret.parent.mkdir(parents=True)
    secret.write_text("ab" * 16)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    used: list[str] = []

    def fake_transcribe(self: ws.WhisperServerEngine, audio: Any, **kwargs: Any) -> Transcript:
        used.append(type(self).__name__)
        return FakeEngine("service").transcribe(audio)

    monkeypatch.setattr(ws.WhisperServerEngine, "transcribe", fake_transcribe)
    monkeypatch.setattr(parakeet.ParakeetEngine, "transcribe", fake_transcribe)
    assert cli.main(["transcribe", "--config", str(config), str(FIXTURE)]) == 0
    config.write_text(config.read_text() + 'engine = "whisper-server"\n')
    assert cli.main(["transcribe", "--config", str(config), str(FIXTURE)]) == 0
    assert used == ["ParakeetEngine", "WhisperServerEngine"]


def test_soak_rejects_parakeet(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bench", "--soak", "--model", parakeet.PARAKEET_MODEL]) == 2
    assert "Whisper models only" in capsys.readouterr().err
