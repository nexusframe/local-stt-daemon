# 03. Rejestr decyzji (ADR)

Każda decyzja ma ten sam układ: kontekst → rozważone opcje → decyzja → **najmocniejszy zarzut i odpowiedź** → kiedy wrócić do tematu.

Oznaczenia:

- ✅ — decyzja wiążąca,
- 🧪 — decyzja tymczasowa, rozstrzygnie ją benchmark ([13](13-benchmark.md)).

---

## ADR-001 ✅ Daemon w Pythonie 3.12

- **Opcje:** Python, Rust, C++ (linkowanie whisper.cpp), Go.
- **Decyzja:** Python 3.12 (systemowy) w venv.
- **Uzasadnienie:**
  - ciężka praca (inferencja) i tak dzieje się w C++ w osobnym procesie,
  - daemon to I/O i sklejanie komponentów,
  - dojrzałe biblioteki: sounddevice, onnxruntime, python-xlib,
  - najkrótsza droga do MVP, łatwe testy.
- **Zarzut:** GIL i narzut Pythona przy audio w czasie rzeczywistym.
- **Odpowiedź:** strumień to 31 ramek/s po 512 próbek. Callback PortAudio tylko kopiuje dane, a onnxruntime i numpy zwalniają GIL. Budżet N4 (≤ 5% rdzenia) jest weryfikowany w benchmarku.
- **Rewizja:** jeśli N4 lub N1 nie zostaną spełnione po profilowaniu.

## ADR-002 ✅ STT w osobnym procesie `whisper-server` (HTTP po loopbacku)

- **Opcje:** `whisper-server`; bindingi (`pywhispercpp`/ctypes); `whisper-cli` per nagranie; faster-whisper.
- **Decyzja:** `whisper-server` z przypiętego tagu whisper.cpp jako osobna usługa systemd. Szczegóły: [06](06-silnik-stt.md) §6.1.
- **Zarzut:** dodatkowy port TCP to powierzchnia ataku i ryzyko prywatności.
- **Odpowiedź:**
  - nasłuch tylko na `127.0.0.1` (host wpisany na sztywno w generatorze `whisper-server.env`, weryfikacja w `doctor`),
  - serwer nie przechowuje danych,
  - wszystkie endpointy są pod losowym `--request-path` (sekret 0600), więc strona WWW nie wywoła `/load` ani `/inference` (CSRF),
  - lokalny proces użytkownika z dostępem do sekretu może co najwyżej zlecić transkrypcję własnego audio.
- **Zarzut 2:** dwa procesy to więcej do zarządzania niż jeden.
- **Odpowiedź:**
  - systemd zarządza oboma,
  - w zamian: izolacja awarii C++, natywny build i ten sam binarny plik do benchmarku,
  - zmiana modelu to restart usługi, a nie `POST /load`, który w v1.9.4 potrafi zostawić serwer w stanie `loading` albo go zakończyć ([06](06-silnik-stt.md) §6.5).
- **Rewizja:** jeśli whisper.cpp usunie serwer albo narzut HTTP okaże się mierzalny (> 5% latencji).

## ADR-003 🧪 Model domyślny `small-q5_1`; `base` tylko do testów

- **Kontekst:** projekt wstępny traktował `base` jako wariant „responsywny”.
- **Fakty:** WER dla polskiego (FLEURS) z paperu Whisper: base 30,8%, small 14,7%, medium 8,0%.
- **Decyzja:** start od `small-q5_1` (181 MiB). Ostateczny wybór wynika z reguły z [13](13-benchmark.md) §13.5, a kandydatami są też `medium-q5_0` i `large-v3-turbo-q5_0` z `audio_ctx`.
- **Zarzut:** `small` może być za wolny dla continuous na i5-8365U.
- **Odpowiedź:**
  - continuous nie blokuje nagrywania (ADR-004),
  - backlog ma twardy limit z komunikatem,
  - jeśli soak nie przejdzie, benchmark wskaże szybszą konfigurację (`audio_ctx`, q5).
  - `base` z 1/3 błędnych słów nie jest użyteczny, niezależnie od szybkości.

## ADR-004 ✅ Stan jako trzy niezależne składowe, jeden wątek-właściciel, capture nigdy nie jest wstrzymywany

- **Kontekst:** diagram wstępny `LISTENING → TRANSCRIBING → INJECT → LISTENING` gubi mowę w trakcie transkrypcji.
- **Decyzja:** `(mode, pipeline, engine)`, zdarzenia do jednej kolejki `controller`, kolejka zadań z jednym workerem i generacjami do anulowania. Szczegóły: [04](04-maszyna-stanow.md).
- **Zarzut:** kolejka może rosnąć bez końca, jeśli silnik jest za wolny.
- **Odpowiedź:** `continuous.max_backlog_s` wyłącza tryb z jasnym komunikatem. Lepsze to niż ciche gubienie mowy albo wielominutowe opóźnienie.

## ADR-005 ✅ Audio: `sounddevice`, strumień otwierany na żądanie

- **Opcje:** sounddevice (PortAudio); `pw-record`/`parec` jako podproces; GStreamer; PyAudio.
- **Decyzja:** sounddevice, 16 kHz mono float32, blok 512. Strumień jest otwarty tylko w trakcie PTT i continuous.
- **Zarzut:** otwieranie przy każdym PTT ucina początek wypowiedzi.
- **Odpowiedź:**
  - dźwięk `start` grany po pierwszej ramce wyznacza moment rozpoczęcia mowy,
  - próbki z okna dźwięku startu są odrzucane, więc sygnał nie trafia do transkrypcji,
  - stale otwarty mikrofon to permanentny wskaźnik prywatności w GNOME i przełączanie słuchawek BT w profil HFP, czyli gorszy kompromis.
- **Zarzut 2:** PortAudio 19.6 nie ma natywnego backendu PulseAudio/PipeWire.
- **Odpowiedź:** otwieramy PCM `pipewire` (pipewire-alsa), a źródło wybieramy zmienną `PIPEWIRE_NODE` (zweryfikowane). PipeWire resampluje i obsługuje hot-plug, a awaryjnie resamplujemy w procesie (`soxr`).

## ADR-006 ✅ VAD: Silero v6.2.1 przez onnxruntime w daemonie

- **Opcje:**
  - Silero ONNX,
  - webrtcvad,
  - próg energii,
  - wbudowany VAD whisper.cpp (`--vad`),
  - Silero przez C API whisper.cpp (ctypes).
- **Decyzja:** Silero ONNX w daemonie, strumieniowo, z histerezą ([05](05-audio-i-vad.md)).
- **Dlaczego nie VAD w serwerze:** VAD serwera działa na już wysłanym nagraniu. Continuous potrzebuje decyzji „koniec wypowiedzi” *na żywo*, żeby w ogóle wiedzieć, co wysłać.
- **Dlaczego nie webrtcvad lub energia:** słaba odporność na szum (klawiatura, wentylator laptopa), a to typowe warunki tej maszyny.
- **Zarzut:** onnxruntime to ~15 MB+ zależności.
- **Odpowiedź:** akceptowalne. Alternatywa przez ctypes do libwhisper wymagałaby budowy biblioteki współdzielonej i własnego ABI, przy niepewnej stabilności API.

## ADR-007 ✅ Hotkeye: XGrabKey (python-xlib); PTT = prawy Ctrl, continuous = Shift + prawy Ctrl

- **Opcje:**
  - `Super+Space` (z projektu),
  - XGrabKey,
  - XRecord/pynput (nasłuch wszystkich klawiszy),
  - XInput2 raw events,
  - evdev (`/dev/input`, grupa `input`),
  - skróty własne GNOME.
- **Decyzja:** XGrabKey na root z wariantami Lock/NumLock. Domyślne klawisze bez Super. Szczegóły: [07](07-hotkeys-x11.md).
- **Odrzucone:**
  - `Super+Space` — zajęte przez przełączanie źródeł wprowadzania i konflikt z overlay-key muttera,
  - skróty GNOME — brak zdarzenia release,
  - evdev — wymaga dodania użytkownika do grupy `input`, a to daje dostęp do wszystkich klawiatur (keylogger),
  - XRecord i XI2 raw — nasłuch *wszystkich* klawiszy to niepotrzebny zakres danych, a klawisz trafia też do aplikacji.
- **Zarzut:** grab prawego Ctrl odbiera go aplikacjom.
- **Odpowiedź:** tak, to świadomy koszt. Lewy Ctrl zostaje. Klawisz można zmienić w configu na `Pause`/`Menu`/`Insert`. Zweryfikowano empirycznie, że grab działa w tej sesji GNOME.

## ADR-008 ✅ Konfiguracja w TOML

- **Opcje:** YAML (z projektu), TOML, JSON, INI.
- **Decyzja:** TOML (`tomllib` w stdlib, zero zależności, typy jednoznaczne, komentarze).
- **Zarzut:** `tomllib` tylko czyta, a `reload` może chcieć zapisywać.
- **Odpowiedź:** daemon nigdy nie zapisuje configu użytkownika. Generuje wyłącznie `whisper-server.env`.

## ADR-009 ✅ Wpisywanie: schowek + XTest Ctrl(+Shift)+V z własnym właścicielem selekcji; `xdotool type` jako fallback

- **Opcje:** `xdotool type`; `xclip` + `xdotool key`; własna selekcja w python-xlib + XTest; XTest z remapowaniem keycode'ów.
- **Decyzja:** własny `ClipboardOwner`. Dzięki temu **wiemy**, czy aplikacja pobrała tekst (SelectionRequest), co daje potwierdzenie wklejenia i bezpieczne przywrócenie schowka. Fallback `type` dla xterm i dla zawartości schowka, której nie da się wiernie zapisać. Szczegóły: [08](08-text-injection.md).
- **Zarzut:** nadpisywanie schowka to skutek uboczny, którego użytkownik się nie spodziewa.
- **Odpowiedź:**
  - zapisujemy i przywracamy wszystkie cele schowka do 256 KiB każdy; większa treść (np. zrzut ekranu) wymusza `type`, żeby jej nie niszczyć,
  - potwierdzenie liczy tylko żądania od klienta aktywnego okna po wysłaniu skrótu, więc menedżery schowka go nie fałszują,
  - `xdotool` 3.20160805 z Ubuntu 24.04 ma udokumentowane problemy z polskimi znakami i wieloma układami, a polskie znaki to rdzeń projektu,
  - backend jest przełączalny w configu.

## ADR-010 ✅ Wpisujemy tylko finalne fragmenty

- **Decyzja:** brak wyników częściowych w oknie docelowym (zgodnie z projektem wstępnym, §7).
- **Zarzut:** w continuous użytkownik długo nie widzi efektu.
- **Odpowiedź:**
  - `min_silence_ms = 700` + latencja silnika to zwykle 2–4 s od końca zdania,
  - podgląd częściowy (tylko powiadomienie lub `status --watch`, nigdy wpisywany) jest rozważany w v0.3,
  - wpisywanie i cofanie w cudzych aplikacjach jest zawodne na X11.

## ADR-011 ✅ Tylko X11 w v0.1–v0.3; Wayland przez interfejsy

- **Kontekst:** docelowa sesja to Ubuntu on Xorg.
- **Decyzja:** jedyne implementacje `HotkeyBackend` i `Injector` są X11-owe. Na starcie daemon sprawdza sesję i kończy się czytelnym błędem (kod 78) poza X11.
- **Rewizja:** gdy użytkownik przejdzie na Wayland. Wtedy dochodzą: portal GlobalShortcuts lub evdev dla hotkeyów oraz `wl-copy` + `ydotool`/`dotool` dla wpisywania.

## ADR-012 ✅ IPC: gniazdo Unix + JSON Lines

- **Opcje:** D-Bus (session bus), gniazdo Unix, HTTP na loopbacku, pliki/FIFO.
- **Decyzja:** gniazdo Unix `0600` w `$XDG_RUNTIME_DIR` + `SO_PEERCRED`, protokół JSON Lines ([10](10-cli-ipc-status.md)).
- **Zarzut:** D-Bus jest standardem desktopowym i ułatwiłby integrację z rozszerzeniami GNOME.
- **Odpowiedź:** D-Bus w Pythonie wymaga `dbus-next`/`PyGObject` i pętli zdarzeń. Gniazdo to ~150 linii w stdlib. Ewentualny most D-Bus można dodać później jako klienta IPC.

## ADR-013 ✅ Sygnalizacja: dźwięki + powiadomienia (tylko błędy) + `status`; bez traya w MVP

- **Zarzut:** bez ikony użytkownik nie wie, czy continuous jest włączony.
- **Odpowiedź:**
  - wyraźne dźwięki start i stop,
  - GNOME pokazuje wskaźnik mikrofonu, gdy strumień jest otwarty (a otwarty jest tylko w trakcie nagrywania, co daje darmowy i wiarygodny wskaźnik),
  - `systemctl --user status` pokazuje `STATUS=`.
  - Tray (AppIndicator wymaga rozszerzenia w GNOME) jest rozszerzeniem po v0.3.

## ADR-014 ✅ Wątki zamiast asyncio

Uzasadnienie w [02](02-architektura.md) §2.2. Wszystkie kluczowe biblioteki są blokujące.

## ADR-015 ✅ whisper.cpp budowany lokalnie z przypiętego tagu (v1.9.4)

- **Opcje:** paczka apt (brak w 24.04), snap, prebuilt z GitHuba, build lokalny.
- **Decyzja:** build lokalny, `GGML_NATIVE=ON`, tag przypięty w `install.sh`.
- **Zarzut:** build trwa kilka minut i wymaga `cmake`.
- **Odpowiedź:** jednorazowo. W zamian dostajemy optymalizację pod AVX2 tej maszyny i powtarzalność benchmarku.

## ADR-016 🧪 Jeden model dla PTT i continuous

- **Decyzja:** jeden `stt.model`. Drugi serwer dla continuous powstaje tylko wtedy, gdy spełniona jest reguła z [13](13-benchmark.md) §13.5.
- **Zarzut:** PTT toleruje wolniejszy i lepszy model, a continuous potrzebuje szybszego.
- **Odpowiedź:** prawda, ale drugi model to +0,2–0,6 GB RAM i drugi proces. Najpierw mierzymy, czy różnica jest realna.

## ADR-017 ✅ Filtrowanie halucynacji po stronie daemona

- **Kontekst:** Whisper na ciszy lub szumie generuje m.in. „Napisy stworzone przez społeczność Amara.org” (potwierdzone w openai/whisper#928).
- **Decyzja:**
  - bramka ciszy przed wysłaniem (RMS w v0.1, VAD w v0.2),
  - `-sns` w serwerze,
  - filtry `no_speech_prob` + `avg_logprob`,
  - lista wzorców (konfigurowalna),
  - detekcja pętli powtórzeń.

  Szczegóły: [06](06-silnik-stt.md) §6.8.
- **Zarzut:** wzorzec może wyciąć prawdziwą wypowiedź.
- **Odpowiedź:** wzorce niepotwierdzone dopasowują tylko **cały** segment (`^…$`), a każde odrzucenie jest logowane (DEBUG).
