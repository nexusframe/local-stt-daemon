# 08. Text processing and injection into a window

```text
Transcript ──► TextProcessor ──► str | None ──► Injector ──► InjectResult
```

`TextProcessor` knows nothing about X11. `Injector` knows nothing about Whisper.

## 8.1 Processing context

```python
@dataclass(frozen=True)
class TextContext:
    source: Literal["ptt", "continuous"]
    session_id: int | None
    seq: int | None
    cut: Literal["release", "max_duration", "silence", "max_length", "flush"]
    prev_cut: str | None          # cut of the previous segment from this session (continuous)
    prompt_tail: str | None       # session context passed in the prompt (continuous only; excludes vocabulary_prompt); None in PTT
```

## 8.2 `TextProcessor` — steps (in this order)

1. **Segment filter** ([06](06-stt-engine.md) §6.8): `no_speech_prob` together with `avg_logprob`, the hallucination list, repetition loops, and prompt echo.
2. **Assembly**: adjacent retained segments are joined with `"".join(s.text for s in run)`, without applying `strip()` to individual segments and without adding spaces. A Whisper segment boundary can occur inside a word: `" trans"` + `"krypcja"` must produce `" transkrypcja"`. If the filter removed a segment between two retained runs, insert one separator in its place so that words on either side of the removed content are not joined. The engine adapter preserves whitespace in `TranscriptSegment.text` (06 §6.8).
3. **Whitespace normalization**: sequences of spaces and tabs → one space, `strip()`. Replace Whisper line breaks with spaces.
4. **User replacements** from `text.replacements`: a list of `{pattern, replace, regex}` applied in order, e.g. `{pattern = "(?i)\\bnowa linia\\b", replace = "\n", regex = true}`. This is the only “command” mechanism in v0.1–v0.3.
5. **Continuous-mode continuity**:
   - if `cut in ("max_length", "max_duration")` (the segment ends in the middle of an utterance) and the text ends with a single period, remove that period (`?`, `!`, and `…` remain),
   - if `prev_cut == "max_length"`, the first letter is uppercase, and the second word is not capitalized (the “not a proper name” heuristic), lowercase the first letter.
6. **Separator**: when `text.append_space = true` (the default), append a **trailing space** to every non-empty result. Subsequent segments and dictations then join naturally without tracking window state.
7. Empty result → `None`; the pipeline skips injection.

Each step is a pure function with unit tests ([14](14-tests.md)).

## 8.3 Injector interface

```python
class Injector(Protocol):
    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult: ...

@dataclass(frozen=True)
class InjectResult:
    ok: bool
    backend: str                     # "clipboard" | "type"
    chars: int
    window_class: str | None
    left_in_clipboard: bool          # text intentionally left in the clipboard
    error: str | None
    cancelled: bool = False          # cancellation; no emergency clipboard fallback
    no_target: bool = False          # no active window (step 2 in 8.5); selects the notification
```

Implementations (v0.1): `ClipboardPasteInjector`, `XdotoolTypeInjector`, and `AutoInjector` (selected per window). Tests use `RecordingInjector`.

`CancellationToken` is shared by a job generation. It allows waits to be interrupted and validity to be checked atomically while marking the start of an injection operation under the same short lock used by `pipeline.cancel_all()`. An XTest sequence and each `type` chunk are separate operations; after each completes, the injector clears the in-progress marker. The lock does not cover waiting for the clipboard or subprocess execution.

If cancellation stops a job before all text is entered, the injector returns `cancelled=True`, and the pipeline reports `JobDiscarded(cancelled)` instead of an error or retry. An operation started before cancellation may finish (including releasing synthetic modifiers and handling the clipboard); text in another application is not undone. The `cancel` response then reports `injection_in_flight=true`. No subsequent operation from the old generation may start. If text was partially entered, the result contains the number of characters sent; the job is not counted as fully injected. If one started operation manages to inject the entire result, return a normal `InjectResult` and `JobFinished`, even if the remaining jobs were cancelled in the meantime. The in-progress flag remains set until all paste handling, including confirmation and clipboard restoration, is complete.

## 8.4 Method selection — why clipboard + paste is the default

| Criterion | `xdotool type` | clipboard + Ctrl+V (custom selection owner) |
|---|---|---|
| Polish characters | keymap-dependent. Ubuntu 24.04 has xdotool **3.20160805** (without the 2025–2026 fixes); known bugs with multiple layouts (#150, #354) and `MappingNotify` races when remapping keycodes → missing/incorrect characters in Chrome/Electron | keyboard-layout-independent, Unicode 1:1 |
| Speed | ~12 ms/character → 300 characters ≈ 4 s; the user cannot type during this time | one paste; the full operation includes saving the clipboard, confirmation, and an additional 150 ms before restoration; duration is measured |
| Atomicity | injection can be interrupted by a focus change | atomic |
| Terminals | works | requires `Ctrl+Shift+V` (selected by `WM_CLASS`) |
| Side effects | none | overwrites CLIPBOARD → save and restore all targets ≤ 256 KiB (larger content → `type` fallback); clipboard history managers will see the text |
| Applications that ignore XTest | does not work | does not work (the same XTest is used for Ctrl+V) |

Decision: `injection.backend = "auto"`:

- **clipboard** by default,
- **type** for windows in `injection.type_window_classes` (default: `["xterm", "URxvt"]`, because xterm does not paste CLIPBOARD with Ctrl+Shift+V),
- **type** also when the current clipboard contents **cannot be saved and restored faithfully** (8.5, step 3), to avoid destroying them.

## 8.5 `ClipboardPasteInjector` — algorithm

Components:

- `ClipboardOwner` — a thread with **its own X11 connection** and an invisible 1×1 window. It handles `SelectionRequest`, `SelectionClear`, and `SelectionNotify`, and stores both the content currently being served (`served: dict[target, (type, format, bytes)]`) and what was saved from the user (`user_saved`).
- `KeySender` — XTest (`Xlib.ext.xtest.fake_input`) on the worker thread's connection.

Steps in `inject(text, cancel=token)`:

1. **Wait for modifiers to be released.** Every 20 ms, call `query_keymap()` and check the keycode bits from `get_modifier_mapping()`, for at most `injection.modifier_wait_ms` (1000). On timeout, log WARNING `modifiers still held` and continue. Do not use `--clearmodifiers` or synthetic releases, because they leave modifiers “stuck” if the user releases a key during the operation. **Exception:** when the PTT key is held (a new recording is in progress and the active grab would intercept injected keys), wait until release or cancellation. Every wait before injection begins, including waits for clipboard responses, checks the token at least every 20 ms. After sending the shortcut, complete the confirmation and restoration protocol even if the token is cancelled. Cancellation before injection begins ends the method without sending keys.
2. **Target window.** Read `_NET_ACTIVE_WINDOW` from the root window, then `WM_CLASS` (instance, class).
   - Value `0` or a desktop window → **no target**: after atomically checking the token and starting the operation, place the text in the clipboard without restoring it, set `left_in_clipboard = true` and `no_target = true`, and show “No active field — text is in the clipboard”. End.
3. **Save the clipboard.** To roll back a cancelled takeover, also remember the content immediately preceding this operation (`rollback_saved`); if we already own it, this is a copy of `served`, which may differ from historical `user_saved`.
   - **We are the owner** (serving restored user content or text from a failed paste) → `saved = user_saved` (what the user had *before* our first intervention; text from a failed paste does not overwrite `user_saved`).
   - **Another client is the owner** → `ConvertSelection(CLIPBOARD, TARGETS)` (300 ms timeout), then retrieve **every** non-meta target (`TARGETS`, `TIMESTAMP`, `MULTIPLE`, `SAVE_TARGETS`, `DELETE`). For each target, save its bytes, property type, and format (8/16/32). Limits: at most 32 targets, each ≤ 256 KiB, total ≤ 1 MiB, entire operation ≤ 500 ms.
     - Everything within limits → `saved = {target: (type, format, bytes)}`, `user_saved = saved`. Restore bytes 1:1, so their format need not be understood. Thus a Firefox/Chrome copy (text, `text/html`, and browser metadata) or Nautilus files (`x-special/gnome-copied-files`) are fully restored.
     - Limit exceeded or the owner responds via INCR (typically images or large documents) → **switch to `XdotoolTypeInjector`** (the clipboard cannot be faithfully restored, so leave it untouched).
     - No response or no owner → `saved = None`.
4. **Take ownership of the clipboard.** Check the token again, call `set_selection_owner(CLIPBOARD, our_window, time)`, and verify that `get_selection_owner` equals our window. Serve `TARGETS`, `UTF8_STRING`, `text/plain;charset=utf-8`, `TEXT`, and `STRING` (`STRING` only if the text fits Latin-1). Text limit: 64 KiB without INCR support; longer text uses the `type` backend (not expected in practice). If cancellation arrives after takeover but before sending the shortcut, restore `rollback_saved` regardless of `restore_clipboard`, provided we still own the clipboard, and finish with `cancelled=True`. Do not overwrite a clipboard taken over by the user in the meantime.
5. **Paste shortcut**, selected by `WM_CLASS` (case-insensitive comparison):
   - `injection.paste_shortcut_overrides` (class → shortcut map),
   - classes in `injection.terminal_window_classes` → `Ctrl+Shift+V`,
   - all others → `Ctrl+V`.
6. **Send the shortcut through XTest.** Immediately before the first press, atomically check the token and mark the operation as started (8.3). A cancelled token blocks the entire sequence. Always use the **`Control_L` and `Shift_L`** keycodes, never `Control_R`/`Shift_R`. The validator ([09](09-configuration.md) §9.3) rejects daemon shortcuts containing `Control_L` or `Shift_L` as the keysym, so an injected key cannot trigger our own PTT grab. Sequence: press modifiers → press/release `v` (keycode from `keysym_to_keycode(XK_v)`) → release modifiers, with `sync()` and an 8 ms pause after each step. Record `t_sent` (monotonic time immediately before the first press).
7. **Confirmation.** `ClipboardOwner` counts only text-target requests (not `TARGETS`) that meet both conditions:
   - they arrived **after `t_sent`** (rejecting clipboard managers that fetch content through XFixes immediately after the owner changes),
   - the `requestor` window belongs **to the same X client as the active window**: `requestor & ~resource_id_mask == active_window & ~resource_id_mask`, where the mask comes from `display.info.resource_id_mask`. X resource IDs contain the client base, so the XRes extension is unnecessary.

   Wait at most `injection.paste_timeout_ms` (1000):
   - confirmed → after an additional 150 ms (applications sometimes fetch several targets), proceed to restoration,
   - no confirmation → the application did not paste (focus is not in a text field or the window ignores XTest). **Do not restore the clipboard**: leave the text there, set `left_in_clipboard = true`, log a WARNING, and show “Could not paste — text is in the clipboard (Ctrl+V)”. Dictation is never silently lost. Preserve `user_saved` for the next successful paste.
8. **Restoration** (`injection.restore_clipboard = true`), only if we still own CLIPBOARD; after `SelectionClear`, do not reclaim it:
   - non-empty `saved` → remain the owner but now serve `saved` (all saved targets) until another application takes over the clipboard. Then `SelectionClear` → clear `served` and `user_saved`,
   - `saved is None` → `set_selection_owner(CLIPBOARD, X.NONE)`.
   - After the daemon exits, restored content disappears if its original owner no longer serves it. `xclip` behaves the same way. This is accepted.

PRIMARY (middle-button selection) is **not modified**.

## 8.6 `XdotoolTypeInjector`

```bash
xdotool type --delay 12 -- "<text>"
```

- Run via `subprocess.run([...], timeout=max(5, len(text)*0.05))`. Pass the text as an argument (not through the shell), so escaping is not an issue.
- First perform the same step 1 (wait for modifiers to be released). **No `--clearmodifiers`.**
- Split text into chunks of 200 characters. Immediately before each subprocess invocation, atomically check the token and mark the operation as started (8.3). Cancellation blocks subsequent chunks; the current one may finish. Cancellation does not trigger the clipboard fallback, even after partial text entry.
- `\n` → xdotool sends `Return`.
- `injection.type_delay_ms` (12) is configurable. Values < 8 ms cause Chrome to lose characters.

## 8.7 Edge cases

| Situation | Behavior |
|---|---|
| User changes windows between recording and injection | text goes to the window active at injection time (deliberate simplicity; DEBUG log with both window classes) |
| Password field / screen-lock window | not detected; the GNOME lock screen has its own grab, so XTest does not reach it |
| User types during continuous mode | the paste is atomic, so it may be inserted between the user's characters; accepted and documented |
| `xdotool` absent | `type` backend unavailable → `doctor` WARN; `auto` then uses only the clipboard, and when the clipboard cannot be saved (step 3), it pastes without restoration and logs a WARNING |
| X11 error in the injector | `InjectResult(ok=False)`, text in the clipboard (if possible), notification |

## 8.8 Wayland (future)

On Wayland, both implementations stop working. New backends (`wl-copy` + `ydotool`/`dotool`, RemoteDesktop portal) implement the same `Injector`. The `auto` backend selection then depends on `XDG_SESSION_TYPE`. This is outside the scope of v0.1–v0.3.
