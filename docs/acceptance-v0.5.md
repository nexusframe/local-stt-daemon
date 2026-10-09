# v0.5 acceptance

Results of the v0.5 acceptance from [15](15-implementation-plan.md): the [14.4](14-tests.md) v0.5 checklist. v0.5 has no measured criteria. Each item records the date, how it was checked, and the result. Status: **checklist complete** (2026-10-10). All six items pass. One item has a note. Two findings were decided (see [Findings](#findings)).

## Environment

- Reference machine: Ubuntu 24.04, GNOME on X11 (`DISPLAY=:1`), AC power, governor `powersave`.
- Code: commit `934b50e`, installed with `install.sh --no-apt` on 2026-10-09 at 22:58. The fix of finding 1 came after the live tests.
- Config: the user's `config.toml`. It has the defaults (`engine = "parakeet"`, `backend = "auto"`, `history.size = 10`), except `text.commands = true`, `stt.languages = ["pl", "en"]`, an explicit `language_toggle = "Ctrl+Control_R"`, and the user's own `hallucination_patterns`. For item 4, `backend` was set to `"clipboard-only"` and then restored. The restored file is identical to its backup.
- Microphone: built-in only.
- The user dictated. The results come from the user's report and from the journal (INFO level).

## 14.4 checklist (v0.5)

| # | Item | Result | Notes |
|---|---|---|---|
| 1 | `install.sh --no-apt` from the release commit → `doctor` reports no FAIL; ruff, mypy and the full test suite pass | pass | 2026-10-09 22:58. `doctor`: 0 FAIL, 0 WARN, 17 OK. ruff check and format are clean, mypy is clean (43 files), pytest: 1016 passed in 2 min 16 s. |
| 2 | `text.commands = true`: one PTT dictation with the five commands in GNOME Text Editor; “nowa linia” in GNOME Terminal runs no command | pass | 2026-10-09 23:44, job 1 (15.2 s of audio, `total` 1.70 s). The history shows `Lista kupów: chleb; mleko; masło⏎termin – jutro...`. All five commands gave their signs. “kupów” for “zakupów” is a recognition error, not a command error. In GNOME Terminal, the cursor moved to a new line and no command ran (user report). |
| 3 | `history` newest first; `last 2`; `busy`, `no_history`, usage error; empty after a restart | pass | 2026-10-09 23:45–23:47, three PTT sentences. `local-stt history` listed them newest first. `sleep 3; local-stt last 2` inserted the second newest text into the editor (job 6, “inserted again from the history”). Then by the assistant: `last` during continuous mode → “cannot insert a history text while recording”, code 4; `last 99` → “no history text number 99”, code 4; `last 0` → usage error, code 2; after `systemctl --user restart local-stt` → “history is empty”. See [finding 1](#1-history-showed-a-line-break-only-text-as-an-empty-line). |
| 4 | `clipboard-only`: PTT sends no keys, one notification, `Ctrl+V` pastes; a continuous session of ≥ 3 segments → one notification, one `Ctrl+V` pastes all | pass, with a note | 2026-10-09 23:49–23:51. PTT jobs 1–2: `result=clipboard`, inject 2–3 ms. In the first continuous try, the three sentences came as one segment (6.55 s), because the pauses were shorter than `min_silence_ms`. In the second try (session 4) the pauses were about 3 s: three segments (`seq` 1–3, `cut=silence`, inject 2–5 ms), one notification, and one `Ctrl+V` pasted all three sentences (user report). The assistant could not read the clipboard, because `xclip` and `xsel` are not installed. |
| 5 | Regression with `backend = "auto"`: PTT in Firefox and VS Code, 1 minute of continuous dictation | pass | 2026-10-09 23:52–23:53, PTT jobs 7–8: `total` 0.87 s and 0.89 s, `injected`. 2026-10-10 00:06–00:07, session 14 in GNOME Text Editor, the test paragraph (7 sentences): 5 segments (2 `max_length`, 3 `silence`), 48 s of audio, 587 characters, `total` 0.92–1.52 s. The user reports that no sentence is missing or repeated. See [observation 2](#2-one-paste-waited-175-s). |
| 6 | The journal contains no dictated text | pass | `journalctl --user -u local-stt -u local-stt-engine` since the install: 0 matches for words of the dictated texts. |

## Findings

### 1. `history` showed a line-break-only text as an empty line

When the user said only “nowa linia”, the history entry was `"\n"`. `local-stt history` showed it as `1  ` (an empty text). Cause: `_format_history` in `cli.py` called `strip()` before it replaced `\n` with `⏎`. 10 §10.1 already says that a line break shows as `⏎`. **Fixed** (user decision 2026-10-10): the replacement comes first, then `strip()`. New test: `test_history_shows_edge_line_breaks`.

### 2. One paste waited 17.5 s

In continuous session 9 (2026-10-09 23:58), job 15 had `inject=17500ms`. The next four segments waited in the queue (`queued` up to 15.3 s), and then they were inserted in the correct order. The journal has no WARNING. 08 §8.5 step 1 says that the injector waits without a limit while the PTT key is held. The probable cause is that the user held the right Ctrl key during the session. This is not verified, because the INFO log does not record the wait. Job 4 of item 3 (inject 1585 ms) is possibly the same case. **No change** (user decision 2026-10-10). The behavior agrees with the spec.
