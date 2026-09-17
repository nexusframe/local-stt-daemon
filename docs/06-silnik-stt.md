# 06. Silnik STT — whisper.cpp

Stan wiedzy: whisper.cpp **v1.9.4** (2026-09-11). Fakty poniżej zweryfikowano w kodzie źródłowym `examples/server/server.cpp` z tego tagu, który w kilku miejscach różni się od nieaktualnego README serwera.

## 6.1 Dlaczego osobny proces `whisper-server`

Rozważane opcje ([03](03-decyzje.md), ADR-002):

| Opcja | Za | Przeciw |
|---|---|---|
| **`whisper-server` (HTTP, localhost)** ✅ | natywny build (AVX2, `GGML_NATIVE`), model w RAM między żądaniami, zmiana modelu = restart usługi, crash silnika nie zabija daemona, ten sam binarny plik do benchmarku, zero bindingów C w Pythonie | dodatkowa usługa systemd, narzut HTTP (~ms, pomijalny) |
| `pywhispercpp` / własny ctypes | jeden proces | kompilacja przez pip, wheel może nie mieć natywnych flag, segfault w C zabija daemona, trudniejsza wymiana silnika |
| `whisper-cli` per nagranie | najprostsze | ładuje model przy każdym wywołaniu (0,3–1 s + zimny cache) |
| faster-whisper (CTranslate2) | szybki na CPU | Python + CTranslate2 + modele HF (~GB zależności), poza wymaganiem „whisper.cpp” — zostaje jako kandydat na drugi `SttEngine` |

## 6.2 Budowa

```bash
sudo apt install build-essential cmake git
git clone --depth 1 --branch v1.9.4 https://github.com/ggml-org/whisper.cpp ~/.local/share/local-stt/src/whisper.cpp
cd ~/.local/share/local-stt/src/whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF
cmake --build build -j4 --config Release --target whisper-server whisper-cli whisper-bench
install -m755 build/bin/whisper-server build/bin/whisper-cli build/bin/whisper-bench ~/.local/share/local-stt/bin/
```

- `GGML_NATIVE` jest domyślnie ON, więc build od razu wykorzystuje AVX2/FMA tej maszyny. **Binarka nie jest przenośna na starsze CPU**, co jest akceptowalne, bo budujemy lokalnie.
- Flash attention jest domyślnie **włączone** (`-fa`) w serwerze, CLI i bibliotece.
- Wersja jest przypięta tagiem, a `install.sh --whisper-tag TAG --rebuild-whisper` przebudowuje ją świadomie. `whisper-server` nie ma flagi `--version`, więc po buildzie zapisujemy tag do `bin/.whisper-tag` i porównujemy z tym plikiem.
- **OpenBLAS** (`-DGGML_BLAS=1 -DGGML_BLAS_VENDOR=OpenBLAS`, pakiet `libopenblas-dev`) jest opcją benchmarkową. Domyślnie go nie włączamy, dopóki pomiar nie pokaże zysku ([13](13-benchmark.md)).
- Nie budujemy wsparcia FFmpeg. Serwer bez `--convert` czyta WAV bezpośrednio z przesłanych bajtów.

Pliki binarne: `whisper-server`, `whisper-cli`, `whisper-bench` (plus `quantize`, jeśli kiedyś będzie potrzebny).

## 6.3 Modele

Pobieranie: `local-stt models pull <nazwa>` ściąga `ggml-<nazwa>.bin` z `https://huggingface.co/ggerganov/whisper.cpp/resolve/main/` (to samo źródło co `models/download-ggml-model.sh`) i weryfikuje sumę SHA256 z `scripts/models.sha256`. Plik z nieprawidłową sumą jest usuwany.

| Model (plik `ggml-<nazwa>.bin`) | Rozmiar | WER PL FLEURS / CV9 (paper Whisper) | Rola |
|---|---:|---|---|
| `base-q5_1` | 57 MiB | 30,8 / 32,8 (base) | tylko test/fallback — **za słaby dla polskiego** |
| `small-q5_1` | 181 MiB | 14,7 / 16,9 (small) | **domyślny start** |
| `small-q8_0` | 252 MiB | jw. | kandydat benchmarku |
| `small` (f16) | 465 MiB | jw. | punkt odniesienia jakości small |
| `medium-q5_0` | 514 MiB | 8,0 / 10,1 (medium) | kandydat PTT, jeśli latencja pozwoli |
| `large-v3-turbo-q5_0` | 547 MiB | brak liczb w paperze, na wykresach OpenAI lepszy od medium | kandydat PTT z `audio_ctx` |

Uwagi:

- Kwantyzacja q5/q8 zmniejsza RAM i przyspiesza ładowanie. Wpływ na WER mierzymy sami ([13](13-benchmark.md)).
- Projekt wstępny zakładał `base` jako główny kandydat „responsywności”. Liczby z paperu dla polskiego (≈31% WER, czyli co trzecie słowo źle) go dyskwalifikują. `base` zostaje tylko do testów.
- **Wybór modelu jest decyzją pomiarową.** Na starcie domyślny jest `small-q5_1`, a ostateczny default ustala [13-benchmark.md](13-benchmark.md). Obowiązuje jeden model dla obu trybów. Drugi model powstaje tylko wtedy, gdy wymaga go reguła z §13.5 (patrz 6.7).

## 6.4 Uruchomienie serwera

`local-stt.service` nie startuje serwera. Robi to jednostka `local-stt-whisper.service` ([11](11-daemon-systemd-instalacja.md) §11.4), której argumenty pochodzą z pliku `whisper-server.env`. Ten plik jest generowany z configu ([09](09-konfiguracja.md) §9.4). Rozwinięte polecenie:

```bash
~/.local/share/local-stt/bin/whisper-server \
  --host 127.0.0.1 --port 8178 \
  --request-path /<sekret: 32 znaki hex> \
  -m ~/.local/share/local-stt/models/ggml-small-q5_1.bin \
  -l pl -t 4 -bs -1 -sns
```

| Flaga | Wartość | Uzasadnienie |
|---|---|---|
| `--host 127.0.0.1` | wymuszone (nie konfigurowalne) | prywatność (N5) |
| `--port 8178` | `stt.port`, nietypowy, by nie kolidować z domyślnym 8080 | |
| `--request-path /<sekret>` | losowy prefiks wszystkich endpointów (`/…/inference`, `/…/health`, `/…/load`), generowany przez `install.sh` do `~/.config/local-stt/secret` (0600) | ochrona przed CSRF: strona WWW w przeglądarce nie zna ścieżki, więc nie zmieni modelu ani nie zablokuje serwera (12 §12.2) |
| `-l pl` | **obowiązkowe** — domyślnie serwer ma `en` | |
| `-t 4` | `stt.threads`; domyślnie liczba rdzeni fizycznych, benchmark 4 vs 8 | HT daje mało (whisper.cpp#89) |
| `-bs -1` (greedy) | `stt.beam_size` | beam search ×2–5 wolniej, benchmark zdecyduje |
| `-sns` (`suppress_nst`) | zawsze | tłumi tokeny nie-mowy (`[muzyka]` itp.) |
| brak `--vad` | VAD robimy w daemonie przed wysłaniem | serwer dostaje już mowę |
| brak `--convert` | wysyłamy gotowy WAV 16 kHz mono s16 | bez ffmpeg |

Serwer przetwarza **jedno żądanie naraz** (`std::mutex`), co jest zgodne z jednym `PipelineWorker`. Rozłączenie klienta w trakcie przerywa dekodowanie (HTTP 499). Nie korzystamy z tego do anulowania, ale nie szkodzi.

## 6.5 Kontrakt HTTP (to, czego używamy)

### `GET /health`

- `200 {"status":"ok"}` → `engine=READY`
- `503 {"status":"loading model"}` → `engine=STARTING`
- błąd połączenia → `STARTING` albo `DOWN` według reguły z [04](04-maszyna-stanow.md) §4.5

Wszystkie ścieżki mają prefiks `--request-path`. Klient czyta sekret z `~/.config/local-stt/secret`.

### `POST /inference` (multipart/form-data)

| Pole | Wartość | Uwagi |
|---|---|---|
| `file` | WAV RIFF PCM s16le, 16000 Hz, mono | budowany w pamięci (`io.BytesIO` + `wave`) |
| `response_format` | `verbose_json` | segmenty z `no_speech_prob`, `avg_logprob` |
| `language` | `pl` | redundantnie z `-l`, by nie zależeć od flag serwera |
| `no_language_probabilities` | `true` | **inaczej serwer robi drugi przebieg enkodera** |
| `temperature` / `temperature_inc` | `0.0` / `0.2` | fallback temperaturowy przy wysokiej entropii |
| `prompt` | słownik + kontekst (6.6) | pomijane, gdy puste |
| `no_timestamps` | `false` | potrzebujemy granic segmentów do filtrowania |
| `audio_ctx` | `0` lub wyliczony (6.7) | |

Odpowiedź (fragment, który czytamy):

```json
{
  "text": " Dzisiaj chciałbym pojechać do Warszawy.",
  "duration": 3.2,
  "segments": [
    {"id": 0, "text": " Dzisiaj chciałbym pojechać do Warszawy.",
     "start": 0.0, "end": 3.1, "avg_logprob": -0.21, "no_speech_prob": 0.02}
  ]
}
```

- `avg_logprob` jest liczone z uwzględnieniem tokenów specjalnych, więc jest obciążone. Traktujemy je jako sygnał względny z progiem konfigurowalnym, nie jako wartość bezwzględną.
- Pole `temperature` w odpowiedzi odbija wartość z żądania, a nie temperaturę faktycznie użytą, więc go nie używamy.
- `compression_ratio` nie istnieje. Detekcję pętli halucynacji robimy sami (6.8).

Timeout żądania: `max(10 s, 4 × długość_audio × RTF_z_ostatnich_10_zadań)`, ale nie więcej niż `stt.request_timeout_max_s` (120 s).

### `POST /load` — świadomie **nieużywane**

Zweryfikowano w `examples/server/server.cpp` z v1.9.4:

- przy nieistniejącym pliku handler zwraca 400, ale **zostawia stan `LOADING_MODEL`**, więc `/health` odpowiada 503 do restartu,
- przy nieudanym ładowaniu modelu proces kończy się (`exit(1)`),
- ładowanie blokuje mutex inferencji.

Zmianę modelu robimy więc zawsze przez restart usługi z nowym `whisper-server.env` ([04](04-maszyna-stanow.md) §4.6). To jedna ścieżka, a po restarcie serwer ma zawsze model zgodny z configiem. Koszt to ponowne załadowanie modelu (sekundy) — tyle samo co `/load`.

## 6.6 Prompt (słownik i kontekst)

Pole `prompt` = `initial_prompt` Whispera (limit modelu ~224 tokeny). Walidator ogranicza `stt.vocabulary_prompt` do 300 znaków, a kontekst ma maks. 200 znaków, więc całość się mieści i nic nie jest obcinane:

```text
<stt.vocabulary_prompt>  +  " "  +  <ostatnie ≤200 znaków wpisanego tekstu z tej samej sesji continuous>
```

- `stt.vocabulary_prompt` to np. `"Kubernetes, PipeWire, whisper.cpp, Gdańsk."`. Nazwy własne w prompcie wyraźnie poprawiają ich pisownię.
- W PTT kontekst to tylko słownik, bo każde nagranie jest niezależne.
- W continuous kontekst ciągnie interpunkcję i wielkie litery między fragmentami. Ryzyko: halucynacja powtarza prompt. Chroni przed tym filtr z 6.8 (odrzucamy wynik identyczny z końcówką promptu).

## 6.7 `audio_ctx` — główna dźwignia latencji

Enkoder Whispera zawsze przetwarza **okno 30 s** (1500 ramek, 50 ramek/s), nawet gdy nagranie ma 3 s. Na CPU enkoder to dominujący koszt. Parametr `audio_ctx` skraca to okno:

```text
audio_ctx = min(1500, ceil(duration_s * 50) + stt.audio_ctx_margin)    # margin domyślnie 128 (~2,5 s)
```

- Zysk dla 5-sekundowej wypowiedzi to kilkukrotnie krótszy enkoder.
- Ryzyko: model trenowano na pełnym oknie. Przy mocno skróconym kontekście jakość może spaść lub pojawią się powtórzenia. Dlatego `stt.dynamic_audio_ctx` (`true`/`false`) jest przełącznikiem, a benchmark porównuje WER i latencję dla każdego modelu ([13](13-benchmark.md)). Wartość domyślna do czasu benchmarku to `false`.

### Osobne modele dla PTT i continuous?

`whisper-server` trzyma jeden model. Obsługa dwóch modeli oznaczałaby dwa serwery (2× RAM) albo restart serwera z innym modelem przy każdej zmianie trybu (koszt kilku sekund). Decyzja: **jeden model**, ustawiany w `stt.model`. Opcja `stt.continuous_model` (drugi serwer na porcie `8179`, uruchamiany tylko przy włączonym continuous) jest opisana jako rozszerzenie v0.3 w [15](15-plan-implementacji.md) i powstanie tylko wtedy, gdy benchmark pokaże, że jeden model nie spełnia jednocześnie N2 i N3.

## 6.8 Wynik → `Transcript`

`WhisperServerEngine.transcribe()` zwraca strukturę niezależną od silnika:

```python
@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start_s: float
    end_s: float
    no_speech_prob: float | None
    avg_logprob: float | None

@dataclass(frozen=True)
class Transcript:
    text: str
    segments: list[TranscriptSegment]
    audio_duration_s: float
    processing_s: float          # zmierzone po stronie klienta
    engine: str                  # "whisper.cpp"
    model: str                   # "small-q5_1"
```

Filtrowanie treści robi `TextProcessor`, a nie silnik ([08](08-text-injection.md) §8.2):

1. odrzucenie segmentu, gdy `no_speech_prob > stt.no_speech_threshold` (0,6) **i** `avg_logprob < stt.logprob_threshold` (-1,0),
2. odrzucenie segmentu, dla którego `re.search(pattern, text, re.IGNORECASE)` znajduje dopasowanie któregokolwiek wzorca z `text.hallucination_patterns` (kotwice `^…$` są częścią wzorca) — domyślna lista:
   - `napisy (stworzone|wykonane) przez społeczność amara\.org` — **potwierdzone** (openai/whisper#928)
   - `(zdjęcia|tłumaczenie) i napisy stworzone przez społeczność amara\.org` — potwierdzone (#928)
   - `^\s*dzięk(i|uję) za (uwagę|obejrzenie|oglądanie)[.!]?\s*$` — **niepotwierdzone**, dodane z analogii do angielskiego „Thanks for watching”. Wpis dopasowuje wyłącznie cały segment, żeby nie wycinać tych słów z normalnej wypowiedzi.
   - `^\s*(subskrybuj|zasubskrybuj)[^.]*[.!]?\s*$` — niepotwierdzone, jw.
3. odrzucenie powtórzeń: segment identyczny z poprzednim w tym samym wyniku albo n-gram (n ≥ 3 słowa) powtórzony ≥ 4 razy z rzędu (pętla dekodera),
4. odrzucenie całego wyniku, jeśli jest równy (po normalizacji) końcówce przekazanego promptu.

Każde odrzucenie jest logowane na DEBUG jako `filtered: <reason>`. Treść trafia do logu tylko przy `logging.log_text = true` ([12](12-logi-prywatnosc-bledy.md)).

## 6.9 Interfejs silnika (wymienność — N7)

```python
class SttEngine(Protocol):
    name: str
    def health(self) -> EngineHealth: ...                         # READY / STARTING / DOWN
    def transcribe(self, audio: np.ndarray, *, sample_rate: int,  # float32 mono [-1, 1]
                   language: str, prompt: str | None,
                   timeout_s: float) -> Transcript: ...
```

Implementacje:

- `WhisperServerEngine` (v0.1)
- `FakeEngine` (testy: zwraca zaprogramowane teksty z opóźnieniem)

Wybór implementacji: `stt.engine = "whisper-server"`. Nowy silnik (np. faster-whisper w osobnym procesie) dodajemy jako nową klasę plus wpis w rejestrze `local_stt/stt/__init__.py`, bez zmian w reszcie kodu.
