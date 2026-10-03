# 07. Global hotkeys (X11)

## 7.1 Default bindings

| Action | Default key | Type |
|---|---|---|
| Push-to-talk | **right Ctrl** (`Control_R`), held down | press + release |
| Continuous start/stop | **Shift + right Ctrl** (`Shift+Control_R`) | toggle on press |
| Cancel PTT recording | **Esc** (`hotkeys.ptt_cancel_key`) pressed *while holding* right Ctrl | press |
| Cancel continuous | `local-stt cancel` (optionally: a custom GNOME shortcut; see 7.7) | IPC |

### Why not `Super+Space` from the preliminary design

1. **Conflict.** On Ubuntu 24.04, `org.gnome.desktop.wm.keybindings switch-input-source = ['<Super>space', 'XF86Keyboard']`, while `switch-input-source-backward = ['<Shift><Super>space', ...]`. Both shortcuts from the design are already taken (verified on the reference machine).
2. **Overlay key.** Mutter grabs `Super_L` and opens Activities if no other key was pressed before it was released. Custom grabs for `Super+X` interact poorly with this logic (a known Ulauncher and Albert issue on GNOME X11).
3. **PTT ergonomics.** Holding two keys for a dozen or more seconds of speech is uncomfortable. A single large key under the thumb or little finger is better.

### Why right Ctrl

- Mutter does not use it: `locate-pointer-key = 'Control_L'` refers only to left Ctrl, and the feature itself is disabled.
- **Right Alt cannot be used.** It is AltGr (`ISO_Level3_Shift`, `mod5`), without which characters such as ą, ę, and ł cannot be typed.
- In practice, right Ctrl is rarely used as a modifier. The cost is that, while the daemon is running, `right Ctrl + key` combinations do not reach applications because the grab intercepts them (left Ctrl works normally).
- Modifier keys do not auto-repeat, so PTT does not generate false release/press pairs.
- **Empirically verified** on the reference machine (python-xlib 0.33, events injected through XTest):
  - grabs for `Control_R` and `Shift+Control_R`, including NumLock/CapsLock variants, are accepted without `BadAccess`,
  - `KeyPress` and `KeyRelease` reach the daemon,
  - with `Shift+Control_R`, releasing Shift *before* Ctrl also reaches the daemon (active grab), and `KeyRelease Control_R` arrives correctly.

The keys are configurable ([09](09-configuration.md)). On keyboards with `Menu`/`Pause`/`Insert`/`Scroll_Lock`, a good alternative is a single unused key.

## 7.2 Configuration syntax

```text
hotkey  := (modifier "+")* keysym
modifier:= "Ctrl" | "Shift" | "Alt" | "Super"
keysym  := X11 keysym name, e.g. Control_R, Pause, F9, space, Menu
```

`"Control_R"` means the key without additional modifiers. `"Shift+Control_R"` means the key with Shift held down. Modifier names map to the `ShiftMask`, `ControlMask`, `Mod1Mask`, and `Mod4Mask` masks. Immediately before grabbing, we check the mapping with `get_modifier_mapping()`. *Implementation (task 1.9):* the mask of each name is taken from the row that contains its keys (`Alt_L`/`Alt_R`/`Meta_L`, `Super_L`/`Super_R`; on Xvfb and the reference machine: `mod1`, `mod4`); a name with no row makes that shortcut a problem (`modifier Alt is not mapped`). The parser and the rules below live in `hotkeys/spec.py`; `config.py` calls them.

Validation at startup and on `reload`:

- the keysym must exist and have a keycode in the current map (`keysym_to_keycode != 0`),
- the PTT and continuous shortcuts must not be identical,
- `ptt_cancel_key` must be a single keysym without modifiers, distinct from the PTT and continuous keysyms,
- a shortcut whose keysym is `ISO_Level3_Shift`, `Alt_R`, or `Super_L` is rejected with a message about the conflict with AltGr or Mutter,
- a shortcut whose keysym is `Control_L` or `Shift_L` is rejected because XTest uses these keys when pasting ([08](08-text-injection.md) §8.5).

## 7.3 Implementation — `HotkeyListener`

The `local_stt/hotkeys/x11.py` module runs in a separate thread and has **its own** `Xlib.display.Display()` **connection**. X11 connections are not shared between threads.

### Grab

```python
LOCKS = [0, LockMask, numlock_mask, LockMask | numlock_mask]  # + scroll_lock_mask, if mapped

for hk in hotkeys:
    for extra in LOCKS:
        root.grab_key(hk.keycode, hk.mods | extra, owner_events=False,
                      pointer_mode=GrabModeAsync, keyboard_mode=GrabModeAsync,
                      onerror=catch_bad_access)
display.sync()
```

- `numlock_mask` and `scrolllock_mask` are determined dynamically by finding which row of `get_modifier_mapping()` contains the `Num_Lock` or `Scroll_Lock` keycode. On the reference machine, NumLock is `mod2`.
- Each grab is sent with `onerror=CatchError(BadAccess)`, followed by `display.sync()`. If `BadAccess` occurs, we log ERROR `hotkey <X> is already grabbed by another client`. The daemon continues without that shortcut, `status` shows `hotkeys: degraded`, and `doctor` reports the cause. If only some lock variants fail, all variants of that shortcut are ungrabbed (it would otherwise work only in some NumLock/CapsLock states). A keysym without a keycode (§7.2) is reported the same way, `<keysym> has no keycode in the current keyboard map`; this also applies to `ptt_cancel_key`, which is never grabbed.
- We do not use `AnyModifier`, as it would conflict with every existing grab for that keycode.
- **KeyPress** matching: `event.detail == hk.keycode and (event.state & ~lock_masks & RELEVANT) == hk.mods`, where `RELEVANT = Shift|Control|Mod1|Mod4`.
- **KeyRelease** matching — **by keycode only**. The state in a release event includes the modifier of the key being released (for example, `ControlMask` when releasing `Control_R`; empirically verified as `state=20`), so comparing masks would never match. A PTT release counts only when `ptt_down == True`. Release of the continuous key is ignored, so the shared `Control_R` keycode in both shortcuts is unambiguous: its meaning is determined by the KeyPress that set `ptt_down`.

### Event loop

```python
while running:
    timeout = 0.25 if ptt_down else None      # 0.25 s: check for a lost release (below)
    ready = select([display.fileno(), wakeup_pipe], [], [], timeout)
    while display.pending_events():
        ev = display.next_event()
        handle(ev)
    if ptt_down:
        check_lost_release_if_due()           # at most once every 250 ms
```

**Draining must repeat until Xlib's own queue is empty** (task 1.9, tested). Any round trip on the listener's connection — `refresh_keyboard_mapping()` after `MappingNotify`, `query_keymap()`, grabs — can read subsequent key events from the socket into python-xlib's internal queue; `select()` on the socket then does not wake for them. Found on Xvfb: XTest's first use of a keyboard sends `MappingNotify`, and in 10–20 % of runs the following `KeyPress`/`KeyRelease` stayed queued with the loop asleep. On GNOME, `MappingNotify` also arrives on a layout switch. The implementation drains in a loop (`while pending_events(): batch…; process(batch)`) and re-checks `pending_events()` after the keymap check before calling `select()`. `MappingNotify` only refreshes Xlib's keyboard mapping; grabs are not redone (that the configured keycodes survive a layout switch is a hypothesis, not tested on GNOME).

The wake-up pipe lets the Controller interrupt the loop on `reload` and `shutdown`. Grabbing and ungrabbing are always performed by the listener thread: the Controller submits them through the command queue and wakes the loop through the pipe.

### Press/release semantics

| X11 event | Condition | Emitted |
|---|---|---|
| `KeyPress` PTT | `ptt_down == False` | `PttPressed`, `ptt_down = True` |
| `KeyPress` PTT | `ptt_down == True` | nothing (auto-repeat) |
| `KeyRelease` PTT | the next queued event is a `KeyPress` with the same keycode and `time` | nothing; consume both events (auto-repeat) |
| `KeyRelease` PTT | otherwise | `PttReleased`, `ptt_down = False` |
| `KeyPress` `hotkeys.ptt_cancel_key` (default: `Escape`) | `ptt_down == True` | `PttCancelKey` |
| `KeyPress` continuous | `ptt_down == False` | `ContinuousToggle` |
| `KeyRelease` continuous | — | nothing |

- **Cancel key without a separate grab.** While PTT is held, an *active* keyboard grab is in effect and all key events reach the daemon. `Esc` therefore arrives without being grabbed globally, so applications do not lose `Esc`. Other keys pressed during this time are ignored.
- **Auto-repeat.** python-xlib 0.33 does not provide the XKB extension (`XkbSetDetectableAutoRepeat` is unavailable), so we use the classic “same keycode and timestamp” test. It does not matter for the default `Control_R`, but is required if the user configures, for example, `Pause` or `F9`. Tested on Xvfb: XTest events sent in one batch carry the same server timestamp while they fit within one server millisecond (in 1000 press-release-press-release batches, 6 crossed a millisecond boundary). When a `KeyRelease` is the last event read, the listener reads once more before deciding. The test (`tests/integration/test_hotkeys_x11.py`) brackets the batch with server timestamps and retries when they differ.
- **Protection against a lost release.** A release can be lost, for example when switching VTs or locking the screen. When `ptt_down == True`, the loop wakes every 250 ms (`select` timeout) and calls `display.query_keymap()`. If the PTT keycode bit is clear, it emits a synthetic `PttReleased` and logs a WARNING.

## 7.4 Loss of the X connection

An `Xlib.error.ConnectionClosedError` during operation (end of session) means the daemon cannot continue. The listener emits `X11ConnectionLost`, and the controller closes capture and the IPC socket **without any X11 operations** (ungrabbing is impossible and unnecessary). The process exits with code **0**, so `Restart=on-failure` does not restart it in a loop with a dead `DISPLAY`. After logout, `PartOf=graphical-session.target` stops the unit; at the next login it starts with the new environment ([11](11-daemon-systemd-installation.md)).

## 7.5 Startup

At startup, the daemon determines the graphical session type through `loginctl show-user $UID -p Display --value` → `loginctl show-session <id> -p Type --value` (verified: `x11`). `XDG_SESSION_TYPE` in the user manager environment may be stale after changing sessions, and `XDG_SESSION_ID` is not set there at all. `XDG_SESSION_TYPE` is used only as a fallback if `loginctl` fails.

- type ≠ `x11` → ERROR `unsupported session (only X11)` and exit with code 78 (`EX_CONFIG`). The unit has `RestartPreventExitStatus=78`, so systemd does not restart it in a loop.
- X11 session, but `DISPLAY` is absent from the environment or `Display()` raises a connection error (for example, a race while importing variables at login) → ERROR and exit with code **1**. `Restart=on-failure` retries startup every 2 s, at most 5 times in 60 s.

## 7.6 Interface (for future Wayland support)

```python
class HotkeyBackend(Protocol):
    def start(self, sink: Callable[[Event], None]) -> None: ...
    def apply(self, config: HotkeysConfig) -> list[HotkeyProblem]: ...
    def stop(self) -> None: ...
```

The only implementation in v0.1–v0.3 is `X11GrabHotkeys`. `HotkeyProblem(hotkey, value, reason)` (in `interfaces.py`) is what `status`/`doctor` show for `hotkeys: degraded`. `apply()` replaces all grabs, runs in the listener thread and blocks the caller until done; after the listener ends (X connection lost, `stop()`) `apply()` and `stop()` return immediately without X operations. The press/release table is implemented by `KeyRouter` (no X calls; unit-tested); `X11GrabHotkeys` adds the grabs, the auto-repeat filter and the lost-release check.

## 7.7 Control without grabs (alternative and additional shortcuts)

Every action can also be invoked from the CLI:

```bash
local-stt ptt start|stop     # for devices / scripts
local-stt toggle             # continuous start/stop
local-stt cancel
```

This allows additional shortcuts to be assigned through *Settings → Keyboard → Custom Shortcuts* in GNOME, for example `Ctrl+Alt+End` → `local-stt cancel` (available on the reference machine; `Ctrl+Alt+Esc` is used by `cycle-panels`). GNOME handles only press events, so hold-to-talk PTT is available only through the X11 grab.

Setting `hotkeys.enabled = false` disables grabbing entirely. Only the CLI then remains available.
