# 12. Logowanie, prywatność i obsługa błędów

## 12.1 Logowanie

- Mechanizm: moduł `logging` z biblioteki standardowej. Wyjście to stderr, a pod systemd trafia ono do journald.
- Poziomy: `INFO` (20), `DEBUG` (10) i własny `TRACE` (5). Źródła poziomu w kolejności ważności: `--log-level`, `LOCAL_STT_LOG_LEVEL`, `logging.level`.
- Pod systemd, rozpoznawanym po zmiennej `JOURNAL_STREAM`, format to `<PRI>logger: message`, gdzie `PRI` to priorytet sd-daemon (`<3>` error, `<4>` warning, `<6>` info, `<7>` debug/trace). Znacznik czasu dodaje journald, a `journalctl -p warning` filtruje poprawnie. Poza systemd format to `2026-09-17 10:21:03.412 INFO  logger: message`.
- Nazwy loggerów: `local_stt.controller`, `.audio`, `.vad`, `.stt`, `.text`, `.inject`, `.hotkeys`, `.ipc`.

### Co logujemy na którym poziomie

| Poziom | Przykłady |
|---|---|
| INFO | start/stop, wersje (daemon, whisper.cpp, model), urządzenie audio, zmiany `engine`, zmiany `mode`, **linia czasów zadania**, reload, ostrzeżenia z `doctor`-owych testów przy starcie |
| DEBUG | zdarzenia Controllera i przejścia, `VAD speech started/ended (speech_ms, cut)`, powód odrzucenia nagrania, `filtered: <reason>`, wybór backendu i `WM_CLASS`, czasy HTTP |
| TRACE | co 1 s: średnie/maks. `p` VAD i RMS dBFS; każde zdarzenie X11; surowe nagłówki odpowiedzi HTTP (bez body) |

Linia czasów zadania (`logging.timings`):

```text
job=17 src=continuous seq=4 audio=3.84s queued=0.21s stt=1.62s rtf=0.42 text=2ms inject=84ms total=1.95s chars=62 backend=clipboard result=injected
```

Znaczenie pól:

- `queued` — czas oczekiwania w kolejce,
- `total` — czas od końca wypowiedzi (puszczenie PTT albo koniec ciszy VAD) do końca wpisywania. To metryka N2 z perspektywy użytkownika.

### Czego nie logujemy nigdy

- Surowego audio, ani w logach, ani w plikach.
- Treści transkrypcji, chyba że `logging.log_text = true`. Wtedy pojawia się dodatkowa linia DEBUG `text job=17: "…"`, a przy starcie jednorazowe WARNING `log_text is enabled — transcripts will be stored in the journal`.
- Treści schowka (zapisanej i przywracanej).
- Pola `prompt` (może zawierać poprzedni dyktowany tekst).

## 12.2 Prywatność

Zasada: **audio i tekst nie opuszczają komputera, a na dysk trafiają tylko na wyraźne polecenie użytkownika.**

| Obszar | Gwarancja | Jak egzekwowana |
|---|---|---|
| Sieć w runtime | jedyne połączenia procesu daemona to `127.0.0.1:<stt.port>` (HTTP do serwera) i gniazdo Unix IPC | host na sztywno `127.0.0.1` (niekonfigurowalny); moduły importowane przez daemon używają tylko `http.client` do loopbacku — kod sieciowy do Internetu (`models.py`: `urllib` do HF) jest w module ładowanym wyłącznie przez polecenie `models pull`, co sprawdza test importów; `doctor` sprawdza nasłuch serwera |
| Internet | potrzebny wyłącznie w `install.sh` i `local-stt models pull` | daemon nie ma żadnej ścieżki kodu łączącej się z Internetem; test e2e uruchamia daemon i serwer w osobnej przestrzeni sieciowej z samym loopbackiem ([14](14-testy.md) §14.3) |
| Dysk | brak zapisu audio i tekstu; wyjątki: `record-corpus` (katalog wskazany przez użytkownika), `bench` (wyniki liczbowe + transkrypcje korpusu), `log_text=true` | przegląd kodu; `tmp` nieużywany (WAV w `BytesIO`) |
| Zrzuty pamięci | audio w RAM nie trafia do core dumpów | `LimitCORE=0` w obu unitach |
| Inni użytkownicy lokalni | nie mogą sterować daemonem | gniazdo `0600` w katalogu `0700` + `SO_PEERCRED` |
| Port serwera | strona WWW może wysłać żądanie na `127.0.0.1` bez preflight CORS (np. multipart `POST`), co pozwoliłoby podmienić model przez `/load` albo zablokować serwer długim `/inference` | wszystkie endpointy pod losowym `--request-path` z pliku `secret` (0600) — strona go nie zna; inne procesy **tego samego użytkownika** mogą go przeczytać (akceptowane: i tak mają dostęp do mikrofonu i ekranu) |
| Schowek | dyktowany tekst jest przez ~0,2–1 s w CLIPBOARD (a po niepotwierdzonym wklejeniu zostaje tam celowo), menedżery historii schowka mogą go zapisać | dokumentacja + `injection.backend = "type"` dla wrażliwych zastosowań |
| Powiadomienia | nigdy nie zawierają treści | [10](10-cli-ipc-status.md) §10.6 |

## 12.3 Macierz błędów

| # | Błąd | Wykrycie | Reakcja użytkowa | Log |
|---|---|---|---|---|
| E1 | Config niepoprawny przy starcie | walidator | proces kończy się kodem 78 (bez restartu); `systemctl status` pokazuje komunikat | ERROR z listą problemów |
| E2 | Config niepoprawny przy reload | walidator | stary config zostaje; CLI wypisuje błędy | ERROR |
| E3 | Sesja nie X11 / brak DISPLAY | start | kod 78 | ERROR |
| E4 | Hotkey zajęty (`BadAccess`) | grab | daemon działa; `hotkeys: degraded`; powiadomienie przy starcie | ERROR z nazwą skrótu |
| E5 | Utrata połączenia X11 w trakcie pracy | `ConnectionClosedError` | wyjście 0 bez operacji X11, bez restartu ([07](07-hotkeys-x11.md) §7.4); błąd połączenia **przy starcie** → wyjście 1 i restart (limit 5/60 s) | WARNING |
| E6 | Serwer nie odpowiada przy starcie | `/health` | `STARTING` do `startup_timeout_s`, potem `DOWN`; hotkey → dźwięk `error` + powiadomienie | WARNING → ERROR |
| E7 | Serwer padł w trakcie | błąd połączenia w workerze | `engine=DOWN`, kolejka wstrzymana i wznawiana po `READY`; po `startup_timeout_s` bez powrotu wszystkie zadania → `JobFailed` + jedno zbiorcze powiadomienie; continuous → zatrzymany z flush ([04](04-maszyna-stanow.md) §4.3, §4.5) | ERROR |
| E8 | Timeout transkrypcji | `socket.timeout` | 1 ponowienie po 1 s, potem `JobFailed` (continuous trwa) | ERROR z długością audio |
| E9 | HTTP 4xx/5xx od serwera | status | `JobFailed` bez retry dla 4xx, retry dla 5xx | ERROR z kodem i body ≤ 200 znaków |
| E10 | Mikrofon: brak / zajęty / zniknął | [05](05-audio-i-vad.md) §5.6 | tam opisane | WARNING/ERROR |
| E11 | Kolejka nie nadąża | `queued_audio_s > max_backlog_s` | continuous wyłączony, kolejka dokończona | WARNING |
| E12 | Wklejenie niepotwierdzone | brak pasującego żądania selekcji w `paste_timeout_ms` ([08](08-text-injection.md) §8.5 krok 7) | tekst zostaje w schowku + powiadomienie | WARNING |
| E13 | `xdotool` błąd / timeout | kod wyjścia | `InjectResult(ok=False)`, tekst w schowku (świadomie kosztem dotychczasowej zawartości — dyktowanie nie może zginąć), powiadomienie | ERROR ze stderr xdotool |
| E14 | Wyjątek w wątku (bug) | `threading.excepthook` | wątki krytyczne ([02](02-architektura.md) §2.2: controller, hotkeys, audio-consumer, pipeline, clipboard-owner) → log + `os._exit(1)` (bo `sys.exit` w wątku kończy tylko ten wątek) → restart przez systemd; wątki IPC i engine-monitor → log + ponowne uruchomienie wątku | CRITICAL z tracebackiem |
| E15 | Gniazdo IPC zajęte przez działającą instancję | `connect()` OK | wyjście 1 z komunikatem | ERROR |
| E16 | Nieudany restart serwera przy reload (np. uszkodzony model) | `systemctl` kod ≠ 0 lub `DOWN` po `startup_timeout_s` | powiadomienie „Nie udało się uruchomić silnika z nowym configiem”; `doctor` wskazuje przyczynę; stary config **nie** jest przywracany automatycznie (plik env już zmieniony) | ERROR |

Zasada nadrzędna: **żaden wyjątek nie jest połykany po cichu.** Albo jest obsłużony zgodnie z tabelą, albo kończy proces i systemd go restartuje.
