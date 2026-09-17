#!/usr/bin/env python3
"""Convert FLEURS pl_pl samples into the benchmark corpus layout (docs/13-benchmark.md §13.2).

    .venv/bin/python scripts/fleurs_to_corpus.py ~/stt-corpus-public [--split test] [--seed 0]

Selects one recording per distinct sentence and assigns groups by duration, like corpus A:
short 1-3 s, medium 4-10 s, long_utt 12-25 s, difficult = 5-10 s sentences containing digits.
Writes <group>/NNN.wav (16 kHz mono s16) + NNN.txt (raw transcription with case and punctuation)
and SOURCE.md with the CC-BY-4.0 attribution. Runs online, outside the daemon.
"""

import argparse
import csv
import io
import random
import struct
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.wav import float32_to_wav_bytes

REVISION = "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
BASE_URL = f"https://huggingface.co/datasets/google/fleurs/resolve/{REVISION}/data/pl_pl/"
SAMPLE_RATE = 16000
GROUP_COUNTS = {"short": 12, "medium": 16, "long_utt": 8, "difficult": 4}  # as in corpus A


@dataclass(frozen=True)
class Sample:
    sentence_id: str
    filename: str
    text: str
    duration_s: float


def parse_tsv(text: str) -> list[Sample]:
    # columns: id, file name, raw transcription, normalized, words, num_samples, gender
    return [
        Sample(row[0], row[1], row[2], int(row[5]) / SAMPLE_RATE)
        for row in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    ]


def group_of(sample: Sample) -> str | None:
    d = sample.duration_s
    if 5.0 <= d <= 10.0 and any(c.isdigit() for c in sample.text):
        return "difficult"
    if 1.0 <= d <= 3.0:
        return "short"
    if 4.0 <= d <= 10.0:
        return "medium"
    if 12.0 <= d <= 25.0:
        return "long_utt"
    return None


def select(samples: list[Sample], seed: int) -> list[tuple[str, Sample]]:
    """One random recording per sentence, then up to GROUP_COUNTS per group, numbered in order."""
    rng = random.Random(seed)
    by_sentence: dict[str, list[Sample]] = {}
    for s in samples:
        by_sentence.setdefault(s.sentence_id, []).append(s)
    pool: dict[str, list[Sample]] = {g: [] for g in GROUP_COUNTS}
    for sentence_id in sorted(by_sentence):
        chosen = rng.choice(by_sentence[sentence_id])
        group = group_of(chosen)
        if group is not None:
            pool[group].append(chosen)
    selected: list[tuple[str, Sample]] = []
    for group, count in GROUP_COUNTS.items():
        rng.shuffle(pool[group])
        selected += [(group, s) for s in pool[group][:count]]
    return selected


def float_wav_to_float32(data: bytes) -> NDArray[np.float32]:
    """Decode a mono 16 kHz IEEE-float WAV (FLEURS format, unsupported by the wave module)."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt, payload = 12, None, None
    while pos + 8 <= len(data):
        chunk_id, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk_id == b"data":
            payload = body
        pos += 8 + size + (size & 1)
    if fmt is None or payload is None:
        raise ValueError("missing fmt or data chunk")
    tag, channels, rate, _, _, bits = fmt
    if (tag, channels, rate, bits) != (3, 1, SAMPLE_RATE, 32):
        raise ValueError(f"expected float32 mono {SAMPLE_RATE} Hz, got {fmt}")
    return np.frombuffer(payload, dtype="<f4").astype(np.float32)


def _open(url: str) -> io.BufferedIOBase:
    request = urllib.request.Request(url, headers={"User-Agent": "local-stt-corpus"})
    response: io.BufferedIOBase = urllib.request.urlopen(request, timeout=60)
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--split", default="test", choices=["dev", "test", "train"])
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    with _open(BASE_URL + f"{args.split}.tsv") as f:
        selected = select(parse_tsv(f.read().decode("utf-8")), args.seed)
    wanted = {s.filename: (group, s) for group, s in selected}
    numbers = {s.filename: n for n, (_, s) in enumerate(selected, start=1)}
    print(f"selected {len(selected)} samples; streaming {args.split}.tar.gz ...", file=sys.stderr)

    written = 0
    with (
        _open(BASE_URL + f"audio/{args.split}.tar.gz") as f,
        tarfile.open(fileobj=f, mode="r|gz") as tar,
    ):
        for member in tar:
            name = Path(member.name).name
            if name not in wanted or not member.isfile():
                continue
            group, sample = wanted.pop(name)
            extracted = tar.extractfile(member)
            assert extracted is not None
            audio = float_wav_to_float32(extracted.read())
            target = args.out_dir / group / f"{numbers[name]:03d}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_suffix(".wav").write_bytes(float32_to_wav_bytes(audio, SAMPLE_RATE))
            target.with_suffix(".txt").write_text(sample.text + "\n", encoding="utf-8")
            written += 1
            if not wanted:
                break
    if wanted:
        print(f"error: {len(wanted)} selected files not found in the archive", file=sys.stderr)
        return 1

    counts = {g: sum(1 for group, _ in selected if group == g) for g in GROUP_COUNTS}
    (args.out_dir / "SOURCE.md").write_text(
        "# Public benchmark corpus — FLEURS part\n\n"
        f"- Source: FLEURS `pl_pl` `{args.split}` split, https://huggingface.co/datasets/google/fleurs"
        f" @ `{REVISION}`, selected with `scripts/fleurs_to_corpus.py --seed {args.seed}`.\n"
        "- License: CC-BY-4.0, © Google (Conneau et al., FLEURS, 2022). Converted from 32-bit"
        " float to 16 kHz mono 16-bit WAV; transcripts are the raw transcription column.\n"
        f"- Group counts: {counts}\n",
        encoding="utf-8",
    )
    print(f"wrote {written} samples to {args.out_dir}: {counts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
