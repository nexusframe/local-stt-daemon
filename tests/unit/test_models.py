import hashlib
import subprocess
import sys
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from local_stt import models
from local_stt.cli import main


class _CountingHandler(SimpleHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Loopback HTTP server serving files from a temporary directory."""
    root = tmp_path / "served"
    root.mkdir()
    _CountingHandler.hits = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_CountingHandler, directory=str(root)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/", root
    finally:
        httpd.shutdown()
        httpd.server_close()


def _model(url: str, content: bytes, filename: str = "ggml-test.bin") -> models.Model:
    return models.Model("test", filename, url + filename, hashlib.sha256(content).hexdigest())


def test_registry_covers_all_spec_models() -> None:
    registry = models.load_registry()
    assert set(registry) == {
        "base-q5_1",
        "small-q5_1",
        "small-q8_0",
        "small",
        "medium-q5_0",
        "large-v3-turbo-q5_0",
        "silero-vad",
    }
    assert registry["small-q5_1"].filename == "ggml-small-q5_1.bin"
    assert registry["small-q5_1"].url == (
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin"
    )
    assert registry["silero-vad"].filename == "silero_vad.onnx"
    assert "/v6.2.1/" in registry["silero-vad"].url
    for model in registry.values():
        assert len(model.sha256) == 64
        int(model.sha256, 16)


@pytest.mark.parametrize("line", ["abc  file.bin", "a" * 64 + " file.bin", "a" * 64 + "  "])
def test_parse_checksums_rejects_malformed_lines(line: str) -> None:
    with pytest.raises(ValueError):
        models.parse_checksums(line)


def test_pull_downloads_and_verifies(server: tuple[str, Path], tmp_path: Path) -> None:
    url, root = server
    content = b"model bytes" * 1000
    (root / "ggml-test.bin").write_bytes(content)
    dest = tmp_path / "models"

    assert models.pull(_model(url, content), dest) is True
    assert (dest / "ggml-test.bin").read_bytes() == content
    assert list(dest.iterdir()) == [dest / "ggml-test.bin"]


def test_pull_checksum_mismatch_leaves_nothing(server: tuple[str, Path], tmp_path: Path) -> None:
    url, root = server
    (root / "ggml-test.bin").write_bytes(b"tampered")
    dest = tmp_path / "models"

    with pytest.raises(models.ModelError, match="checksum mismatch"):
        models.pull(_model(url, b"expected"), dest)
    assert list(dest.iterdir()) == []


def test_pull_http_error_leaves_nothing(server: tuple[str, Path], tmp_path: Path) -> None:
    url, _ = server
    dest = tmp_path / "models"

    with pytest.raises(models.ModelError, match="download failed"):
        models.pull(_model(url, b"x", filename="missing.bin"), dest)
    assert not any(dest.iterdir())


def test_pull_skips_valid_existing_file(server: tuple[str, Path], tmp_path: Path) -> None:
    url, _ = server
    content = b"already here"
    dest = tmp_path / "models"
    dest.mkdir()
    (dest / "ggml-test.bin").write_bytes(content)

    assert models.pull(_model(url, content), dest) is False
    assert _CountingHandler.hits == 0


def test_pull_replaces_corrupt_existing_file(server: tuple[str, Path], tmp_path: Path) -> None:
    url, root = server
    content = b"good model"
    (root / "ggml-test.bin").write_bytes(content)
    dest = tmp_path / "models"
    dest.mkdir()
    (dest / "ggml-test.bin").write_bytes(b"corrupt")

    assert models.pull(_model(url, content), dest) is True
    assert (dest / "ggml-test.bin").read_bytes() == content


def test_model_status(tmp_path: Path) -> None:
    model = _model("http://unused/", b"content")
    assert models.model_status(model, tmp_path) is models.ModelStatus.MISSING
    (tmp_path / model.filename).write_bytes(b"content")
    assert models.model_status(model, tmp_path) is models.ModelStatus.OK
    (tmp_path / model.filename).write_bytes(b"other")
    assert models.model_status(model, tmp_path) is models.ModelStatus.CORRUPT


def test_verify_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert models.cmd_verify(tmp_path) == 0  # missing models are not a failure
    (tmp_path / "silero_vad.onnx").write_bytes(b"not the real model")
    assert models.cmd_verify(tmp_path) == 1
    assert "silero-vad" in capsys.readouterr().out


def test_cli_pull_unknown_model_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "pull", "nonexistent"]) == 2
    assert "unknown model" in capsys.readouterr().err


def test_cli_does_not_load_network_code() -> None:
    # docs/12 §12.2: Internet networking code is loaded only by the `models` command.
    code = (
        "import sys, local_stt.cli; "
        "bad = {'local_stt.models', 'urllib.request'} & set(sys.modules); "
        "sys.exit(f'loaded: {bad}' if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_list_bench_shows_results_marks_and_unmeasured_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from local_stt.bench.report import ConfigStats, ModelResult

    (tmp_path / "ggml-small-q8_0.bin").write_bytes(b"x" * (1 << 20))
    q8 = ConfigStats("k", "small-q8_0", 4, 1000, -1, wer_runs=[0.074])
    q8.p90_text_ready_s, q8.peak_rss_mb = 3.67, 467.0
    turbo = ConfigStats("k", "large-v3-turbo-q5_0", 4, 0, -1, wer_runs=[0.079])
    results = {
        "small-q8_0": ModelResult(q8, "r", "2026-10-03", "A", True, True),
        "large-v3-turbo-q5_0": ModelResult(turbo, "r", "2026-09-17", "B", False, False),
    }
    assert (
        models.cmd_list_bench(
            tmp_path,
            results,
            current="small-q8_0",
            config_label="t=4 ctx=1000 greedy",
            bench_dir=tmp_path,
        )
        == 0
    )
    rows = {
        line.split()[1] if line.startswith("*") else line.split()[0]: line
        for line in capsys.readouterr().out.splitlines()[1:7]
    }
    assert rows["small-q8_0"].startswith("* small-q8_0")
    assert "1.0 MiB" in rows["small-q8_0"] and "7.4" in rows["small-q8_0"]
    assert "3.67" in rows["small-q8_0"] and "467" in rows["small-q8_0"]
    assert rows["small-q8_0"].endswith("t=4 ctx=1000 greedy    2026-10-03 A")
    assert rows["large-v3-turbo-q5_0"].endswith("t=4 ctx=full greedy !  2026-09-17 B stage 1")
    assert "missing" in rows["base-q5_1"] and "not measured" in rows["base-q5_1"]


def test_list_bench_without_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    models.cmd_list_bench(
        tmp_path, {}, current="small-q8_0", config_label="t=4", bench_dir=tmp_path / "bench"
    )
    assert "no benchmark results in" in capsys.readouterr().out
