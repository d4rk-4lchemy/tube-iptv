# Tube IPTV

Jeden wspólny kanał IPTV z linków obsługiwanych przez **yt-dlp**. Filmy i playlisty trafiają do losowanej puli; wszyscy widzowie oglądają tę samą transmisję. Panel po angielsku, podgląd w przeglądarce, eksport playlisty IPTV i wybór wydań stable/nightly bez przebudowy obrazu.

## Uruchomienie

```bash
sudo docker compose up -d --build
```

- Panel: **http://localhost:8000**
- Playlista IPTV: **http://localhost:8000/playlist.m3u8**
- Strumień HLS: **http://localhost:8000/channels/main/index.m3u8**
- API: **http://localhost:8000/api/docs**

Serwer nasłuchuje na `0.0.0.0:8000`. Na telewizorze użyj adresu IP lub nazwy hosta serwera zamiast `localhost`. W panelu dodaj linki, poczekaj na ich odczytanie i skopiuj adres IPTV do VLC/Kodi/klienta IPTV. Podgląd w panelu uruchamia się dopiero po kliknięciu; sam otwarty panel ani pobranie playlisty IPTV nie uruchamia emisji.

Konfiguracja opcjonalna:

```bash
cp .env.example .env
# Edytuj .env, następnie:
sudo docker compose up -d
```

`PUBLIC_URL` określa zewnętrzny adres serwera, np. `http://192.168.1.20:8000` lub adres reverse proxy. Bez tej wartości adres jest wyznaczany z żądania. Proxy powinno przepuszczać HLS bez cache, z timeoutem odpowiedzi przynajmniej 120 sekund.

`ADMIN_PASSWORD` włącza HTTP Basic dla panelu i API (login: `admin`). `STREAM_TOKEN` zabezpiecza playlistę i segmenty osobnym tokenem, automatycznie uwzględnianym w kopiowanych adresach. Domyślna konfiguracja jest przeznaczona do zaufanej sieci lokalnej: panel bez hasła, źródła mogą kierować również do serwisów w LAN. Nie wystawiaj takiej konfiguracji do Internetu; użyj hasła i HTTPS w reverse proxy. Użytkownik administracyjny kontroluje adresy otwierane przez ekstraktor.

Stan, źródła i zainstalowane wersje yt-dlp są zapisywane w wolumenie `tube-data`. Aktualizacja obrazu nie usuwa tych danych. Aplikacja działa jako UID/GID `10001`, z systemem plików kontenera tylko do odczytu i `/tmp` w tmpfs.

## Jak działa transmisja

```text
linki → yt-dlp (metadane playlist) → SQLite → losowanie materiału
                                               ↓
                         yt-dlp (adresy video/audio, bez pobierania)
                                               ↓
                     FFmpeg (sieć → H.264 + AAC, 1920×1080 / 25 fps)
                                               ↓
                    lokalny HTTP PUT → ograniczony bufor RAM
                                               ↓
                            jedna playlista HLS → wszyscy widzowie
```

- yt-dlp działa z `--skip-download` i `--no-cache-dir`. FFmpeg czyta wskazane przez niego adresy bezpośrednio z sieci. Także rozdzielone ścieżki audio/video YouTube nie wymagają lokalnego scalania plików.
- **Żaden film ani segment HLS nie jest zapisywany do pliku.** FFmpeg wysyła segmenty na chroniony losowym sekretem endpoint loopback. Aplikacja przechowuje je jako bajty w RAM, bez katalogu z mediami i bez plików tymczasowych wideo.
- Bufor opublikowanych segmentów: maksymalnie 12 segmentów / 64 MiB. Maksymalny pojedynczy segment: 8 MiB. Oddzielna kolejka segmentów oczekujących na manifest również ma limit. Limit dotyczy buforów mediów, nie całkowitego RSS Pythona, ekstraktora i FFmpeg.
- Wyjście ma zawsze **1920×1080 / 25 fps**. yt-dlp preferuje bezpośredni strumień HTTPS do 1080p (ze względu na przewijanie); jeśli brak takiego wariantu, dopuszcza pozostałe formaty, w tym HLS; słabsze materiały są powiększane z zachowaniem proporcji i czarnymi pasami (np. po bokach dla 4:3).
- Filmy VOD są odczytywane z ograniczonym zapasem: osobna kolejka przechowuje do **3 gotowych segmentów (zwykle 12 s, maksymalnie 24 MiB)** przed publikacją. FFmpeg może ją szybko uzupełnić, ale pełna kolejka wstrzymuje upload; aplikacja nie pobiera całego filmu z wyprzedzeniem. Osobne zadanie publikuje segmenty według zegara, więc player nie zużywa tego zapasu przez start przy końcu playlisty. Transmisje źródłowe live nadal są odczytywane w czasie rzeczywistym.
- Do 20 sekund przed końcem filmu aplikacja może odczytać metadane/adresy kolejnego źródła. Ten odczyt jest anulowany po wygaśnięciu widzów; dane usuniętego źródła nie są używane przy przejściu.
- Segment trwa zwykle 4 sekundy. Playlisty HLS udostępniają ostatnich 6 segmentów, starsze są krótko przechowywane dla klientów. Granice materiałów mają `EXT-X-DISCONTINUITY`, a globalna numeracja segmentów nie cofa się.
- Zegar kanału startuje po odczytaniu pierwszego źródła i biegnie również bez widzów. Pierwszy widz uruchamia ekstrakcję i FFmpeg z przewinięciem do aktualnej pozycji programu. Kolejni dołączają do tego samego producenta. Na start kanał wysyła planszę **LOADING…** z ciszą (H.264/AAC 1080p, wideo CBR 3000 kb/s z wypełnieniem HRD, również dla statycznego obrazu), także do zewnętrznych odtwarzaczy IPTV. Pierwsze dwa segmenty planszy generowane są w przyspieszonym tempie w RAM, równolegle z ekstrakcją źródła, a kolejne w tempie odtwarzania. Pierwszy gotowy segment filmu kończy generator planszy; przejście używa `EXT-X-DISCONTINUITY`. Odtwarzacz może jeszcze wyświetlić wcześniej zbuforowaną planszę. Plansza nie zatrzymuje zegara kanału ani nie skraca ekstrakcji. HLS wprowadza kilkunastosekundowe opóźnienie.
- HLS składa się z krótkich zapytań HTTP, więc zakończenie oglądania jest rozpoznawane po braku kolejnych żądań. Domyślnie **25 sekund** po ostatnim żądaniu manifestu/segmentu kanał zatrzymuje procesy i zwalnia RAM (`IDLE_SECONDS`). Aktywne oczekiwanie na pierwszy manifest utrzymuje start; po rozłączeniu żądania lub 90 sekundach timeoutu jest kończone. Samo API statusu nie przedłuża emisji.
- Licznik widzów pokazuje aktywne sesje HLS, a nie zweryfikowaną liczbę osób. Nietypowe klienty odrzucające URL po przekierowaniu mogą zawyżać ten licznik do wygaśnięcia sesji.
- Punkt startu zegara, katalog programów i seed losowania są przechowywane jako metadane w SQLite. Po restarcie bieżąca pozycja jest obliczana z czasu rzeczywistego, także po przejściu przez wiele cykli bez widzów. Sam zegar nie uruchamia yt-dlp ani FFmpeg i nie pobiera mediów.
- Przykład: film A ma 10 minut i zaczyna się o 12:00. Widz dołączający o 12:02 dostaje materiał przewinięty do około 02:00, z uwzględnieniem czasu ekstrakcji i zwykłego opóźnienia HLS. Po zakończeniu A zegar przechodzi do następnego filmu nawet wtedy, gdy nikt nie ogląda. Segmenty mają `EXT-X-PROGRAM-DATE-TIME`.
- Losowanie odbywa się bez powtórzeń w obrębie cyklu. Identyczne adresy materiałów z nakładających się playlist są deduplikowane. Na granicy cyklu nie ma natychmiastowej powtórki, jeśli są inne materiały.
- Dodanie źródła nie cofa bieżącego slotu; usunięcie stosuje wybraną politykę kończenia filmu. Przyszła rotacja jest przeliczana z nowej puli. Domyślnie usunięcie lub wyłączenie źródła zatrzymuje także bieżący materiał, anuluje jego ekstrakcję/FFmpeg, usuwa stare segmenty i przebudowuje kolejkę. Przełącznik **Finish the current video when its source is removed** pozwala dokończyć wyłącznie aktualny film; reszta usuniętej playlisty nie wróci do kolejki. Ustawienie jest zapisywane w SQLite i domyślnie wyłączone. Podgląd WWW resetuje swój bufor po zmianie rewizji streamu (najpóźniej przy kolejnym odczycie statusu, co 2 s). Zewnętrzny odtwarzacz IPTV może jeszcze odtworzyć bajty pobrane wcześniej do własnego bufora — serwer nie może ich wycofać. Materiał obecny również w innym włączonym źródle pozostaje dostępny. Playlistę można ponownie odczytać przyciskiem ↻. Limit odczytu to pierwsze 500 pozycji (`MAX_PLAYLIST_ITEMS`).
- Niedostępny materiał jest ponawiany z rosnącym odstępem; zegar biegnie dalej i po końcu jego slotu przechodzi do następnego programu. Powolny serwis źródłowy lub błąd ekstrakcji może spowodować przerwę w odtwarzaniu; aplikacja nie przechowuje zapasowych filmów na dysku. Dokładna ramówka wymaga długości filmów w metadanych yt-dlp. Dla nieznanej długości aplikacja rezerwuje tymczasowo godzinny slot (oznaczony `~` w panelu); długość może zostać skorygowana podczas aktywnego odtwarzania po ekstrakcji lub dojściu do końca filmu. Bez długości materiału nie da się dokładnie wyznaczyć granic kolejnych programów bez dostępu do mediów. Transmisje live otwierane są na ich bieżącej krawędzi, bez przewijania; slot jest ograniczony dostępnymi metadanymi lub godziną.

## Wersje yt-dlp

Panel pobiera z GitHuba ostatnie 25 wydań z oficjalnych repozytoriów stable i nightly. „Najnowsza dostępna” rozwiązuje się do konkretnego tagu. Można przełączyć gałąź, zaktualizować lub wrócić z nightly do stable.

Instalator pobiera oficjalny Python zipapp `yt-dlp` i `SHA2-256SUMS` przez HTTPS, weryfikuje SHA-256, uruchamia `--version`, a dopiero potem atomowo zapisuje wybór. Niepowodzenie zostawia poprzednią wersję aktywną. Rozpoczęte procesy kończą pracę ze swoją wersją; nowe używają wybranej. Wersje są przechowywane w `/data/versions`; nie są to pliki multimedialne. Aplikacja nie wykonuje dowolnych komend z interfejsu.

Obraz zawiera początkowe stable przypięte w `uv.lock`, zależności yt-dlp, komponent EJS oraz Deno do obsługi wyzwań JavaScript. Przełączenie zipappa nie aktualizuje Deno ani zależności EJS — gdy przyszłe wydanie podniesie ich wymagania, trzeba zaktualizować lock/obraz. API GitHuba podlega limitom; lista jest cache'owana przez 5 minut, a błędy są widoczne w panelu.

Dostępność materiału nadal zależy od źródłowego serwisu. Blokady IP, logowanie, ograniczenia geograficzne, DRM lub zmiany YouTube mogą uniemożliwić ekstrakcję. Aktualizacja nightly może pomóc w zmianach ekstraktora, ale nie omija ograniczeń dostępu. Obecna wersja UI nie zarządza cookies ani kontami serwisów.

## VAAPI / QSV

Domyślnie działa kodowanie CPU (`libx264`). Opcjonalnie:

```bash
stat -c '%g' /dev/dri/renderD128 /dev/dri/card0
# Wpisz odpowiednie RENDER_GID i VIDEO_GID w .env
ENCODER=vaapi sudo -E docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

W praktyce najwygodniej ustawić `ENCODER=vaapi` w `.env`. Dla Intel Quick Sync użyj `ENCODER=qsv`. `VAAPI_DEVICE` pozwala wskazać inne urządzenie renderujące. Plik `compose.gpu.yaml` przekazuje `/dev/dri` i dodatkowe grupy urządzenia; nie wymaga kontenera uprzywilejowanego. W obrazie są sterowniki Mesa VAAPI i Intel Media.

Sprzętowe jest kodowanie; dekodowanie i skalowanie pozostają programowe, aby obsługiwać mieszane formaty źródłowe. Niewspierany sprzęt/sterownik daje błąd w dzienniku — wtedy wróć do `software`. Tryby GPU wymagają weryfikacji na docelowym hoście. W środowisku przygotowania sprawdzono pełną ścieżkę HLS/Chromium zarówno na CPU, jak i VAAPI; QSV zwróciło błąd inicjalizacji sesji MFX, więc nie jest tu potwierdzone.

## Rozwój

Python / FastAPI / SQLite, natywne moduły JS i lokalnie dostarczany hls.js. Brak zależności runtime od CDN. Jeden worker Uvicorn jest wymagany, ponieważ producent i bufory są współdzielone w pamięci tego procesu.

| Moduł | Odpowiedzialność |
| --- | --- |
| `app/sources.py` | Jedyna ścieżka wejściowa: ekstrakcja przez yt-dlp |
| `app/versions.py` | Wydania, weryfikacja, atomowe przełączanie |
| `app/engine.py` | Producent kanału, przewijanie wejść, HLS w RAM |
| `app/buffer.py` | Ograniczony zapas segmentów i publikacja według czasu programu |
| `app/slate.py` | Plansza startowa HLS w RAM, generowana tylko dla aktywnej sesji |
| `app/timeline.py` | Trwały zegar ramówki, losowanie cykli, bieżący program i offset |
| `app/db.py` | Kanały, źródła, materiały i ustawienia |
| `app/main.py` | API, lifecycle, autoryzacja, endpointy IPTV |
| `app/static/` | Panel, moduły API/odtwarzacza, responsywne style |

Model danych już rozdziela kanały od źródeł i materiałów; obecne API/UI udostępnia tylko kanał `main`. Kolejne kanały wymagają rejestru instancji `Channel`, dynamicznych tras i CRUD kanałów. Własną kolejność można wprowadzić jako nową strategię w `Timeline`. XMLTV może wykorzystać sloty `Timeline`, ale wymaga osobnego eksportera i kompletnej metadanej długości; **nie jest jeszcze zaimplementowane**. Każda taka rozbudowa ma zachować yt-dlp jako jedyny sposób interpretacji źródeł. Nie ma skanera lokalnych plików ani bibliotek Plex/Jellyfin.

Lokalnie (Python 3.11+, FFmpeg z libx264 i drawtext, font DejaVu Sans, Node lub Deno):

```bash
uv sync --extra test
npm ci && npm run vendor
DATA_DIR=.data uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Nie zmieniaj portu samego Uvicorna bez ustawienia identycznego `PORT` — FFmpeg przesyła segmenty na ten port przez loopback. Docker ustawia obie wartości wspólnie.

Testy:

```bash
uv run pytest -q
npx playwright install chromium
mkdir -p artifacts
node scripts/ui-check.mjs
```

Test pełnej ścieżki na **osobnym pustym kontenerze** (sam generuje dwie krótkie próbki audio/video, bez pobierania filmów):

```bash
sudo docker run -d --name tube-iptv-test -p 8001:8000 \
  --add-host host.docker.internal:host-gateway --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 -e IDLE_SECONDS=25 tube-iptv:local
BROWSER_CHECK=1 uv run python scripts/smoke.py
sudo docker rm -fv tube-iptv-test
```

Test sprawdza prawdziwy yt-dlp i FFmpeg, współdzielenie osi czasu, kodeki przez ffprobe, przejścia HLS, odtwarzanie w Chromium oraz zatrzymanie i zwolnienie bufora. Port 8766 udostępnia na czas testu lokalne próbki kontenerowi. `TUBE_URL` i `FIXTURE_HOST` pozwalają zmienić adresy.

Eksport obrazu:

```bash
sudo docker save tube-iptv:local | gzip > tube-iptv.tar.gz
# Na innym hoście:
gunzip -c tube-iptv.tar.gz | sudo docker load
```

## Dokumentacja wykorzystanych projektów

- [yt-dlp: instalacja, wydania i opcje](https://github.com/yt-dlp/yt-dlp#readme)
- [FFmpeg: muxer HLS, publikowanie przez HTTP PUT](https://ffmpeg.org/ffmpeg-formats.html#hls-2)
- [hls.js: odtwarzacz przeglądarkowy](https://github.com/video-dev/hls.js)
- [Tunarr: architektury transmisji i transkodowanie](https://tunarr.com/configure/transcoding/) — punkt odniesienia dla kanału współdzielonego; Tube ma odrębną implementację i źródła wyłącznie yt-dlp.

Test zegara i rzeczywistego przewijania (tylko na pustym, testowym kontenerze):

```bash
TEST_CONTAINER=tube-iptv-test uv run python scripts/timeline-check.py
```

Test przesuwa zapisany zegar o 120 sekund, restartuje testowy kontener, dekoduje pierwszą klatkę HLS i sprawdza jej kolor. Następnie wygasza transmisję i sprawdza ponowne dołączenie bez cofania programu. Używa wygenerowanego lokalnie filmu oraz portu 8767; nie pobiera filmów testowych z YouTube.

## Diagnostyka startu i segmentów

`docker compose logs -f` pokazuje czas ekstrakcji, pierwszy kompletny segment, gotowość dwóch segmentów, przerwane uploady i ostrzeżenia FFmpeg. Te same ostatnie pomiary są w `GET /api/status` → `diagnostics`. Adresy HTTP z ostrzeżeń FFmpeg są redagowane, aby nie zapisywać podpisanych URL-i CDN.

Manifest FFmpeg i segment mogą docierać różnymi połączeniami, w dowolnej kolejności. Serwer publikuje segment dopiero po otrzymaniu **zarówno metadanych, jak i całego body**, zachowując kolejność wejściową. Utracony upload oznacza discontinuity i zachowanie jego miejsca na osi czasu.

Wewnętrzny odbiornik uploadów słucha wyłącznie na `127.0.0.1`, na automatycznie wybranym porcie, i sprawdza sekret kanału. Używa trwałych połączeń HTTP oraz `100 Continue`: FFmpeg dostaje zgodę na przesłanie segmentu dopiero, gdy jest miejsce w buforze. Stała lokalna nazwa użytkownika `upload` w URL uruchamia ten handshake w FFmpeg 5; nie jest hasłem ani sposobem autoryzacji. Samo keep-alive nie zapewniało poprawnego hamowania producenta. Odbiornik doczytuje kompletne żądania również po zamknięciu strony nadawczej, bez zapisu materiału na dysk. Po zakończeniu FFmpeg aplikacja czeka na końcową playlistę `ENDLIST` i przyjęcie jej segmentów do RAM, ale nie czeka na opróżnienie kolejki emisji przed przygotowaniem kolejnego źródła.

Segment kolejnego klipu zaczyna się najwcześniej po końcu poprzedniego segmentu. Recovery wznawia od końca już przyjętych danych, również tych oczekujących w RAM; nie przewija ponownie do pozycji zegara. Wcześniejsze zakończenie źródła przesuwa istniejącą kolejność harmonogramu zamiast rozpoczynać ją od pierwszych utworów.

Test usuwania aktywnego źródła, kończenia ostatniego filmu oraz dużych segmentów:

```bash
# Na pustej, osobnej instancji pod portem 8001:
uv run python scripts/removal-check.py
```

Test planszy startowej i skalowania 4:3 do 1080p (pusty kontener testowy):

```bash
uv run python scripts/loading-check.py
```

Test używa celowo opóźnionego źródła HTTP na porcie 8769, sprawdza zdekodowane piksele w Chromium i zapisuje zrzuty planszy oraz filmu w `artifacts/`.

Test odporności na przerwę producenta (wyłącznie pusty kontener testowy; zatrzymuje jego FFmpeg na 6 sekund):

```bash
TEST_CONTAINER=tube-iptv-test uv run python scripts/buffer-check.py
```

Diagnostyka `/api/status` pokazuje `reserve_segments`, `reserve_seconds`, `reserve_bytes` oraz bieżący i maksymalny odstęp między publikacjami. Zapas nie gwarantuje ciągłości przy przerwie dłuższej niż dostępne segmenty ani przy problemach połączenia od serwera do odtwarzacza.
