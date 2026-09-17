# 09. Konfiguracja

## 9.1 Format i lokalizacja

- Format: **TOML**, parsowany przez `tomllib` z biblioteki standardowej Pythona 3.12. Nie potrzeba zależności, a składnia jest jednoznaczna (w YAML `no`/`on` bywają interpretowane jako bool). Projekt wstępny dopuszczał zmianę formatu. Uzasadnienie: [03](03-decyzje.md), ADR-008.
- Plik: `$XDG_CONFIG_HOME/local-stt/config.toml` (domyślnie `~/.config/local-stt/config.toml`). Ścieżkę można nadpisać flagą `--config PATH` lub zmienną `LOCAL_STT_CONFIG`.
- Brak pliku oznacza pracę na wartościach domyślnych. `install.sh` kopiuje `config.example.toml` z pełnym komentarzem.
- Walidacja: dataclasses + ręczne walidatory w `local_stt/config.py`. Każdy błąd ma format `<sekcja>.<klucz>: <problem> (got <wartość>)`, np. `vad.end_threshold: must be < start_threshold (got 0.6)`. Nieznany klucz daje **błąd**, a nie ostrzeżenie, żeby literówki nie przechodziły po cichu.
- Zmiany wchodzą w życie po `local-stt reload` według grup z [04](04-maszyna-stanow.md) §4.6. Klucze oznaczone ⟳ powodują, że daemon sam generuje nowy `whisper-server.env` i restartuje `local-stt-whisper.service`, gdy tryb to IDLE, a kolejka jest pusta.

## 9.2 Pełny plik z wartościami domyślnymi

```toml
# ~/.config/local-stt/config.toml

[stt]
engine = "whisper-server"            # jedyna implementacja w v0.1–v0.3
port = 8178                          # ⟳
model = "small-q5_1"                 # ⟳ nazwa pliku ggml-<model>.bin w models_dir
models_dir = "~/.local/share/local-stt/models"   # ⟳
language = "pl"                      # ⟳
threads = 4                          # ⟳ -t dla whisper-server
beam_size = -1                       # ⟳ -1 = greedy
vocabulary_prompt = ""               # np. "Kubernetes, PipeWire, Gdańsk."
continuous_context = true            # doklejaj końcówkę poprzedniego fragmentu do promptu
dynamic_audio_ctx = false            # patrz 06 §6.7 – ustawić wg benchmarku
audio_ctx_margin = 128
no_speech_threshold = 0.6
logprob_threshold = -1.0
startup_timeout_s = 60
request_timeout_max_s = 120
extra_server_args = []               # ⟳ np. ["-nf"]; flagi zarządzane przez local-stt są odrzucane (9.3)

[audio]
device = "default"                   # "default" albo nazwa węzła PipeWire z `local-stt devices`

[ptt]
min_duration_ms = 300
max_duration_s = 120
silence_rms_dbfs = -50.0

[continuous]
max_backlog_s = 60                   # suma audio w kolejce, po której tryb się wyłącza

[vad]
enabled = true                       # v0.2: false → PTT z bramką RMS zamiast VAD; continuous odmawia (vad_disabled)
model = "silero_vad.onnx"            # w models_dir
start_threshold = 0.50
end_threshold = 0.35
min_speech_ms = 250
min_silence_ms = 700
speech_pad_ms = 300
max_segment_s = 15
split_search_s = 3

[hotkeys]
enabled = true
push_to_talk = "Control_R"
continuous_toggle = "Shift+Control_R"
ptt_cancel_key = "Escape"            # pojedynczy keysym; działa tylko przy trzymanym PTT

[text]
append_space = true
hallucination_patterns = [
  'napisy (stworzone|wykonane) przez społeczność amara\.org',
  '(zdjęcia|tłumaczenie) i napisy stworzone przez społeczność amara\.org',
  '^\s*dzięk(i|uję) za (uwagę|obejrzenie|oglądanie)[.!]?\s*$',
  '^\s*(za)?subskrybuj[^.]*[.!]?\s*$',
]
# [[text.replacements]]
# pattern = '(?i)\bnowa linia\b'
# replace = "\n"
# regex = true

[injection]
backend = "auto"                     # auto | clipboard | type
restore_clipboard = true
modifier_wait_ms = 1000
paste_timeout_ms = 1000
type_delay_ms = 12
type_window_classes = ["xterm", "URxvt"]
terminal_window_classes = [
  "gnome-terminal-server", "kitty", "alacritty", "konsole", "tilix",
  "org.wezfurlong.wezterm", "terminator", "xfce4-terminal", "guake", "tabby", "ghostty",
]
[injection.paste_shortcut_overrides]
# "emacs" = "Ctrl+Y"

[feedback]
sounds = true                        # dźwięki start/stop/cancel/error
sound_volume = 0.4                   # 0.0–1.0 (skalowanie generowanych WAV)
notifications = "errors"             # none | errors | all
                                     # all = też „Dyktowanie włączone/wyłączone”, „Silnik gotowy” (10 §10.6)

[logging]
level = "INFO"                       # INFO | DEBUG | TRACE
log_text = false                     # true = pełny tekst transkrypcji w logach (tylko do debugowania)
timings = true                       # linia z czasami każdego zadania (bez treści)
```

## 9.3 Walidacje międzypolowe

| Reguła | Komunikat |
|---|---|
| `vad.end_threshold < vad.start_threshold` | `vad.end_threshold: must be < start_threshold` |
| `0 < thresholds < 1` | `…: must be in (0, 1)` |
| `vad.split_search_s < vad.max_segment_s` | |
| `vad.max_segment_s ≤ 28` | margines pod okno 30 s Whispera |
| plik `models_dir/ggml-<model>.bin` istnieje | `stt.model: file not found: … (run: local-stt models pull small-q5_1)` |
| gdy `vad.enabled`: plik `models_dir/<vad.model>` istnieje | `vad.model: file not found: … (run: local-stt models pull silero-vad)` |
| `len(stt.vocabulary_prompt) ≤ 300` | limit promptu Whispera ([06](06-silnik-stt.md) §6.6) |
| `hotkeys.push_to_talk != hotkeys.continuous_toggle` | |
| hotkey nie używa `ISO_Level3_Shift`, `Alt_R`, `Super_L` (konflikt) ani `Control_L`, `Shift_L` (używane przez XTest przy wklejaniu, [08](08-text-injection.md) §8.5) | [07](07-hotkeys-x11.md) §7.2 |
| `hotkeys.ptt_cancel_key` to jeden keysym bez modyfikatorów, różny od keysymów PTT i continuous | |
| `stt.extra_server_args` nie zawiera `--host`, `--port`, `--request-path`, `--inference-path`, `-m`, `-l`, `-t`, `-bs`, `--public`, `--convert` | `stt.extra_server_args: flag X is managed by local-stt` |
| `continuous` wymaga `vad.enabled` | ostrzeżenie `vad.enabled=false: continuous dictation unavailable`; `toggle` zwraca `vad_disabled` |
| regexy w `text.*` się kompilują | `text.hallucination_patterns[2]: invalid regex: …` |

## 9.4 Pliki generowane dla `whisper-server`

`install.sh` tworzy jednorazowo `~/.config/local-stt/secret` (uprawnienia 0600): 32 znaki hex z `secrets.token_hex(16)`. Wartość służy jako `--request-path` serwera ([06](06-silnik-stt.md) §6.4).

Z sekcji `[stt]` i sekretu daemon (także `install.sh` i `local-stt reload`) generuje `~/.config/local-stt/whisper-server.env` (0600):

```sh
# wygenerowane przez local-stt – nie edytuj ręcznie
LOCAL_STT_WHISPER_ARGS="--host 127.0.0.1 --port 8178 --request-path /3f9c…e1 -m /home/leto/.local/share/local-stt/models/ggml-small-q5_1.bin -l pl -t 4 -bs -1 -sns"
```

Jednostka `local-stt-whisper.service` wczytuje go przez `EnvironmentFile=` ([11](11-daemon-systemd-instalacja.md) §11.4). Config pozostaje jedynym źródłem prawdy, a host jest zawsze `127.0.0.1` i nie jest konfigurowalny.
