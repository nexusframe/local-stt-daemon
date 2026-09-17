#!/usr/bin/env python3
"""Continuous benchmark recording from a Wolne Lektury audiobook (docs/13-benchmark.md §13.2).

    .venv/bin/python scripts/wolnelektury_to_corpus.py ~/stt-corpus-public \\
        --slug borowski-kamienny-swiat-lato-w-miasteczku --start 39.9 --end 335.28

Downloads the book's single MP3 and plain text, decodes with GStreamer (gst-launch-1.0) to
16 kHz mono, keeps [--start, --end] seconds (cut points chosen in digital silence to drop the
spoken Wolne Lektury intro and outro), and writes long/NNN.wav + NNN.txt. The reference text is
the book text without the author line and the license footer: the kept audio starts with the
title. Runs online, outside the daemon.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from local_stt.audio.wav import float32_to_wav_bytes, wav_bytes_to_float32

API = "https://wolnelektury.pl/api/books/{slug}/"
SAMPLE_RATE = 16000


def reference_text(book_txt: str) -> str:
    """Book text without the leading author line and the footer after the '-----' separator."""
    text = book_txt.replace("\r\n", "\n")  # Wolne Lektury TXT files use CRLF
    body, sep, _ = text.partition("\n-----\n")
    if not sep:
        raise ValueError("license footer separator '-----' not found")
    lines = body.strip().splitlines()[1:]  # first line: author, read in the intro that is cut
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in "\n".join(lines).split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p) + "\n"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "local-stt-corpus"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data: bytes = response.read()
        return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--start", type=float, required=True, help="seconds")
    parser.add_argument("--end", type=float, required=True, help="seconds")
    args = parser.parse_args()

    book = json.loads(_get(API.format(slug=args.slug)))
    mp3 = [m for m in book["media"] if m["type"] == "mp3"]
    if len(mp3) != 1 or not book.get("txt"):
        print("error: expected exactly one MP3 and a TXT for this book", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        src, wav = Path(tmp) / "book.mp3", Path(tmp) / "book.wav"
        src.write_bytes(_get(mp3[0]["url"]))
        subprocess.run(
            ["gst-launch-1.0", "-q", "filesrc", f"location={src}", "!", "decodebin", "!",
             "audioconvert", "!", "audioresample", "!",
             f"audio/x-raw,format=S16LE,rate={SAMPLE_RATE},channels=1", "!", "wavenc", "!",
             "filesink", f"location={wav}"],
            check=True,
        )  # fmt: skip
        audio, rate = wav_bytes_to_float32(wav.read_bytes())
    audio = audio[int(args.start * rate) : int(args.end * rate)]

    long_dir = args.out_dir / "long"
    long_dir.mkdir(parents=True, exist_ok=True)
    number = 1
    while (long_dir / f"{number:03d}.wav").exists():
        number += 1
    target = long_dir / f"{number:03d}"
    target.with_suffix(".wav").write_bytes(float32_to_wav_bytes(audio, rate))
    target.with_suffix(".txt").write_text(
        reference_text(_get(book["txt"]).decode("utf-8")), encoding="utf-8"
    )
    authors = ", ".join(a["name"] for a in book.get("authors", []))
    with (long_dir / "SOURCE.md").open("a", encoding="utf-8") as f:
        f.write(
            f"- `{target.name}.wav`: {authors}, “{book['title']}”, audiobook from Wolne Lektury "
            f"(https://wolnelektury.pl/katalog/lektura/{args.slug}/), read by {mp3[0]['artist']}, "
            f"recording directed by {mp3[0]['director']}; kept {args.start}-{args.end} s of the "
            f"MP3 ({len(audio) / rate:.1f} s). Text: public domain; audiobook under the Wolne "
            "Lektury free license — attribution per https://wolnelektury.pl/info/zasady-wykorzystania/\n"
        )
    print(f"wrote {target}.wav ({len(audio) / rate:.1f} s) and {target}.txt", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
