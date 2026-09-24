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
| <http://localhost:8000/channels/main/index.m3u8> | Main HLS stream |
| <http://localhost:8000/api/docs> | OpenAPI documentation |

For a TV or another device, replace `localhost` with the server's IP address or hostname. Copy the IPTV URL into VLC, Kodi, or another IPTV player. The browser preview starts only after clicking **Watch channel**; opening the console or downloading the playlist does not start media production.

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
| `ENCODER` | `software` by default; use `vaapi` or `qsv` for hardware encoding. |
| `IDLE_SECONDS` | Stops media processes after this many seconds without HLS requests. Defaults to `25`. |

The default configuration is intended for a trusted local network. Do not expose it directly to the Internet. Use `ADMIN_PASSWORD` and HTTPS at a reverse proxy. The proxy should pass HLS requests without caching and allow response timeouts of at least 120 seconds.

Application state, sources, and installed yt-dlp releases are stored in the `tube-data` volume. Rebuilding the image does not remove them. The container runs as UID/GID `10001`, uses a read-only filesystem, and mounts `/tmp` as tmpfs.

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
- The published buffer is limited to 12 segments / 64 MiB, with an 8 MiB limit per segment. A separate queue for segments waiting for the manifest is bounded too. These limits cover media buffers, not the complete Python, extractor, or FFmpeg RSS.
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
| `app/timeline.py` | Persistent clock, shuffle cycles, programme, and offset |
| `app/db.py` | Channels, sources, media items, and settings |
| `app/main.py` | API, lifecycle, authorization, and IPTV endpoints |
| `app/static/` | Console, player/API modules, and responsive styles |

The API and UI expose independent channels with separate sources and schedules. Custom ordering and XMLTV require additional implementation. Any extension should keep yt-dlp as the only source interpreter; local media scanners and Plex/Jellyfin libraries are not supported.

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
