# 08. Przetwarzanie tekstu i wpisywanie do okna

```text
Transcript ──► TextProcessor ──► str | None ──► Injector ──► InjectResult
```

`TextProcessor` nie wie nic o X11. `Injector` nie wie nic o Whisperze.

## 8.1 Kontekst przetwarzania

```python
@dataclass(frozen=True)
class TextContext:
    source: Literal["ptt", "continuous"]
    session_id: int | None
    seq: int | None
    cut: Literal["release", "max_duration", "silence", "max_length", "flush"]
    prev_cut: str | None          # cut poprzedniego fragmentu z tej sesji (continuous)
    prompt_tail: str | None       # końcówka promptu przekazana do silnika
```

## 8.2 `TextProcessor` — kroki (w tej kolejności)

1. **Filtr segmentów** ([06](06-silnik-stt.md) §6.8): `no_speech_prob` razem z `avg_logprob`, lista halucynacji, pętle powtórzeń, echo promptu.
2. **Złożenie** pozostałych segmentów: `" ".join(s.text.strip())`.
3. **Normalizacja białych znaków**: sekwencje spacji i tabulatorów → jedna spacja, `strip()`. Znaki nowej linii z Whispera zamieniamy na spację.
4. **Zamiany użytkownika** `text.replacements`: lista `{pattern, replace, regex}` stosowana po kolei, np. `{pattern = "(?i)\\bnowa linia\\b", replace = "\n", regex = true}`. To jedyny mechanizm „komend” w v0.1–v0.3.
5. **Ciągłość continuous**:
   - jeśli `cut in ("max_length", "max_duration")` (fragment kończy się w środku wypowiedzi) i tekst kończy się pojedynczą kropką, ta kropka jest usuwana (`?`, `!` i `…` zostają),
   - jeśli `prev_cut == "max_length"`, a pierwsza litera jest wielka i drugie słowo nie jest pisane wielką literą (heurystyka „to nie nazwa własna”), pierwsza litera jest zamieniana na małą.
6. **Separator**: przy `text.append_space = true` (domyślnie) do każdego niepustego wyniku dopisujemy **spację na końcu**. Kolejne fragmenty i następne dyktowania kleją się wtedy naturalnie, bez pamiętania stanu okna.
7. Pusty wynik → `None`, pipeline pomija wpisywanie.

Każdy krok jest czystą funkcją z testami jednostkowymi ([14](14-testy.md)).

## 8.3 Interfejs Injector

```python
class Injector(Protocol):
    def inject(self, text: str) -> InjectResult: ...

@dataclass(frozen=True)
class InjectResult:
    ok: bool
    backend: str                     # "clipboard" | "type"
    chars: int
    window_class: str | None
    left_in_clipboard: bool          # tekst celowo zostawiony w schowku
    error: str | None
```

Implementacje (v0.1): `ClipboardPasteInjector`, `XdotoolTypeInjector`, `AutoInjector` (wybór per okno). W testach `RecordingInjector`.

## 8.4 Wybór metody — dlaczego domyślnie schowek + wklejenie

| Kryterium | `xdotool type` | schowek + Ctrl+V (własny właściciel selekcji) |
|---|---|---|
| Polskie znaki | zależne od keymapy. Ubuntu 24.04 ma xdotool **3.20160805** (bez poprawek z 2025–2026); znane błędy z wieloma układami (#150, #354) i wyścigi `MappingNotify` przy remapowaniu keycode'ów → zgubione/złe znaki w Chrome/Electron | niezależne od układu klawiatury, Unicode 1:1 |
| Szybkość | ~12 ms/znak → 300 znaków ≈ 4 s, użytkownik nie może pisać w tym czasie | jedna operacja, < 100 ms |
| Atomowość | wpisywanie można przerwać zmianą fokusu w trakcie | atomowe |
| Terminale | działa | wymaga `Ctrl+Shift+V` (wybór po `WM_CLASS`) |
| Skutki uboczne | brak | nadpisuje CLIPBOARD → zapis i przywrócenie wszystkich celów ≤ 256 KiB (większa treść → fallback `type`); menedżery historii schowka zobaczą tekst |
| Aplikacje ignorujące XTest | nie działa | nie działa (ten sam XTest do Ctrl+V) |

Decyzja: `injection.backend = "auto"`:

- domyślnie **clipboard**,
- **type** dla okien z `injection.type_window_classes` (domyślnie `["xterm", "URxvt"]`, bo xterm nie ma wklejania CLIPBOARD pod Ctrl+Shift+V),
- **type** także wtedy, gdy obecnej zawartości schowka **nie da się wiernie zapisać i przywrócić** (8.5, krok 3), żeby jej nie zniszczyć.

## 8.5 `ClipboardPasteInjector` — algorytm

Komponenty:

- `ClipboardOwner` — wątek z **własnym połączeniem X11** i niewidocznym oknem 1×1. Obsługuje `SelectionRequest`, `SelectionClear` i `SelectionNotify`, a także przechowuje aktualnie serwowaną zawartość (`served: dict[target, (type, format, bytes)]`) oraz to, co zapisaliśmy od użytkownika (`user_saved`).
- `KeySender` — XTest (`Xlib.ext.xtest.fake_input`) na połączeniu wątku roboczego.

Kroki `inject(text)`:

1. **Poczekaj na puszczenie modyfikatorów.** Co 20 ms wywołujemy `query_keymap()` i sprawdzamy bity keycode'ów z `get_modifier_mapping()`, maks. `injection.modifier_wait_ms` (1000). Jeśli czas minie, logujemy WARNING `modifiers still held` i kontynuujemy. Nie używamy `--clearmodifiers` ani sztucznego zwalniania, bo zostawia „zawieszone” modyfikatory, gdy użytkownik puści klawisz w trakcie. **Wyjątek:** gdy wciśnięty jest klawisz PTT (trwa nowe nagranie, aktywny grab przechwyciłby wstrzyknięte klawisze), czekamy bez limitu do jego puszczenia, a wpisanie następuje zaraz po nim.
2. **Okno docelowe.** Czytamy `_NET_ACTIVE_WINDOW` z roota, a potem `WM_CLASS` (instance, class).
   - Wartość `0` lub okno pulpitu → **brak celu**: tekst trafia do schowka bez przywracania, `left_in_clipboard = true`, powiadomienie „Brak aktywnego pola — tekst jest w schowku”. Koniec.
3. **Zapis schowka.**
   - **My jesteśmy właścicielem** (serwujemy przywróconą treść użytkownika albo tekst z nieudanego wklejenia) → `saved = user_saved` (to, co było u użytkownika *przed* naszą pierwszą ingerencją; tekst z nieudanego wklejenia nie nadpisuje `user_saved`).
   - **Właścicielem jest inny klient** → `ConvertSelection(CLIPBOARD, TARGETS)` (timeout 300 ms), potem pobranie **każdego** celu poza meta (`TARGETS`, `TIMESTAMP`, `MULTIPLE`, `SAVE_TARGETS`, `DELETE`). Dla każdego zapamiętujemy bajty, typ właściwości i format (8/16/32). Limity: maks. 32 cele, każdy ≤ 256 KiB, łącznie ≤ 1 MiB, cała operacja ≤ 500 ms.
     - Wszystko w limitach → `saved = {target: (type, format, bytes)}`, `user_saved = saved`. Bajty odtwarzamy 1:1, więc nie musimy rozumieć formatu. Dzięki temu kopia z Firefoksa/Chrome (tekst, `text/html` i metadane przeglądarki) czy pliki z Nautilusa (`x-special/gnome-copied-files`) wracają w całości.
     - Przekroczony limit albo właściciel odpowiada przez INCR (typowo obrazy, duże dokumenty) → **przełączenie na `XdotoolTypeInjector`** (nie potrafimy wiernie odtworzyć schowka, więc go nie ruszamy).
     - Brak odpowiedzi lub brak właściciela → `saved = None`.
4. **Przejęcie schowka.** `set_selection_owner(CLIPBOARD, our_window, time)` i sprawdzenie `get_selection_owner` == nasze okno. Serwujemy cele `TARGETS`, `UTF8_STRING`, `text/plain;charset=utf-8`, `TEXT`, `STRING` (`STRING` tylko, gdy tekst mieści się w Latin-1). Limit tekstu: 64 KiB bez obsługi INCR, a dłuższy tekst → backend `type` (w praktyce nie występuje).
5. **Skrót wklejania** wybierany po `WM_CLASS` (porównanie bez wielkości liter):
   - `injection.paste_shortcut_overrides` (mapa klasa → skrót),
   - klasy z `injection.terminal_window_classes` → `Ctrl+Shift+V`,
   - pozostałe → `Ctrl+V`.
6. **Wysłanie skrótu przez XTest.** Zawsze używamy keycode'ów **`Control_L` i `Shift_L`**, nigdy `Control_R`/`Shift_R`. Walidator ([09](09-konfiguracja.md) §9.3) odrzuca skróty daemona zawierające `Control_L` lub `Shift_L` jako keysym, więc wstrzyknięty klawisz nie może uruchomić naszego własnego grabu PTT. Sekwencja: press modyfikatorów → press/release `v` (keycode z `keysym_to_keycode(XK_v)`) → release modyfikatorów, każdy krok z `sync()` i 8 ms przerwy. Zapamiętujemy `t_sent` (czas monotoniczny tuż przed pierwszym press).
7. **Potwierdzenie.** `ClipboardOwner` liczy tylko żądania celu tekstowego (nie `TARGETS`), które spełniają oba warunki:
   - przyszły **po `t_sent`** (odrzuca menedżery schowka, które pobierają treść od razu po zmianie właściciela, przez XFixes),
   - okno `requestor` należy **do tego samego klienta X co okno aktywne**: `requestor & ~resource_id_mask == active_window & ~resource_id_mask`, gdzie maska pochodzi z `display.info.resource_id_mask`. Identyfikatory zasobów X zawierają bazę klienta, więc nie potrzeba rozszerzenia XRes.

   Czekamy maks. `injection.paste_timeout_ms` (1000):
   - potwierdzone → po dodatkowych 150 ms (aplikacje czasem pobierają kilka celów) przechodzimy do przywracania,
   - brak potwierdzenia → aplikacja nie wkleiła (fokus nie w polu tekstowym albo okno ignoruje XTest). **Nie przywracamy schowka**: tekst zostaje w schowku, `left_in_clipboard = true`, WARNING w logu i powiadomienie „Nie udało się wkleić — tekst jest w schowku (Ctrl+V)”. Dyktowanie nigdy nie ginie po cichu. `user_saved` zostaje zachowane i posłuży przy kolejnym udanym wklejeniu.
8. **Przywrócenie** (`injection.restore_clipboard = true`):
   - `saved` niepuste → nadal jesteśmy właścicielem, ale serwujemy już `saved` (wszystkie zapisane cele), dopóki inna aplikacja nie przejmie schowka. Wtedy `SelectionClear` → czyścimy `served` i `user_saved`,
   - `saved is None` → `set_selection_owner(CLIPBOARD, X.NONE)`.
   - Po wyjściu daemona przywrócona treść znika, jeśli oryginalny właściciel już jej nie serwuje. To samo zachowanie ma `xclip`. Akceptujemy to.

PRIMARY (zaznaczenie środkowym przyciskiem) **nie jest ruszane**.

## 8.6 `XdotoolTypeInjector`

```bash
xdotool type --delay 12 -- "<text>"
```

- Uruchamiany przez `subprocess.run([...], timeout=max(5, len(text)*0.05))`. Tekst przekazujemy jako argument (nie przez powłokę), więc nie ma problemu z escapingiem.
- Najpierw ten sam krok 1 (czekanie na puszczenie modyfikatorów). **Bez `--clearmodifiers`.**
- Tekst dzielimy na kawałki po 200 znaków, żeby timeout i anulowanie działały między kawałkami (sprawdzenie generacji pipeline).
- `\n` → xdotool wysyła `Return`.
- `injection.type_delay_ms` (12) to parametr. Wartości < 8 ms w Chrome gubią znaki.

## 8.7 Zachowania brzegowe

| Sytuacja | Zachowanie |
|---|---|
| Użytkownik zmienił okno między nagraniem a wpisaniem | tekst trafia do okna aktywnego w chwili wpisywania (świadoma prostota; log DEBUG z klasami obu okien) |
| Pole hasła / okno blokady ekranu | nie wykrywamy; ekran blokady GNOME ma własny grab, więc XTest tam nie trafia |
| Użytkownik pisze na klawiaturze w trakcie continuous | wklejenie jest atomowe, więc może wstawić się między jego znaki; akceptowane, udokumentowane |
| `xdotool` nieobecny | backend `type` niedostępny → `doctor` WARN; `auto` używa wtedy wyłącznie schowka, a gdy schowka nie da się zapisać (krok 3), wkleja bez przywracania i loguje WARNING |
| Błąd X11 w injectorze | `InjectResult(ok=False)`, tekst w schowku (jeśli się da), powiadomienie |

## 8.8 Wayland (przyszłość)

Na Waylandzie obie implementacje przestają działać. Nowe backendy (`wl-copy` + `ydotool`/`dotool`, portal RemoteDesktop) implementują ten sam `Injector`. Wybór backendu `auto` zależy wtedy od `XDG_SESSION_TYPE`. Poza zakresem v0.1–v0.3.
