# 07. Globalne hotkeye (X11)

## 7.1 Domyślne przypisania

| Akcja | Domyślny klawisz | Typ |
|---|---|---|
| Push-to-talk | **prawy Ctrl** (`Control_R`), trzymany | press + release |
| Continuous start/stop | **Shift + prawy Ctrl** (`Shift+Control_R`) | toggle na press |
| Anuluj nagranie PTT | **Esc** (`hotkeys.ptt_cancel_key`) wciśnięty *podczas trzymania* prawego Ctrl | press |
| Anuluj continuous | `local-stt cancel` (opcjonalnie: własny skrót GNOME, zob. 7.7) | IPC |

### Dlaczego nie `Super+Space` z projektu wstępnego

1. **Konflikt.** Na Ubuntu 24.04 `org.gnome.desktop.wm.keybindings switch-input-source = ['<Super>space', 'XF86Keyboard']`, a `switch-input-source-backward = ['<Shift><Super>space', ...]`. Oba skróty z projektu są zajęte (sprawdzone na maszynie referencyjnej).
2. **Overlay key.** Mutter łapie `Super_L` i otwiera Activities, jeśli po jego puszczeniu nie przyszedł inny klawisz. Własne grabby na `Super+X` źle współpracują z tą logiką (znany problem Ulaunchera i Alberta w GNOME X11).
3. **Ergonomia PTT.** Trzymanie dwóch klawiszy przez kilkanaście sekund mowy jest niewygodne. Jeden duży klawisz pod kciukiem lub małym palcem jest lepszy.

### Dlaczego prawy Ctrl

- Mutter go nie używa: `locate-pointer-key = 'Control_L'` i jest to tylko lewy Ctrl, a sama funkcja jest wyłączona.
- **Prawego Alt nie można użyć.** To AltGr (`ISO_Level3_Shift`, `mod5`), bez którego nie da się wpisać ą, ę, ł itd.
- Prawy Ctrl w praktyce służy rzadko jako modyfikator. Koszt: dopóki daemon działa, kombinacje `prawy Ctrl + klawisz` nie docierają do aplikacji, bo grab je przejmuje (lewy Ctrl działa normalnie).
- Modyfikatory nie mają autorepeat, więc PTT nie generuje fałszywych par release/press.
- **Zweryfikowano empirycznie** na maszynie referencyjnej (python-xlib 0.33, zdarzenia wstrzykiwane przez XTest):
  - grab `Control_R` i `Shift+Control_R` z wariantami NumLock/CapsLock zostaje przyjęty bez `BadAccess`,
  - `KeyPress` i `KeyRelease` docierają do daemona,
  - przy kombinacji `Shift+Control_R` puszczenie Shifta *przed* Ctrl również trafia do daemona (aktywny grab), a `KeyRelease Control_R` przychodzi poprawnie.

Klawisze są konfigurowalne ([09](09-konfiguracja.md)). Dobra alternatywa na klawiaturach z `Menu`/`Pause`/`Insert`/`Scroll_Lock` to pojedynczy klawisz, którego nikt nie używa.

## 7.2 Składnia w konfiguracji

```text
hotkey  := (modifier "+")* keysym
modifier:= "Ctrl" | "Shift" | "Alt" | "Super"
keysym  := nazwa keysym X11, np. Control_R, Pause, F9, space, Menu
```

`"Control_R"` oznacza klawisz bez dodatkowych modyfikatorów. `"Shift+Control_R"` oznacza klawisz z wciśniętym Shiftem. Nazwy modyfikatorów mapujemy na maski `ShiftMask`, `ControlMask`, `Mod1Mask` i `Mod4Mask`. Bezpośrednio przed grabem sprawdzamy mapowanie przez `get_modifier_mapping()`.

Walidacja przy starcie i przy `reload`:

- keysym musi istnieć i mieć keycode w bieżącej mapie (`keysym_to_keycode != 0`),
- skrót PTT i skrót continuous nie mogą być identyczne,
- `ptt_cancel_key` to pojedynczy keysym bez modyfikatorów, różny od keysymów PTT i continuous,
- skrót, którego keysym to `ISO_Level3_Shift`, `Alt_R` albo `Super_L`, jest odrzucany z komunikatem o konflikcie z AltGr lub mutterem,
- skrót, którego keysym to `Control_L` albo `Shift_L`, jest odrzucany, bo tych klawiszy używa XTest przy wklejaniu ([08](08-text-injection.md) §8.5).

## 7.3 Implementacja — `HotkeyListener`

Moduł `local_stt/hotkeys/x11.py` działa w osobnym wątku i ma **własne połączenie** `Xlib.display.Display()`. Połączenia X11 nie są współdzielone między wątkami.

### Grab

```python
LOCKS = [0, LockMask, numlock_mask, LockMask | numlock_mask]  # + scroll_lock_mask, jeśli zmapowany

for hk in hotkeys:
    for extra in LOCKS:
        root.grab_key(hk.keycode, hk.mods | extra, owner_events=False,
                      pointer_mode=GrabModeAsync, keyboard_mode=GrabModeAsync,
                      onerror=catch_bad_access)
display.sync()
```

- `numlock_mask` i `scrolllock_mask` wyznaczamy dynamicznie: szukamy, w którym wierszu `get_modifier_mapping()` jest keycode `Num_Lock` lub `Scroll_Lock`. Na maszynie referencyjnej NumLock to `mod2`.
- Każdy grab wysyłamy z `onerror=CatchError(BadAccess)`, a potem wywołujemy `display.sync()`. Jeśli wystąpi `BadAccess`, logujemy ERROR `hotkey <X> is already grabbed by another client`. Daemon działa dalej bez tego skrótu, `status` pokazuje `hotkeys: degraded`, a `doctor` wypisuje przyczynę.
- Nie używamy `AnyModifier`: kolidowałby z każdym istniejącym grabem na tym keycode.
- Dopasowanie **KeyPress**: `event.detail == hk.keycode and (event.state & ~lock_masks & RELEVANT) == hk.mods`, gdzie `RELEVANT = Shift|Control|Mod1|Mod4`.
- Dopasowanie **KeyRelease** — **tylko po keycode**. Stan w zdarzeniu release zawiera modyfikator zwalnianego klawisza (np. `ControlMask` przy puszczaniu `Control_R`, sprawdzone empirycznie: `state=20`), więc porównanie masek nigdy by nie trafiło. Release PTT liczy się wyłącznie przy `ptt_down == True`. Release klawisza continuous jest ignorowany, więc wspólny keycode `Control_R` w obu skrótach nie jest dwuznaczny: o znaczeniu decyduje KeyPress, który ustawił `ptt_down`.

### Pętla zdarzeń

```python
while running:
    timeout = 0.25 if ptt_down else None      # 0,25 s: kontrola zgubionego release (niżej)
    ready = select([display.fileno(), wakeup_pipe], [], [], timeout)
    while display.pending_events():
        ev = display.next_event()
        handle(ev)
    if ptt_down:
        check_lost_release_if_due()           # najwyżej raz na 250 ms
```

Wake-up pipe pozwala Controllerowi przerwać pętlę przy `reload` i `shutdown`. Grab i ungrab zawsze wykonuje wątek listenera: Controller zleca je przez kolejkę poleceń i wybudza pętlę przez pipe.

### Semantyka press/release

| Zdarzenie X11 | Warunek | Emitowane |
|---|---|---|
| `KeyPress` PTT | `ptt_down == False` | `PttPressed`, `ptt_down = True` |
| `KeyPress` PTT | `ptt_down == True` | nic (autorepeat) |
| `KeyRelease` PTT | następne zdarzenie w kolejce to `KeyPress` tego samego keycode z tym samym `time` | nic, oba zdarzenia zjadamy (autorepeat) |
| `KeyRelease` PTT | w pozostałych przypadkach | `PttReleased`, `ptt_down = False` |
| `KeyPress` `hotkeys.ptt_cancel_key` (domyślnie `Escape`) | `ptt_down == True` | `PttCancelKey` |
| `KeyPress` continuous | `ptt_down == False` | `ContinuousToggle` |
| `KeyRelease` continuous | — | nic |

- **Klawisz anulowania bez osobnego grabu.** Dopóki PTT jest trzymany, trwa *aktywny* grab klawiatury i wszystkie zdarzenia klawiszy trafiają do daemona. `Esc` dociera więc bez grabowania go globalnie, a aplikacje nie tracą `Esc`. Inne klawisze wciśnięte w tym czasie są ignorowane.
- **Autorepeat.** python-xlib 0.33 nie ma rozszerzenia XKB (`XkbSetDetectableAutoRepeat` jest niedostępne), więc stosujemy klasyczny test „ten sam keycode i timestamp”. Dla domyślnego `Control_R` nie ma to znaczenia, ale test jest potrzebny, gdy użytkownik ustawi np. `Pause` lub `F9`.
- **Zabezpieczenie przed zgubionym release.** Release może zginąć, np. przy przełączeniu VT albo zablokowaniu ekranu. Gdy `ptt_down == True`, pętla budzi się co 250 ms (timeout `select`) i wywołuje `display.query_keymap()`. Jeśli bit keycode PTT jest zgaszony, emituje syntetyczny `PttReleased` i loguje WARNING.

## 7.4 Utrata połączenia z X

`Xlib.error.ConnectionClosedError` w trakcie pracy (koniec sesji) oznacza, że nie da się dalej działać. Listener emituje `X11ConnectionLost`, a controller zamyka capture i gniazdo IPC **bez żadnych operacji X11** (ungrab jest niemożliwy i zbędny). Proces kończy się kodem **0**, więc `Restart=on-failure` nie restartuje go w pętli z martwym `DISPLAY`. Po wylogowaniu `PartOf=graphical-session.target` zatrzymuje unit, a przy następnym logowaniu startuje on z nowym środowiskiem ([11](11-daemon-systemd-instalacja.md)).

## 7.5 Start

Przy starcie daemon ustala typ sesji graficznej przez `loginctl show-user $UID -p Display --value` → `loginctl show-session <id> -p Type --value` (zweryfikowane: `x11`). Zmienne `XDG_SESSION_TYPE` w środowisku menedżera użytkownika mogą być nieaktualne po zmianie sesji, a `XDG_SESSION_ID` nie jest tam w ogóle ustawiane. `XDG_SESSION_TYPE` służy tylko jako fallback, gdy `loginctl` zawiedzie.

- typ ≠ `x11` lub brak `DISPLAY` → ERROR `unsupported session (only X11)` i wyjście z kodem 78 (`EX_CONFIG`). Unit ma `RestartPreventExitStatus=78`, więc systemd nie restartuje go w pętli.
- Sesja X11, ale `Display()` rzuca błąd połączenia (np. wyścig przy logowaniu) → ERROR i wyjście z kodem **1**. `Restart=on-failure` ponawia start co 2 s, maks. 5 razy w 60 s.

## 7.6 Interfejs (dla przyszłego Waylanda)

```python
class HotkeyBackend(Protocol):
    def start(self, sink: Callable[[Event], None]) -> None: ...
    def apply(self, config: HotkeysConfig) -> list[HotkeyProblem]: ...
    def stop(self) -> None: ...
```

Jedyną implementacją w v0.1–v0.3 jest `X11GrabHotkeys`.

## 7.7 Sterowanie bez grabów (alternatywa i skróty dodatkowe)

Każdą akcję można też wywołać z CLI:

```bash
local-stt ptt start|stop     # dla urządzeń / skryptów
local-stt toggle             # continuous start/stop
local-stt cancel
```

Dzięki temu dodatkowe skróty można przypisać przez *Ustawienia → Klawiatura → Skróty własne* w GNOME, np. `Ctrl+Alt+End` → `local-stt cancel` (na maszynie referencyjnej wolny; `Ctrl+Alt+Esc` jest zajęty przez `cycle-panels`). GNOME obsługuje tylko press, więc do PTT z trzymaniem służy wyłącznie grab X11.

Ustawienie `hotkeys.enabled = false` całkowicie wyłącza grabowanie. Wtedy działa tylko CLI.
