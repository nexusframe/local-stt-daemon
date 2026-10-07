# v0.3 acceptance

Results of the v0.3 acceptance from [15](15-implementation-plan.md): the [14.4](14-tests.md) v0.3 checklist and the continuous-mode WER criterion. Each item records the date, how it was checked, and the result. Status: **checklist complete** (2026-10-07). All four items pass, two with a note: item 1's continuous-mode switch follows the spec but carries a usability risk (kept, user decision 2026-10-07), and item 3's criterion was corrected because the transcription is not fully deterministic. One defect was found and fixed on the way: `models list --bench` crashed on directories written by exploration scripts.

## Environment

- Reference machine: Ubuntu 24.04, GNOME on X11 (`DISPLAY=:1`), AC power, governor `powersave`, platform profile `performance`.
- Code: commit `627f325`. The installed copy was from `6b56d93` (`local-stt 0.0.1`, without `bench --context`), so it was reinstalled with `install.sh --no-apt` at 18:38 (`doctor` 0 FAIL, 17 OK), and again at ~18:43 with the `models list --bench` fix. With the fix: 787 unit tests and the E2E + integration suites (`e2e`, `needs_x11`, `needs_whisper`, `needs_audio`; 69 tests) pass.
- Config: the user's `config.toml` — defaults (`small-q8_0`, `threads = 4`, `audio_ctx = 1000`) except `stt.languages = ["pl", "en"]`, an explicit `language_toggle = "Ctrl+Control_R"`, and the user's own `hallucination_patterns`.
- Microphone: built-in only (`alsa_input.pci-0000_00_1f.3.analog-stereo`).

## 14.4 checklist (v0.3)

| # | Item | Result | Notes |
|---|---|---|---|
| 1 | `Ctrl+Control_R` switches the language in PTT and continuous mode; sound + notification, no sound while the microphone is open; a recorded job keeps its language; `local-stt language` shows, toggles, selects, rejects `de` (code 4) | pass, with a note | 2026-10-07 18:44–18:49, GNOME Text Editor, by the user: PTT pl → en → pl with sound and the “Language: EN” notification, English dictation came out in English. Continuous mode: switch during speech → no sound, notification shown. The segment being spoken (18:49:07–10, switch at 18:49:08) came out in English: per spec a continuous job takes the language active when its segment **arrives** ([04](04-state-machine.md) §4.6 “Language switch”), so this is correct. CLI checked by the assistant: `language` prints `en (languages: pl, en)`, `toggle` cycles, `en`/`pl` select (code 0), `de` → `rejected: language must be one of stt.languages: pl, en`, code 4. See [note](#note-a-switch-right-after-a-sentence-can-translate-it). |
| 2 | `local-stt models list --bench` lists the models with WER, p90 latency and RAM; `*` marks `stt.model` | pass (after fix) | First run 18:39 **FAIL**: `KeyError: 'stage'`, see [finding](#finding-models-list---bench-crashes-on-foreign-run-directories). After the fix: six models, `* small-q8_0` (WER 7.4 %, p90 3.67 s, 467 MB, corpus A, 2026-10-03), values unchanged from task 3.1. |
| 3 | `local-stt bench --context` on `long/001` reproduces the 3.4 results: word-error counts within ±1 word per policy, same conclusion | pass (criterion corrected) | 2026-10-07 18:51–19:05, daemon stopped, `--threads 4 --audio-ctx 1000 --context-chars 0,100,200,300 --context-reset 5`, results in `~/.local/share/local-stt/bench/2026-10-07T16-51-50Z/`. Word errors (WER / raw), 2026-10-06 → 2026-10-07: 0 chars 130/216 → 130/216; 100 chars 126/217 → 126/216; 200 chars 128/219 → 128/219; 300 chars 125/218 → 126/217. Same 59 segments; 4 segment texts differ, e.g. “Kurela” → “Kurlela”, “Rada, Rado i Polonu” → “rada, rado i polonu”. WER spread between policies stays < 1 point, so the 3.4 decision holds. The original item said “the text is deterministic”; it is not — probably the temperature fallback (`temperature_inc 0.2`, 06 §6.5) sampling on uncertain segments, **untested**. Item reworded in [14](14-tests.md). |
| 4 | v0.2 regression: 2 min continuous with a pause > 5 s and a sentence longer than `max_segment_s` → complete text, no journal errors; one PTT dictation inserted | pass | 2026-10-07 19:31:07–19:34:09, GNOME Text Editor, the user read a Polish text: 18 segments (`seq` 1…18 without gaps, 11 `silence`, 7 `max_length` — the long passage at 19:32:57–19:34:04 split six times), all `injected`; pauses of ~6 and ~8 s between segments 6/7 and 12/13 (from the timings). Then PTT job 19 (6.0 s, 98 chars) `injected`. No WARNING or ERROR in either unit's journal. The text was complete; at `max_length` joins Whisper wrote “...” and one word was cut (“rozw... ...wijać”) — the known join behaviour (task 3.3, rejected). |

## Plan criteria

| Criterion ([15](15-implementation-plan.md)) | Result | Notes |
|---|---|---|
| 14.4 (v0.3) checklist | pass | above |
| Continuous-mode WER on `long/` no worse than v0.2 | pass | `long/001` with the production policy (200 chars): 16.60 % (128/771) on 2026-10-07, equal to the v0.2-code baseline from task 3.3 (16.6 %, 128/771). |

## Finding: `models list --bench` crashes on foreign run directories

`report.latest_results` read every subdirectory of `~/.local/share/local-stt/bench/` that had a `results.jsonl`. The 2026-10-07 Parakeet exploration scripts wrote `parakeet-2026-10-07/` and `parakeet-2026-10-07-pad05/` there with their own line format (no `stage`), and `summarize` raised `KeyError: 'stage'`. Fix: `_run_dirs` also requires `system.json`, which `local-stt bench` always writes and other scripts do not; test `test_latest_results_skip_directories_not_written_by_bench`.

## Note: a switch right after a sentence can translate it

In continuous mode the job's language is fixed when its segment arrives, i.e. after the trailing silence. If the user finishes a Polish sentence and presses `Ctrl+Control_R` before the segment is cut, that sentence is sent with `en` and comes out translated into English (06 §6.5: the language is enforced on the output). The alternative — the language at the start of speech — was offered and declined; the behaviour stays as specified (user decision 2026-10-07).

## Notes

- Not checked: a language switch while PTT is held.
- Open from v0.2, unchanged: the incorrect-segmentation report; N3 only on AC `performance`.
