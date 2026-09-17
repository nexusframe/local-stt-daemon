# 13. Benchmark i wybór modelu

Benchmark to **pierwszy krok implementacji** ([15](15-plan-implementacji.md), etap 0). Jego wyniki wyznaczają domyślny `stt.model`, `stt.threads` i `stt.dynamic_audio_ctx`. Dopóki nie istnieją, obowiązuje `small-q5_1`, `threads=4`, `dynamic_audio_ctx=false`.

## 13.1 Pytania, na które odpowiada

1. Który model daje najniższy WER dla polskiego przy p90 latencji PTT ≤ 2,5 s dla wypowiedzi 4–10 s (N2)?
2. Czy ten sam model utrzyma continuous przez 10 minut bez rosnącej kolejki (N3), także po nagrzaniu CPU?
3. 4 czy 8 wątków?
4. Czy `audio_ctx` dopasowany do długości nagrania skraca latencję bez utraty jakości?
5. Czy OpenBLAS coś daje (opcjonalnie, osobny build)?

## 13.2 Korpus

### A. Własne nagrania (podstawowy — ten mikrofon, ten głos, to otoczenie)

`local-stt record-corpus ~/stt-corpus`:

- wyświetla zdania z `bench/prompts_pl.txt`, nagrywa każde z nich po naciśnięciu Enter (Enter kończy) i zapisuje `NNN.wav` (16 kHz mono s16) + `NNN.txt` (tekst referencyjny),
- pozwala powtórzyć nagranie (`r`) lub pominąć zdanie (`s`).

`bench/prompts_pl.txt` (część repozytorium) zawiera ok. 40 wypowiedzi:

| Grupa | Liczba | Długość | Cel |
|---|---:|---|---|
| krótkie komendy/zdania | 12 | 1–3 s | PTT, typowe krótkie notatki |
| średnie | 16 | 4–10 s | główny przypadek N2 |
| długie | 8 | 12–25 s | granice `max_segment_s`, `audio_ctx` |
| trudne | 4 | 5–10 s | nazwy własne, liczby, anglicyzmy techniczne, nagromadzenie ą/ę/ł/ż/ź/ś/ć/ń |

Do tego **nagranie ciągłe** `long/` (`record-corpus --long`): ok. 5 min czytanego tekstu z naturalnymi pauzami (np. artykuł z polskiej Wikipedii zapisany jako referencja) do testu continuous.

### B. FLEURS pl_pl (opcjonalny — porównywalność z paperem)

`local-stt bench --dataset DIR`. Katalog w formacie jak A. Konwersję 50 losowych próbek ze zbioru testowego FLEURS (licencja CC-BY-4.0) robi `scripts/fleurs_to_corpus.py`. Pobranie danych jest ręczne i online, poza daemonem.

## 13.3 Metryki

| Metryka | Definicja | Źródło |
|---|---|---|
| **WER** | odległość Levenshteina na słowach / liczba słów referencji, po normalizacji | `local_stt/bench/wer.py` (bez zależności) |
| **CER** | jw. na znakach | jw. |
| Normalizacja | małe litery, usunięcie interpunkcji `.,;:!?…„”"'()-–—`, zwinięcie spacji; **polskie znaki bez zmian** (ich brak to błąd); liczby bez normalizacji — rozbieżności `2024`/„dwa tysiące…” raportowane osobno jako `numeric_mismatch` | |
| **RTF** | `processing_s / audio_s` | czas HTTP mierzony po stronie klienta |
| **Latencja PTT** | czas od „puszczenia” do gotowego tekstu = `vad_trim + http + text_processing` (bez wklejania, które jest stałe ~0,1 s i mierzone osobno) | |
| p50 / p90 | percentyle latencji dla grupy „średnie” | |
| CPU | `utime+stime` procesu serwera z `/proc/<pid>/stat` / czas ścienny | |
| Peak RAM | `VmHWM` z `/proc/<pid>/status` | |
| Termika | `/sys/class/thermal/thermal_zone*/temp` (strefa `x86_pkg_temp`), `scaling_cur_freq` wszystkich CPU, próbkowane co 1 s | |
| Kolejka | `queued_audio_s` w czasie (soak) | |
| Segmentacja | liczba segmentów, % cięć `max_length`, liczba segmentów odfiltrowanych | |
| **Błędne segmentacje** | cięcie wypadające **wewnątrz słowa** referencji: dla nagrania `long/` referencyjne granice słów wyznaczamy jednorazowo `whisper-cli -m large-v3-turbo-q5_0 -ml 1 -ojf` (znaczniki czasu słów, ręcznie przejrzane i zapisane jako `long/NNN.words.json`); segmentacja jest błędna, gdy punkt cięcia leży > 40 ms od najbliższej granicy słowa. Raportujemy liczbę i % cięć | soak |

## 13.4 Macierz

`local-stt bench` sam uruchamia **tymczasowy** `whisper-server` na porcie 8199 dla każdej konfiguracji: `nice -n 5` (jak `Nice=5` w unicie), losowy `--request-path`, start → `/health` → rozgrzewka (1 żądanie odrzucane) → pomiary → stop.

Przed startem sprawdza, czy `local-stt-whisper.service` jest aktywny. Jeśli tak, odmawia z komunikatem `systemctl --user stop local-stt-whisper local-stt` (flaga `--allow-concurrent` pomija ten test), bo dwa serwery zafałszowałyby CPU i RAM.

| Wymiar | Wartości |
|---|---|
| model | `base-q5_1`, `small-q5_1`, `small-q8_0`, `small`, `medium-q5_0`, `large-v3-turbo-q5_0` |
| threads | 4, 8 |
| dynamic_audio_ctx | false, true |
| beam | greedy (wszystkie); `-bs 5` tylko dla 2 najlepszych po etapie 1 |

Kolejność jest oszczędna, bo pełna macierz na tym CPU zajęłaby godziny:

1. **Etap 1 — `bench --quick`.** Wszystkie modele, `t=4`, `dynamic_audio_ctx=false`, tylko grupa „średnie” (16 plików). Modele z p50 latencji > 6 s odpadają od razu.
2. **Etap 2.** Pozostałe modele × `t∈{4,8}` × `dynamic_audio_ctx∈{false,true}` na całym korpusie A.
3. **Etap 3 — `bench --soak --model M --threads T --audio-ctx X`.** Dla 1–2 zwycięzców: nagranie `long/` odtwarzane przez prawdziwy `Segmenter` w tempie czasu rzeczywistego, zapętlone do 10 minut, z pomiarem kolejki, RTF i termiki. Wykonać na zasilaczu **i** na baterii (governor `powersave`).
4. **Sanity.** `whisper-bench -m <model> -t 4` dla każdego modelu (surowy czas enkodera), żeby odróżnić narzut HTTP i pipeline'u od samego silnika.

Każda odpowiedź jest greedy i deterministyczna, więc WER liczymy z jednego przebiegu. Latencję podajemy jako medianę z 3 powtórzeń.

## 13.5 Reguła decyzji

```text
kandydaci_ptt = { konfiguracje z p90_latencji(średnie) ≤ 2.4 s }     # N2 = 2.5 s z wklejaniem (~0.1 s)
PTT_default   = argmin WER(korpus A) po kandydaci_ptt
                remis (różnica WER < 1 pp) → niższy peak RAM → niższa latencja

dynamic_audio_ctx = true  tylko jeśli dla wybranego modelu WER(true) − WER(false) ≤ 1.0 pp
threads           = wartość z niższą p50 latencji; remis (< 5%) → 4 (mniej grzania, mniej walki z pulpitem)

continuous: PTT_default przechodzi, jeśli w soak (na baterii):
    średni RTF ≤ 0.5  ORAZ  queued_audio_s w ostatnich 5 min nie ma trendu rosnącego
    (nachylenie regresji liniowej ≤ 0.05 s/min)  ORAZ  brak spadku częstotliwości CPU > 30% trwale
jeśli nie przechodzi → sprawdź następny szybszy model; jeśli taki przechodzi, a różnica WER > 3 pp,
    to dopiero wtedy implementujemy stt.continuous_model (15, v0.3); w przeciwnym razie wspólny szybszy model.
```

Jeśli żaden model nie spełnia N2 (np. nawet `small-q5_1` ma p90 > 2,5 s), celem nie jest zmiana wymagań po cichu. Raport zawiera wtedy **najlepszy dostępny kompromis i jawną rekomendację zmiany N2** do akceptacji.

## 13.6 Wyniki

- Surowe: `~/.local/share/local-stt/bench/<ISO-timestamp>/results.jsonl` (jedna linia = jeden plik × konfiguracja) + `system.json` (CPU, governor, zasilanie, wersja whisper.cpp, kernel).
- Raport: `local-stt bench report DIR` generuje Markdown z tabelami. Wynik, który ustala defaulty, kopiujemy do `docs/benchmark-results.md` w repozytorium razem z datą i uzasadnieniem wyboru.
- Transkrypcje korpusu zapisywane w wynikach to treść nagrana przez użytkownika na potrzeby testu, zgodnie z zasadą z [12](12-logi-prywatnosc-bledy.md) §12.2.

## 13.7 Hipotezy do weryfikacji (nie fakty)

- Z tabel paperu Whisper dla polskiego (FLEURS): base 30,8%, small 14,7%, medium 8,0% WER. Na własnym mikrofonie i przy mowie swobodnej WER będzie wyższy, ale ranking modeli powinien się utrzymać.
- Publiczne wyniki `whisper-bench` z 4-rdzeniowych laptopowych CPU tej generacji (issue ggml-org/whisper.cpp#89) mają duży rozrzut (np. i7-8750H: enkoder small ~4,2 s, medium ~13 s na 4 wątkach; 8 wątków nie przyspieszało). Na i5-8365U (U-series, niższe TDP) spodziewamy się wolniej, więc **`medium` i `large-v3-turbo` bez `audio_ctx` prawdopodobnie nie zmieszczą się w N2**. Dlatego etap 2 testuje `audio_ctx`.
- Kwantyzacja q5_1 vs f16 dla `small`: oczekiwany mały wpływ na WER. Do zmierzenia.
