# Test fixtures

| File | Source | License |
|---|---|---|
| `pl_short.wav` | FLEURS `pl_pl` **dev** split, `13656020374983536198.wav` ([google/fleurs](https://huggingface.co/datasets/google/fleurs) @ `70bb2e84`), converted from 32-bit float to 16 kHz mono s16. Transcript: “Warto poświęcić pół godziny na spacer po tej intrygującej wiosce.” | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/), © Google (Conneau et al., *FLEURS*, 2022) |
| `whisper_v1.9.4_verbose_json.json` | Real `POST /inference` response (`verbose_json`) of whisper.cpp `whisper-server` v1.9.4 with `ggml-base-q5_1.bin` for `pl_short.wav` | derived from the above |

The dev split is used on purpose: `scripts/fleurs_to_corpus.py` samples the **test** split for the benchmark (docs/13-benchmark.md §13.2), so this fixture never overlaps with benchmark data.
