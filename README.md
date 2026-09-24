# Tube IPTV

<p align="center">
  <img src="app/static/logo.svg" alt="Tube IPTV logo" width="144">
</p>

<p align="center"><strong>Your IPTV channels, built from yt-dlp sources.</strong><br>
Add videos or playlists, and viewers of each channel join the same live programme position.</p>

Tube IPTV turns links supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp) into independent HLS channels with continuous schedules. It includes an English web console, browser preview, IPTV playlist export, and selectable stable/nightly yt-dlp releases without rebuilding the image.

## Features

- Independent channels, each with its own sources and a wall-clock programme shared by its viewers.
- Video and playlist sources from yt-dlp-supported services.
- Weekly programmes with independent sources, multiple emission times, a draggable calendar and timezone-aware XMLTV listings.
- HLS output at selectable 480p, 720p, 1080p (default), or 4K with H.264 video, AAC audio, and per-channel frame rates (60 FPS by default).
- RAM-only media buffers: videos and HLS segments are never written to disk.
- Web preview, source management, playlist export, and broadcast diagnostics.
- Stable and nightly yt-dlp releases with checksum verification and atomic activation.
- CPU encoding by default, with optional VAAPI and Intel Quick Sync encoding.
- A single Uvicorn worker with persistent scheduling metadata in SQLite.

## Quick start

Requirements: Docker with Compose and a host that can run FFmpeg in the image.

```bash
docker compose up -d --build
```

Open the web console at <http://localhost:8000> and add a video, playlist, or channel URL.

Use the channel selector to switch channels and **New channel** to create one. Names, sources, previews, diagnostics, resolutions, frame rates, and source-removal settings apply to the selected channel. The yt-dlp installation and updates are shared by all channels. The IPTV playlist contains every channel; each starts media production only when watched. Removing a channel stops its stream and deletes its sources and schedule; at least one channel must remain. Existing installations keep their `main` channel and schedule.

| Endpoint | Purpose |
| --- | --- |
| <http://localhost:8000> | Web console and browser preview |
| <http://localhost:8000/playlist.m3u8> | IPTV playlist containing all channels |
| <http://localhost:8000/epg.xml> | XMLTV programme guide for all channels |
| <http://localhost:8000/channels/main/index.m3u8> | Main HLS stream |
| <http://localhost:8000/api/docs> | OpenAPI documentation |

For a TV or another device, replace `localhost` with the server's IP address or hostname. Copy the IPTV URL into VLC, Kodi, or another IPTV player. The browser preview starts only after clicking **Watch channel**; opening the console or downloading the playlist does not start media production.

### XMLTV programme guide

The IPTV playlist advertises `/epg.xml` through its `x-tvg-url` header. Players that support this attribute can discover the guide automatically. Otherwise, copy **XMLTV EPG URL** from the console's IPTV card into your player's EPG settings. Channel IDs match the playlist's `tvg-id` values and remain stable after renaming.

**EPG horizon** applies to all channels: choose 1–7 days ahead, with 2 days as the default. The setting persists across restarts. The guide includes the current programme with its full start time and the last programme with its full end time. Channels without programmes or media have no programme entries. Channels with weekly programmes also list their unplanned gaps. Times are UTC; players can display them in their local timezone.

For channels without weekly programmes, EPG is a forecast of the shared channel clock, without adjustment for HLS buffering. Adding or removing sources, learning a duration, or ending a video early can change upcoming times. Unknown durations use the same estimated one-hour slots as playback. Refresh EPG in your player to see changes; no historical listings, descriptions, thumbnails, or catch-up playback are provided.

Each request streams a consistent snapshot of the schedule without starting extraction, FFmpeg, or a viewer session. The export is generated in memory and is not cached by the server. It uses the same `STREAM_TOKEN` protection as the playlist; copied and advertised links include that token and respect `PUBLIC_URL`. `ADMIN_PASSWORD` protects the settings API (`GET`/`PATCH /api/epg/settings`, body `{"days": 2}`), while the XMLTV endpoint uses only the stream token.

### Weekly programmes

Open **Programmes → New programme** in the selected channel. Enter a name, duration of 1–1440 local minutes, and one or more weekly times. Each time has a start clock time and selected weekdays; **Every day** selects all seven. Save, then add video, playlist or channel links under **Programme sources**. The same yt-dlp extractor handles these links and general Sources. Pending, empty or unavailable programme pools air black with silent audio.

Programme sources are separate from general **Sources**. A URL can be added independently to several programmes and to the general pool. Enabling, refreshing or removing one does not change the others. General Sources supply the old continuous rotation when there are no programmes; once a programme exists, its weekly schedule controls the channel.

The weekly calendar includes unplanned gaps and uses the application's timezone, even if your browser is in another timezone. Dragging an emission changes that weekday every week, in five-minute steps. For example, dragging Wednesday from an everyday 18:00 rule to 20:00 creates a separate Wednesday rule and leaves the other six days at 18:00. Moving to another column changes the weekday too. Use **Edit** for minute precision, duration changes or keyboard access. Dragging is not a one-date exception. Both copies of an autumn repeated time share the same weekly rule.

Planned overlaps are rejected with the conflicting programme and time, including overnight and Sunday-to-Monday overlaps. Changes to a programme's name, duration or times apply after its current emission finishes; the calendar and EPG retain that emission's original parameters. Removing a programme stops it immediately. Removing the last programme restores the general Sources rotation. Source removal uses the existing **Finish the current video** preference, subject to programme boundaries.

**Content and boundaries:** each emission shuffles its own pool without repeats within a cycle, then repeats the pool as necessary. Near the end, it chooses an unplayed film that can finish no later than five minutes after the planned end. If none fits, it plays a film cut at the planned end. An overrun delays the next programme's actual start but keeps that programme's original planned end. It does not grant another five minutes to the original programme. A very short next programme can be skipped if the overrun covers its whole window. Unknown-duration and live media are bounded by the planned end rather than receiving an overrun allowance. Failed sources are skipped temporarily and retried; exhaustion produces black and silence, without borrowing another programme's sources.

**Between programmes** selects black with silence (default) or random videos from general Sources. If that pool is empty or unavailable, the output is black with silence. Gap videos are cut at the next programme's planned start. Media and synthetic output remain in bounded RAM buffers; the channel clock continues without viewers and resumes at its shared position after a restart.

**Programme EPG** uses programme names and planned emission times, not the titles of individual films. A continuous unplanned gap is listed as **No planned programme**, regardless of its content. The five-minute playback allowance does not move EPG times. HLS/player buffering adds the same delivery delay as before. Calendar timezone rules, including the clock-change rules below, are reflected in EPG; XMLTV timestamps remain UTC.

#### Timezone and clock changes

Set `TZ` to an IANA timezone in `.env` or Compose, for example `TZ=Europe/Warsaw` (the default), then recreate the container. Invalid timezone names fail startup with a clear error. Timezone data is included in the image. The entire application uses one timezone; there is no separate browser or per-channel schedule timezone setting.

- In spring, a start time that does not exist is skipped for that date. In Warsaw, 02:30 is skipped when the clock jumps from 02:00 to 03:00.
- Programme duration defines its **local clock end**. A 01:00 programme lasting 180 local minutes ends at 04:00: it runs for two actual hours in spring and four in autumn. A nonexistent end time is moved to the first valid local moment after the jump.
- In autumn, a repeated start creates two emissions. For an ambiguous end, use its earliest occurrence later than that emission's start. Where this creates an overlap, the next start in real chronological order interrupts the previous emission, even mid-film, without the five-minute allowance. For example, the first 02:30–03:30 emission ends when the second 02:30 emission starts.
- The calendar displays actual 23-, 24- or 25-hour days with timezone offsets for repeated hours. Rules also support zones whose clock changes are not exactly one hour.

Weekly rules currently have no single-date overrides, priorities, episode ordering or automatic playlist refresh. Use **Refresh** on a source to update its extracted media.

#### Music captions

Select **Music** in a programme's editor to burn artist and song information into every non-live clip, including audio-only sources. Captions are part of the HLS picture in all players. Existing programmes default to off; changing Music keeps the current clip unchanged and applies from the next source in the current emission, including after a restart. General Sources used between programmes, loading screens, synthetic black and live sources have no captions.

The artist appears above the bold song title in white DejaVu Sans with a black outline. The left-aligned block occupies the lower-left safe area (5–45% of picture width, bottom margin 10%). Artist names occupy one line; titles wrap to two lines and overflow ends with an ellipsis. Sizes scale with output resolution; the style is fixed in this version.

Captions appear at seconds 3–13 and from 13 seconds before the clip's scheduled broadcast end until 3 seconds before it. Each interval includes 0.3-second fades. Planned cuts and permitted overruns determine the end; joining or recovering a stream does not restart the caption clock. Overlapping/touching intervals merge, leaving the first and last three seconds clear; clips up to six seconds have no caption. Unknown-length VOD gets the first interval and an end interval only when its scheduled cut is known. Unexpected source failures cannot be anticipated.

The existing playback extraction supplies yt-dlp `artists`/`artist` and `track` metadata. Missing fields fall back conservatively to `Artist - Song` titles (also spaced en/em dashes); conflicting partial metadata is not combined. Recognized bracketed presentation tags such as `(Music Video)`, `[Official Visualiser]`, `(Official Lyric Video)` and `[4K]` are removed from fallback titles. Remix, live, acoustic and other version annotations remain. When both artist and track come from a split title, explicit `ft.`, `feat.` and `featuring` credits in the song portion move to the artist line as `ft.`, including credits in parentheses or square brackets. Structured music metadata stays authoritative and is not rewritten. If no artist is known, only the title appears. Uploaders are not treated as artists. As a last resort, direct media URLs can supply their filename, excluding query parameters; resolved CDN URLs are never used. No additional extraction or media download is required.

#### Programme API

All endpoints below use the existing admin authentication and origin protection. IDs are scoped to their channel. The browser uses the same API.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/channels/{id}/programmes` | Programmes, weekly rules and source statuses |
| `POST /api/channels/{id}/programmes` | Create a programme, initially with an empty source pool |
| `GET /api/channels/{id}/programmes/{programme_id}` | Read one programme |
| `PUT` or `PATCH /api/channels/{id}/programmes/{programme_id}` | Replace name, duration and the complete rule list atomically |
| `DELETE /api/channels/{id}/programmes/{programme_id}` | Stop and remove a programme and its sources |
| `GET` or `POST /api/channels/{id}/programmes/{programme_id}/sources` | List sources or add a URL using `{"url":"https://…"}` |
| `GET /api/channels/{id}/schedule?week=2026-10-19` | Calendar week, actual day boundaries, labelled ticks and programme/gap occurrences |
| `PATCH /api/channels/{id}/settings` | Select `{"gap_mode":"black"}` or `{"gap_mode":"sources"}` |

Example programme body (weekdays use Monday = 0 through Sunday = 6):

```json
{
  "name": "Evening show",
  "music": false,
  "duration_minutes": 60,
  "rules": [
    {"weekdays": [0, 1, 2, 3, 4], "time": "18:00"},
    {"weekdays": [5], "time": "10:00"}
  ]
}
```

Responses assign stable programme/rule IDs. Include existing rule IDs when retaining rules in an update; omit the ID for a new rule. Source IDs work with the existing `/api/sources/{source_id}` deletion, enable/disable and refresh endpoints. Conflicts return HTTP 409; invalid fields return 422. The status response includes `schedule_mode`, `timezone`, `gap_mode`, the nominal `programme`, `actual_programme`, and the current film in `now`. The existing stream, playlist and EPG URLs are unchanged.

Programme responses include the boolean `music`. Creating a programme without it defaults to `false`; omitting it from an update preserves its previous value. Explicit values must be JSON booleans. The calendar drag operation preserves this flag.

### Configuration

```bash
cp .env.example .env
# Edit .env, then:
docker compose up -d
```

Important settings:

| Variable | Description |
| --- | --- |
| `PUBLIC_URL` | Public base URL used in copied playlist links, for example `http://192.168.1.20:8000`. |
| `ADMIN_PASSWORD` | Enables HTTP Basic authentication for the console and API. The username is `admin`. |
| `STREAM_TOKEN` | Adds a separate token to playlist and segment URLs. |
| `TZ` | Shared IANA schedule timezone; `Europe/Warsaw` by default. |
| `ENCODER` | `software` by default; use `vaapi` or `qsv` for hardware encoding. |
| `IDLE_SECONDS` | Stops media processes after this many seconds without HLS requests. Defaults to `25`. |

The default configuration is intended for a trusted local network. Do not expose it directly to the Internet. Use `ADMIN_PASSWORD` and HTTPS at a reverse proxy. The proxy should pass HLS requests without caching and allow response timeouts of at least 120 seconds.

Application state, sources, uploaded cookies, and installed yt-dlp releases are stored in the `tube-data` volume. Rebuilding the image does not remove them. The container runs as UID/GID `10001`, uses a read-only filesystem, and mounts `/tmp` as tmpfs.

## How the channel works

The following flow runs independently for each channel. Stream URLs use `/channels/{id}/index.m3u8`; IDs stay stable when a channel is renamed. Buffer limits are per channel, so RAM and encoding costs grow with the number of channels being watched simultaneously.

```text
links → yt-dlp (playlist metadata) → SQLite → shuffled programme pool
                                                   ↓
                          yt-dlp (video/audio URLs, without downloading)
                                                   ↓
                      FFmpeg (network → H.264 + AAC, channel resolution and FPS)
                                                   ↓
                         local HTTP PUT → bounded RAM buffer
                                                   ↓
                              one HLS playlist → every viewer
```

- yt-dlp uses `--skip-download` and `--no-cache-dir`. FFmpeg reads the URLs returned by yt-dlp directly, including separate YouTube audio and video tracks.
- No video or HLS segment is stored as a file. FFmpeg uploads segments to a loopback endpoint protected by a random secret, and the application keeps the bytes in RAM.
- The published buffer is limited to 12 segments / 192 MiB, with an 24 MiB limit per segment. A separate queue for segments waiting for the manifest is bounded too. These limits cover media buffers, not the complete Python, extractor, or FFmpeg RSS.
- **Target bitrate (kbps)** accepts 100–25000 per channel; leave it empty for automatic selection by resolution. It controls H.264 video bitrate and applies from the next video or stream start.
- Output resolution is set per channel: 480p (854×480), 720p (1280×720), 1080p (1920×1080, default), or 4K (3840×2160). Resolution changes apply from the next video or stream start, including the loading slate and audio-only background. Each channel can select 24, 25, 30, 50, or 60 FPS (default), or **Leave original** to preserve source timestamps and native frame rate, including VFR. Fixed rates duplicate or drop frames without motion interpolation. Settings are saved per channel and apply from the next video or stream start. The loading slate and audio-only background use the selected fixed rate, or 60 FPS in original mode. Keyframes are forced every four seconds; with native timing, the next available frame starts the segment. yt-dlp prefers a direct HTTPS stream up to the selected output resolution for seeking and falls back to other formats, including HLS. Smaller sources are scaled with aspect ratio preserved and black padding where needed.
- VOD is read with limited look-ahead: up to 3 ready segments, usually 12 seconds and at most 24 MiB, are held before publication. A full queue pauses the upload, so the application does not download the whole video in advance. Live sources are read in real time.
- Up to 20 seconds before a video ends, the next source may be prepared. That work is cancelled when viewers disappear, and removed sources are not used during the transition.
- Segments are usually 4 seconds long. HLS playlists expose the latest 6 segments, while older segments remain briefly available for clients. Programme boundaries use `EXT-X-DISCONTINUITY`, and the global media sequence never moves backwards.
- The channel clock starts after the first source is read and continues without viewers. The first viewer starts extraction and FFmpeg at the current programme position; later viewers join the same producer. A `LOADING…` slate with silent H.264/AAC output at the selected resolution is sent while the first video is prepared.
- HLS activity is used to detect when viewing ends. By default, the channel stops its processes and releases RAM 25 seconds after the last manifest or segment request (`IDLE_SECONDS`). The status API does not keep the broadcast alive.
- The viewer count represents active HLS sessions, not a verified number of people. Some clients can temporarily inflate it by abandoning redirected URLs.
- The clock origin, programme catalogue, and shuffle seed are stored as SQLite metadata. After a restart, the current position is calculated from wall-clock time, even if multiple cycles passed without viewers.
- Shuffle has no repeats within a cycle. Duplicate URLs across overlapping playlists are deduplicated, and the next cycle avoids an immediate repeat when other items are available.
- Adding a source does not move the current slot. Removing or disabling a source stops the current programme by default, cancels extraction and FFmpeg, removes old segments, and rebuilds the queue. **Finish the current video when its source is removed** can preserve the current video; it is stored in SQLite and disabled by default.
- Unavailable media is retried with increasing delays while the clock continues. Unknown durations receive a temporary one-hour slot, marked with `~` in the console, and can be corrected during playback. Live sources start at their current edge without seeking.

## yt-dlp releases

The console fetches the latest 25 releases from the official stable and nightly repositories. **Latest available** resolves to a concrete tag, and you can switch channels, install a release, or return from nightly to stable.

The installer downloads the official Python zipapp and `SHA2-256SUMS` over HTTPS, verifies SHA-256, runs `--version`, and only then atomically activates the selection. A failed install leaves the previous release active. Existing processes finish with their current version; new jobs use the selected version. Releases live in `/data/versions`, which contains no media files.

The image includes the initial stable release pinned in `uv.lock`, yt-dlp dependencies, the EJS component, and Deno for JavaScript challenges. Switching the zipapp does not update Deno or EJS dependencies. GitHub API results are cached for 5 minutes, and errors appear in the console.

Availability still depends on the source service. IP blocks, login requirements, geographic restrictions, DRM, or extractor changes can prevent extraction. Nightly may help with extractor changes but cannot bypass access restrictions. The current UI does not manage cookies or service accounts.

## Hardware encoding

CPU encoding (`libx264`) is enabled by default. For VAAPI:

```bash
stat -c '%g' /dev/dri/renderD128 /dev/dri/card0
# Put the matching RENDER_GID and VIDEO_GID values in .env
ENCODER=vaapi docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

For Intel Quick Sync, use `ENCODER=qsv`. `VAAPI_DEVICE` selects another render device. `compose.gpu.yaml` passes `/dev/dri` and the device groups without requiring a privileged container. The image includes Mesa VAAPI and Intel Media drivers.

The right-hand **GPU engine** panel selects a shared encoder and GPU for all channels. It lists accessible render devices from `/dev/dri` for VAAPI (Intel/AMD) and Intel QSV, plus NVIDIA devices detected with `nvidia-smi` for NVENC. Save runs a short FFmpeg encoding test; a failed test leaves the previous configuration intact. Saved settings persist in SQLite, override the environment defaults, and apply from the next video or stream start. CPU encoding remains available when no GPU is exposed. Use **Refresh devices** after changing device access.

NVIDIA NVENC requires a compatible host driver, FFmpeg with `h264_nvenc`, and GPU access inside the container (NVIDIA Container Toolkit with `video,utility,compute` driver capabilities). Passing `/dev/dri` alone does not enable NVENC. AMD uses Mesa VAAPI in the supplied Linux image. See the [FFmpeg hardware device documentation](https://ffmpeg.org/ffmpeg.html) and [NVIDIA FFmpeg guide](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html).

Hardware acceleration covers encoding. Decoding and scaling remain software-based so mixed source formats work. GPU modes must be verified on the target host. VAAPI was validated through the full HLS/Chromium path; QSV returned an MFX session initialization error in the preparation environment.

## Development

The stack is Python 3.11+, FastAPI, Uvicorn, SQLite, yt-dlp, FFmpeg, native JavaScript modules, and a locally served hls.js. There is no runtime CDN dependency. Run exactly one Uvicorn worker because the producer and buffers are shared in that process.

```bash
uv sync --extra test
npm ci && npm run vendor
DATA_DIR=.data uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Keep the Uvicorn port and `PORT` identical: FFmpeg uploads segments to that port over loopback. Docker configures both together.

| Module | Responsibility |
| --- | --- |
| `app/sources.py` | The only source input path: yt-dlp extraction |
| `app/versions.py` | Releases, verification, and atomic switching |
| `app/engine.py` | Channel producer, input seeking, and in-memory HLS |
| `app/buffer.py` | Bounded segment reserve and programme-time publication |
| `app/slate.py` | In-memory loading slate for active sessions |
| `app/timeline.py` | Legacy persistent rotation, shuffle cycles and offset |
| `app/schedule.py` | Pure weekly rules, timezone/DST expansion, gaps and EPG snapshots |
| `app/programme_playback.py` | Persistent content plans and bounded programme overruns |
| `app/programmes_api.py` | Programme administration, conflict validation and weekly calendar API |
| `app/db.py` | Channels, sources, media items, and settings |
| `app/main.py` | API, lifecycle, authorization, and IPTV endpoints |
| `app/static/` | Console, player/API modules, and responsive styles |

The API and UI expose independent channels with separate sources and weekly programmes, plus a shared XMLTV export. Custom ordering of films requires additional implementation. Any extension should keep yt-dlp as the only source interpreter; local media scanners and Plex/Jellyfin libraries are not supported.

## Testing

```bash
uv run pytest -q
npx playwright install chromium
mkdir -p artifacts
node scripts/ui-check.mjs
# On an empty disposable container; uses a real YouTube 4K source:
TUBE_URL=http://127.0.0.1:8002 node scripts/resolution-check.mjs
```

Run integration scripts only against an isolated, empty test instance. They modify sources and settings, and some stop or suspend FFmpeg. Example:

```bash
docker run -d --name tube-iptv-test -p 8001:8000 \
  --add-host host.docker.internal:host-gateway --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 -e IDLE_SECONDS=25 tube-iptv:local
BROWSER_CHECK=1 uv run python scripts/smoke.py
docker rm -fv tube-iptv-test
```

The full-path smoke test generates short local audio/video fixtures, exercises real yt-dlp and FFmpeg, checks codecs with ffprobe, verifies HLS transitions and Chromium playback, and checks that the buffer stops and is released. `TUBE_URL` and `FIXTURE_HOST` can override its addresses.

Programme checks use a separate empty container (example port 8003), local generated fixtures and Chromium:

```bash
docker run -d --name tube-programmes-test -p 127.0.0.1:8003:8000 \
  --add-host host.docker.internal:host-gateway --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 -e TZ=Europe/Warsaw tube-iptv:local
TUBE_URL=http://127.0.0.1:8003 uv run python scripts/programmes-check.py
TUBE_URL=http://127.0.0.1:8003 node scripts/programmes-ui-check.mjs
TUBE_URL=http://127.0.0.1:8003 uv run python scripts/programme-boundaries-check.py
docker rm -fv tube-programmes-test
```

The programme checks modify the isolated instance. They verify synthetic black HLS, programme source activation/removal, repeated media transitions and RAM bounds, plus form editing, dragging one weekday, rejected conflicts, the DST calendar and responsive layouts. The boundary check takes up to three minutes and verifies general-source gap videos are cut for a programme, followed by a return to the gap without resetting HLS. Unit tests cover migration, restart/idle continuity and timezone edge cases. Existing database migration is automatic; back up SQLite using its online backup API before deploying a new image, and retain the data volume during recreation.

Additional checks include `scripts/channels-check.py`, `scripts/timeline-check.py`, `scripts/removal-check.py`, `scripts/loading-check.py`, and `scripts/buffer-check.py`. The channels check verifies simultaneous independent streams, shared yt-dlp, channel removal during playback, and idle shutdown. Read the script headers before running them; several expect a separate test container and local fixture ports.

## Diagnostics and upload ordering

`docker compose logs -f` reports extraction time, the first complete segment, reserve readiness, interrupted uploads, and FFmpeg warnings with the channel ID. The same recent measurements are available under `diagnostics` in `GET /api/channels/{id}/status`; `/api/status` remains an alias for `main`. URLs in FFmpeg warnings are redacted so signed CDN URLs are not logged.

FFmpeg manifests and segments can arrive over different connections and in any order. The server publishes a segment only after receiving both its metadata and complete body, preserving input order. A lost upload creates a discontinuity while retaining its position on the programme timeline.

The internal upload receiver listens only on `127.0.0.1`, chooses a random port, and checks the channel secret. It uses persistent HTTP connections and `100 Continue`, allowing FFmpeg to upload only when the buffer has room. The receiver completes full requests after the sending page closes without writing media to disk.

The next clip begins no earlier than the end of the previous segment. Recovery resumes after the end of already accepted data, including data waiting in RAM; it does not seek back to the wall-clock position. If a source ends early, the existing schedule order is preserved.

## Exporting the image

```bash
docker save tube-iptv:local | gzip > tube-iptv.tar.gz
# On another host:
gunzip -c tube-iptv.tar.gz | docker load
```

## Related projects

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — extraction, releases, and options
- [FFmpeg HLS documentation](https://ffmpeg.org/ffmpeg-formats.html#hls-2) — HLS muxing and HTTP PUT publishing
- [hls.js](https://github.com/video-dev/hls.js) — browser playback
- [Tunarr](https://tunarr.com/configure/transcoding/) — reference architecture for shared channels and transcoding

Tube IPTV has its own implementation and accepts sources through yt-dlp only.

### yt-dlp cookies

Under **Install version**, choose **Upload cookies** and select a `cookies.txt` file in [Mozilla/Netscape format](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) (maximum 2 MiB). JSON exports are not supported. Cookies apply to all channels for subsequent source refreshes and media URL extraction. Refresh an existing failed source after uploading.

The file is stored as `/data/cookies.txt` with owner-only permissions in the `tube-data` volume, surviving container restarts and rebuilds. Uploading another valid file replaces it; rejected uploads keep the previous file. Each extractor gets a private temporary copy, so concurrent jobs cannot overwrite the uploaded cookies. After uploading, **Delete cookies** appears beside **Upload cookies**. Deleting removes the saved file; subsequent extraction jobs run without cookies, while already running jobs finish with their private copies. Expired cookies require a fresh upload. Treat this file as account credentials; the upload uses the same administrator authentication as other settings.
