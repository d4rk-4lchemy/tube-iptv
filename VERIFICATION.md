# Weryfikacja

Sprawdzone w środowisku przygotowania aplikacji, 2026-09-23:

- `uv run pytest -q`: **10 testów zaliczonych** — walidacja źródeł, duplikaty, kaskadowe usuwanie, losowanie, ciągłość numeracji HLS i znaczniki discontinuity, limity bufora, zatrzymanie po ostatnim widzu, uwierzytelnianie, ochrona endpointu ingest, błędna aktualizacja i powrót nightly → stable.
- Zbudowano obraz `tube-iptv:local` i uruchomiono przez `compose.yaml`, jako nieuprzywilejowany użytkownik, z rootfs tylko do odczytu. Aplikacja nasłuchuje na `0.0.0.0:8000`.
- Pełna ścieżka CPU na wygenerowanych próbkach HTTP: prawdziwy yt-dlp, FFmpeg, dwa niezależne identyfikatory widza wspólnie oglądające kanał, analiza segmentu przez ffprobe (H.264 1280×720 + AAC), przejście między filmami, odtworzenie w Chromium oraz zatrzymanie i wyczyszczenie RAM.
- Prawdziwe źródło YouTube `jNQXAC9IVRw`: poprawny odczyt, uruchomienie transmisji, pobranie segmentu z bufora RAM i potwierdzenie H.264/AAC przez ffprobe.
- Playlista YouTube `PLwP_SiAcdui0KVebT0mU9Apz359a4ubsC`: odczyt **95 pozycji**, bez uruchomienia FFmpeg. Źródła testowe zostały usunięte.
- Rzeczywiste pobranie i przełączenie na nightly **2026.09.16.232951**, następnie powrót do stable **2026.08.19**; oba pobrania zweryfikowane SHA-256. Test używał osobnego katalogu danych developerskich.
- Panel w Chromium: desktop 1440 px, mobile 390 px, brak poziomego przewijania, zmiana nazwy kanału, listy wydań stable i nightly, brak wyjątków JavaScript. Zrzuty w `artifacts/`.
- VAAPI: pełny test `BROWSER_CHECK=1 uv run python scripts/smoke.py` zaliczony na `/dev/dri/renderD128`, włącznie z odtwarzaniem w Chromium, przejściami między filmami i wyłączeniem po 25 sekundach bez widzów. QSV: próba inicjalizacji zwróciła `Error initializing an MFX session: -3`; ten wariant pozostaje niepotwierdzony na tym sprzęcie/sterowniku. Domyślny encoder: CPU.
- Po zatrzymaniu transmisji kontener miał tylko proces init i Uvicorn. W `/data` i `/tmp` nie było plików multimedialnych, jedynie pliki SQLite.

To weryfikacja funkcjonalna, nie test obciążenia ani gwarancja przyszłej dostępności dowolnego filmu/serwisu. Dostęp YouTube i kompatybilność wydań yt-dlp mogą zmieniać się niezależnie od aplikacji. Obsługa wielu kanałów, ramówki i XMLTV jest kierunkiem rozbudowy, nie obecną funkcją.

## Aktualizacja: język interfejsu i stale działający zegar kanału

- Interfejs, opisy dostępności, powiadomienia, komunikaty API i dziennik są po angielsku; nazwy źródeł i tytuły materiałów zachowują oryginalną treść.
- `uv run pytest -q`: **19 testów zaliczonych**. Dodatkowo sprawdzono dołączenie po 120 sekundach, pozycję po restarcie, przejścia przez wiele cykli bez producenta, zmiany źródeł bez cofnięcia aktywnego programu, usuwanie nieaktywnych slotów, aktualizację nieznanej długości i przewijanie obu wejść audio/video.
- `scripts/timeline-check.py`: zegar przesunięto o 120 sekund i zrestartowano osobny kontener. Pierwsza zdekodowana klatka była zielona (późniejsza część próbki), a nie czerwona (początek). Po wygaśnięciu ostatniej sesji nie było procesu FFmpeg ani bufora, a ponowne połączenie trafiło dalej w ten sam slot. Test przesuwa metadane zegara; nie wymaga czekania dwóch minut czasu rzeczywistego.
- Dokładne przejścia ramówki wymagają znanej długości materiałów. Dla metadanych bez długości panel pokazuje szacowany slot `~`; dla live odtwarzana jest bieżąca krawędź źródła. Opóźnienie bufora HLS nadal obowiązuje.

- Ponowny test pełnej ścieżki po zmianie zegara: `BROWSER_CHECK=1 uv run python scripts/smoke.py` zaliczony — wspólna oś czasu, H.264/AAC, przejścia HLS, odtwarzanie w Chromium i zwolnienie bufora po zakończeniu sesji.

## Diagnostyka opóźnień i usuwanie źródeł

- W lokalnym dzienniku pierwotny start od 20:22:41 do uruchomienia FFmpeg o 20:22:44 obejmował około 3,05 s ekstrakcji. Gotowe segmenty pobierane lokalnie i w Chromium zajmowały około 3–13 ms. Nie było plików multimedialnych w `/data`, a FFmpeg miał wejścia sieciowe i wyjście HTTP.
- Występowały `ClientDisconnect` podczas wewnętrznych uploadów. Analiza segmentów wykazała m.in. przesunięcie PTS o 28 s przy 20 s deklarowanych w playliście. Niezależny test HTTP odtworzył sytuację, w której manifest kończył się przed zakończeniem odbioru odpowiadającego mu segmentu. Dawny kod nie synchronizował obu zdarzeń.
- Nowy kod czeka na komplet danych i metadanych, publikuje według numeru segmentu wejściowego, zachowuje czas utraconych segmentów i oznacza przerwy HLS. Włączono trwałe połączenia wyjściowe HTTP. Błędy odbioru i czasy etapów są jawnie logowane.
- `uv run pytest -q`: **29 testów zaliczonych**. Nowe przypadki obejmują manifest przed body, body w odwrotnej kolejności, utracony upload, natychmiastowe usunięcie/wyłączenie/odświeżenie aktywnego źródła, opcjonalne dokończenie tylko bieżącego filmu, jego końcowe segmenty i źródła nakładające się.
- `scripts/removal-check.py`: rzeczywisty yt-dlp i FFmpeg na ruchomej próbce 720p, segmenty o rozmiarach porównywalnych z normalną emisją. Start: 1,357 s ekstrakcji, pierwszy segment 4,790 s po starcie FFmpeg, dwa segmenty gotowe po 10,162 s łącznie. Brak utraconych/przerwanych uploadów. Usunięcie bieżącego źródła unieważniało jego stare segmenty; włączenie zachowania pozwalało dokończyć ostatni film i otrzymać `EXT-X-ENDLIST`.
- Panel desktop/mobile: przełącznik działa i zachowuje ustawienie po odświeżeniu. Ponownie sprawdzono odtwarzanie HLS w Chromium, przejścia między filmami oraz wygaszanie producenta.

Historyczny całkowity czas pierwszego startu nie był wcześniej logowany, więc nie przypisujemy mu dokładnej wartości. Osobna próba ponownego odczytu tego samego filmu z YouTube zwróciła HTTP 403 na URL-u CDN; to niezależna możliwość błędu źródła, nie dowód przyczyny pierwotnego oczekiwania w przeglądarce.

## Plansza LOADING i wyjście 1080p

- `uv run pytest -q`: **31 testów zaliczonych**, w tym przejście ze sztucznej planszy do filmu i odrzucenie segmentu planszy, którego publikacja była w toku podczas przełączenia.
- Wyjście H.264/AAC ma 1920×1080 / 25 fps. Ekstraktor preferuje format do 1080p. Skalowanie uwzględnia proporcje obrazu (DAR), normalizuje piksele i dopełnia kadr czernią.
- `scripts/loading-check.py`: zimny start, rzeczywisty yt-dlp/FFmpeg i celowo opóźnione lokalne źródło HTTP. Dwa segmenty planszy gotowe po **0,735 s**, tekst zdekodowany przez Chromium po **1,069 s** od kliknięcia. Dwa segmenty właściwego filmu gotowe po 16,422 s. Odtwarzanie automatycznie przeszło do filmu; analiza pikseli potwierdziła czerwony obraz 4:3, czarne pasy boczne oraz 1920×1080. Brak utraconych i przerwanych uploadów. Po rozłączeniu bufor został wyczyszczony, a w kontenerze pozostały tylko init i Uvicorn.
- Plansza to ciągły strumień H.264/AAC z rosnącymi znacznikami czasu, a nie zapętlony plik TS. Pierwsze dwa segmenty powstają w przyspieszonym tempie, dalsze są dawkowane przez HTTP. Generator kończy pracę po pierwszym gotowym segmencie filmu. Zbuforowane wcześniej segmenty planszy pozostają dostępne dla odtwarzacza. Zegar ramówki nadal biegnie.
- `scripts/timeline-check.py`: ponownie zaliczono przewinięcie po 120 sekundach, restart, ponowne połączenie do późniejszego miejsca tego samego filmu oraz brak producenta i bufora bez widza. Test pomija syntetyczne segmenty startowe przy badaniu czasu programu.
- `scripts/removal-check.py`: ruchoma próbka 720p powiększana do 1080p. Ponownie zaliczone natychmiastowe usunięcie aktywnego źródła, unieważnienie starych segmentów, opcjonalne dokończenie ostatniego filmu i ENDLIST. Plansza gotowa po 0,748 s, właściwe dwa segmenty po 10,324 s; 0 utraconych segmentów i 0 przerwanych uploadów.

## Poprawka przewijania wybranego wariantu YouTube

- W instancji z obrazu `b7d0d283870d` film `F46r-_jPPHY` wybierał format 616: VP9 1080p przez `m3u8_native`, plus audio 251. FFmpeg po przewinięciu kończył z kodem 0, lecz z zerem klatek/segmentów. Plansza pozostawała aktywna, a ekstrakcja była bezskutecznie ponawiana. Testy na lokalnym MP4 nie obejmowały tego wariantu wejściowego YouTube.
- Selektor preferuje teraz bezpośrednie wejścia HTTPS do 1080p, pozostawiając alternatywy dla źródeł dostępnych wyłącznie przez HLS i źródeł audio. Dla tego samego filmu wybierany jest format 399 (AV1 1080p HTTPS) + 251; przewinięcie do 20 s i czterosekundowa transkodowana próbka dały 99 klatek oraz audio.
- Dodatkowa weryfikacja dokładności: pierwsza klatka po wejściowym `-ss 20` ma hash `c604e3fb888c4b0fe5c9ff8a5f76a36a`, identyczny z klatką po odkodowaniu wejścia od początku i wyjściowym `-ss 20`. Początek filmu ma inny hash (`b2d62407ca3314113bd02abe72bdee93`). Porównanie wykonano na tej samej wersji formatu, po skalowaniu do 160×90, bez zapisu mediów na dysk.
- `uv run pytest -q`: **34 testy zaliczone**. Nowe testy używają prawdziwego selektora yt-dlp na metadanych formatów: preferencja seekowalnego 1080p zamiast wysoko ocenionego HLS/4K, zachowanie HLS-only, muxed i audio-only.
- Wdrożony obraz `f72fe2239178`: lokalny kanał dołączył do bieżącego filmu na 18 s, pierwszy segment gotowy po 5,00 s od startu FFmpeg, dwa po 11,78 s od połączenia. ffprobe potwierdził H.264 1920×1080 + AAC. Brak utraconych segmentów i przerwanych uploadów. Kontener healthy, po wygaśnięciu widzów producent zatrzymany.
- Log zawiera teraz identyfikatory/protokoły wybranych formatów oraz powód ponowienia; brak segmentów uwzględnia kod wyjścia i pozycję przewijania.

## Plansza LOADING z CBR 3000 kb/s

- Generator planszy używa `-b:v 3000k -minrate 3000k -maxrate 3000k -bufsize 3000k` i `nal-hrd=cbr:filler=1`, aby statyczny obraz wypełniał również bufory IPTV liczone w bajtach.
- Pierwszy czterosekundowy segment miał **1 509 264 bajty**, około **3018,5 kb/s** łącznie z audio i narzutem MPEG-TS (wideo pierwszego segmentu: 2927,7 kb/s podczas inicjalizacji VBV).
- `uv run pytest -q`: 34 testy zaliczone. `scripts/loading-check.py`: potwierdzony rozmiar pierwszego segmentu, tekst planszy, automatyczne przejście do filmu 1080p z pasami dla 4:3 oraz zwolnienie RAM po rozłączeniu. Plansza gotowa po 0,719 s; brak utraconych segmentów i przerwanych uploadów.

## Rezerwa segmentów dla krótkich przerw wejścia

- Badanie poprzedniej instancji: 4-sekundowe segmenty publikowane co około 3,8–4,1 s (próbkowanie co 250 ms), pobranie gotowego segmentu lokalnie 4–14 ms. W Chromium przez 55 sekund zapas 8,7–12,1 s, bez zdarzeń waiting/stalled i bez zgubionych klatek. W logu źródła o 21:13:54 UTC wystąpiło zerwanie TLS i ponowne połączenie; nie potwierdza to przyczyny wszystkich przerw na urządzeniu użytkownika.
- Użytkownik zgłosił jednoczesne zatrzymania obrazu i dźwięku w Televizo na tablecie Android z buforem „Średni”. Nie ustalono wiarygodnej wartości tego ustawienia w sekundach i nie wykonano testu bezpośrednio na tablecie.
- VOD nie jest już ograniczane wejściowym `-re`. Oddzielny `MediaBuffer` przechowuje do trzech gotowych segmentów (zwykle 12 sekund, maksymalnie 24 MiB) przed publikacją. Pełna kolejka blokuje upload producenta, a osobne zadanie publikuje według czasu programu. Zapas nie przesuwa publicznej krawędzi kanału w przyszłość. Dla wejść live pozostaje `-re`.
- Odczyt adresów następnego materiału rozpoczyna się do 20 s przed końcem bieżącego slotu. Wynik jest używany tylko przy zgodności slotu; usunięcie źródła i wygaszenie sesji anulują odpowiednie zadanie.
- `uv run pytest -q`: **37 testów zaliczonych**, w tym granica zapasu, brak przedwczesnej publikacji, publikacja z zapasu bez nowego wejścia, zwolnienie zablokowanego uploadu po zamknięciu oraz anulowanie odczytu następnego źródła.
- `scripts/buffer-check.py`: na osobnym kontenerze rzeczywisty FFmpeg zatrzymano przez SIGSTOP na 6 sekund, następnie wznowiono. Segmenty nadal publikowano co 4,000 s; po wznowieniu rezerwa wróciła do 3 segmentów / 12 s. Obserwacja objęła też kolejne publikacje po wznowieniu. Brak utraconych segmentów/przerwanych uploadów.
- Ponownie zaliczone `scripts/loading-check.py`, `scripts/timeline-check.py` i `scripts/removal-check.py`: plansza 3000 kb/s, przejście do 1080p, właściwe przewinięcie po 120 s, zachowanie zegara po rozłączeniu, natychmiastowe usunięcie źródła, opcjonalny ENDLIST po ostatnim filmie i zwolnienie buforów. W teście planszy gotowa rezerwa 3 segmentów; publikacje co 4,000 s.
- `BROWSER_CHECK=1 scripts/smoke.py` również zaliczony: współdzielony kanał, H.264/AAC 1080p, przejścia między filmami, odtwarzanie w Chromium i wyłączenie po ostatnim widzu.
- Po wdrożeniu obrazu `9d49dbfcd8f3` rzeczywisty kanał YouTube („Deer Dance”) dołączył na 80 s, pierwszy segment opublikowano po 4,00 s od startu FFmpeg, a rezerwa osiągnęła 3 segmenty / 12 s / około 4,8 MB. Brak utraconych segmentów i przerwanych uploadów.

## Utrata uploadów przy pełnej rezerwie — 2026-09-23

- Diagnoza na działającym kontenerze: FFmpeg zamykał połączenie PUT przed odpowiedzią odbiorcy. Samo blokowanie odczytu ASGI nie zapewniało kontroli tempa producenta; rozłączenie mogło usunąć nieodebrane dane. Przy wyłączonych trwałych połączeniach obserwowano przerwane uploady i ośmiosekundowe odstępy publikacji. Samo przywrócenie keep-alive nadal nie wystarczało do niezawodnego odebrania końcówki filmu.
- Odbiornik HTTP na loopback używa `h11` i zachowuje kompletne dane po EOF. Handshake `100 Continue` wstrzymuje następny segment przed wysłaniem body, gdy rezerwa jest pełna. FFmpeg używa trwałego połączenia. Zmiana materiału czeka na odebranie końcowego segmentu i manifestu ENDLIST, ale nie na opróżnienie rezerwy przez widza. Media nadal pozostają wyłącznie w RAM.
- `uv run pytest -q`: **49 testów zaliczonych**. Sprawdzone m.in. kompletny upload 3 MiB z zamknięciem nadawcy, odrzucenie niekompletnego body, trwałe połączenie, sekret odbiornika i wstrzymanie `100 Continue` przy pełnej rezerwie.
- Na osobnym kontenerze `scripts/removal-check.py` odebrał 28,28 s z pozostałych około 28 s materiału oraz ENDLIST. `scripts/buffer-check.py` przetrwał SIGSTOP FFmpeg na 6 s, z maksymalnym odstępem publikacji 4,001 s; oba testy bez utraconych segmentów i przerwanych uploadów.
- Lokalny kanał YouTube po wdrożeniu: przez cztery minuty „Lionhearted” zero utraconych segmentów i przerwanych uploadów, maksymalny odstęp publikacji 4,002 s. Naturalny koniec: odebrany końcowy segment 64, 259,64 s mediów przy 259,66 s pozostałej długości, następnie „Unfold” od 0 s. Po zmianie nadal zero strat i przerwanych uploadów.
- Testy dotyczą lokalnego kontenera i Chromium; nie obejmują bezpośredniego odtwarzania na tablecie użytkownika ani wszystkich możliwych awarii zewnętrznych źródeł.
- `BROWSER_CHECK=1 uv run python scripts/smoke.py`: zaliczony po restarcie izolowanej instancji (pierwszą próbę zakłóciły sesje widzów pozostałe po poprzednim teście). Potwierdzone wspólna oś czasu, H.264/AAC 1080p, niezmienne identyfikatory i nienakładające się czasy segmentów przez kilka filmów, odtwarzanie w Chromium oraz wygaszenie producenta i zwolnienie RAM.
