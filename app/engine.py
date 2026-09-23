"""One producer per channel. FFmpeg uploads HLS over loopback; media never touches disk."""
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import logging
import re
import secrets
import time
from urllib.parse import quote
from . import config
from .process import stop_process
from .timeline import Timeline, known_duration

MAX_SEGMENT = 8 * 1024 * 1024
MAX_BUFFER = 64 * 1024 * 1024
logger = logging.getLogger("uvicorn.error")


@dataclass
class Segment:
    sequence: int
    clip: str
    duration: float
    data: bytes
    discontinuity: int
    program_time: float | None = None
    source_url: str | None = None


def ffmpeg_command(formats, info, destination, encoder=None, offset=0.0, duration=None):
    encoder = encoder or config.ENCODER
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-threads", "2", "-filter_threads", "2"]
    if encoder == "vaapi":
        cmd += ["-vaapi_device", config.VAAPI_DEVICE]
    if encoder == "qsv":
        cmd += ["-init_hw_device", f"qsv=hw,child_device={config.VAAPI_DEVICE}", "-filter_hw_device", "hw"]
    video_index, audio_index = None, None
    for index, fmt in enumerate(formats):
        headers = {**info.get("http_headers", {}), **fmt.get("http_headers", {})}
        header_string = "".join(f"{k}: {v}\r\n" for k, v in headers.items()
                                if not any(c in str(k) + str(v) for c in "\r\n"))
        cmd += ["-re", "-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "3", "-protocol_whitelist", "http,https,tcp,tls,crypto"]
        if header_string:
            cmd += ["-headers", header_string]
        if offset > 0 and not info.get("is_live"):
            cmd += ["-ss", f"{offset:.6f}"]
        cmd += ["-i", fmt["url"]]
        if fmt.get("vcodec") != "none" and video_index is None:
            video_index = index
        if fmt.get("acodec") != "none" and audio_index is None:
            audio_index = index
    if video_index is None:
        cmd += ["-f", "lavfi", "-i", "color=c=0x161a18:s=1920x1080:r=25"]
        video_index = len(formats)
    if audio_index is None:
        audio_index = len(formats) + (video_index == len(formats))
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    # Fit the display aspect ratio (including non-square source pixels), then
    # normalize to square pixels before padding the fixed output canvas.
    filters = ("scale=w='trunc(min(1920,1080*dar)/2)*2':h='trunc(min(1080,1920/dar)/2)*2',"
               "setsar=1,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,fps=25")
    cmd += ["-map", f"{video_index}:v:0", "-map", f"{audio_index}:a:0", "-shortest"]
    if encoder == "vaapi":
        cmd += ["-vf", filters + ",format=nv12,hwupload", "-c:v", "h264_vaapi"]
    elif encoder == "qsv":
        cmd += ["-vf", filters + ",format=nv12,hwupload=extra_hw_frames=64", "-c:v", "h264_qsv"]
    else:
        cmd += ["-vf", filters, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", "2"]
    if duration is not None:
        cmd += ["-t", f"{max(0.04, duration):.6f}"]
    cmd += ["-b:v", "4500k", "-maxrate", "6000k", "-bufsize", "12000k", "-g", "100", "-keyint_min", "100",
            "-sc_threshold", "0", "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-af", "aresample=async=1:first_pts=0", "-max_muxing_queue_size", "1024",
            "-f", "hls", "-hls_time", "4", "-hls_list_size", "6", "-hls_flags", "independent_segments",
            "-hls_segment_filename", destination + "/%06d.ts", "-method", "PUT",
            "-http_persistent", "1", "-timeout", "10", destination + "/index.m3u8"]
    return cmd


class Channel:
    def __init__(self, channel_id, db, sources):
        self.id, self.db, self.sources = channel_id, db, sources
        self.secret = secrets.token_urlsafe(32)
        self.segments = deque()
        self.pending = {}
        self.sequence = 0
        self.discontinuity = 0
        self.last_clip = None
        self.clip = None
        self.published = set()
        self.bytes = 0
        self.viewers = {}
        self.waiters = 0
        self.task = None
        self.monitor = None
        self.process = None
        self.timeline = Timeline(db, channel_id) if db else None
        self.clock_task = None
        self.clip_time = None
        self.clip_duration = 0.0
        self.ingest_clip = None
        self.announced = {}
        self.aborted = set()
        self.window_start = 0
        self.last_input_segment = -1
        self.gap_pending = False
        self.stream_revision = 0
        self.retained_clip = None
        self.metrics = {}
        self.ffmpeg_started = None
        self.loading = None
        self.segment_sink = None
        self.state = "idle"
        self.now = None
        self.error = None
        self.events = deque(maxlen=30)
        self.changed = asyncio.Condition()
        self.lifecycle = asyncio.Lock()

    @property
    def finish_current_on_remove(self):
        return bool(self.db and self.db.setting('finish_current_on_remove', False))

    def retained_current(self):
        return (self.finish_current_on_remove and self.process is not None
                and self.process.returncode is None and self.now is not None)

    def available(self):
        return (bool(self.db.media(self.id)) or self.retained_current()
                or (self.finish_current_on_remove and any(s.clip == self.retained_clip for s in self.segments)))

    async def reconcile_sources(self):
        """Revoke removed media, including an in-flight extraction or encoder."""
        if not self.db:
            return
        async with self.lifecycle:
            allowed = {item['url'] for item in self.db.media(self.id)}
            removed_current = self.now and self.now['url'] not in allowed
            retain = removed_current and self.retained_current()
            if retain:
                self.retained_clip = self.clip
            stale_buffer = any(s.source_url and s.source_url not in allowed
                               and not (self.finish_current_on_remove and s.clip == self.retained_clip) for s in self.segments)
            if (removed_current and not retain) or (self.state == "ended" and allowed):
                await self.stop()
                self.stream_revision += 1
                self.error = None
                self.event('Removed source stopped immediately · queue rebuilt')
                self.scheduled()
                if allowed and (self.viewers or self.waiters):
                    self.task = asyncio.create_task(self.run())
            elif stale_buffer:
                self.segments.clear()
                self.bytes = 0
                self.stream_revision += 1
                self.event('Removed source segments discarded · queue rebuilt')
                self.scheduled()
            else:
                self.scheduled()

    def scheduled(self):
        return self.timeline.sync(self.db.media(self.id),
                                  preserve_removed=self.retained_current()) if self.timeline else None

    async def clock_loop(self):
        while True:
            await self.reconcile_sources()
            await asyncio.sleep(1)

    def event(self, text):
        self.events.appendleft({"time": time.time(), "text": text})
        logger.info("channel=%s %s", self.id, text)

    async def touch(self, viewer):
        async with self.lifecycle:
            self.viewers[viewer] = time.monotonic()
            if (self.task is None or self.task.done()) and not (self.state == "ended" and not self.db.media(self.id)):
                self.task = asyncio.create_task(self.run())
            if self.monitor is None or self.monitor.done():
                self.monitor = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            self.viewers = {key: stamp for key, stamp in self.viewers.items() if now - stamp < config.IDLE_SECONDS}
            if not self.viewers and not self.waiters:
                async with self.lifecycle:
                    if self.viewers or self.waiters:
                        continue
                    await self.stop()
                return

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.segments.clear()
        self.pending.clear()
        self.announced.clear()
        self.aborted.clear()
        self.bytes = 0
        self.now = None
        self.retained_clip = None
        self.state = "idle"
        self.event("Stream stopped · RAM released · channel clock continues")
        async with self.changed:
            self.changed.notify_all()

    async def close(self):
        if self.clock_task:
            self.clock_task.cancel()
            await asyncio.gather(self.clock_task, return_exceptions=True)
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        await self.stop()

    async def run(self):
        started = time.monotonic()
        self.metrics = {"resolve_seconds": None, "last_resolve_seconds": None,
                        "first_segment_seconds": None, "ready_seconds": None,
                        "lost_segments": 0, "upload_aborts": 0, "last_segment_at": None}
        self.event("Viewer connected · tuning into the channel clock")
        from .slate import LoadingSlate
        self.loading = LoadingSlate(self)
        self.loading.start()
        failures = 0
        completed = None
        try:
            while True:
                slot = self.scheduled()
                if not slot:
                    self.state = "empty"
                    self.error = "Add at least one available, enabled source"
                    await asyncio.sleep(2)
                    continue
                if slot.key == completed:
                    await asyncio.sleep(min(0.5, max(0.04, slot.ends_at - time.time())))
                    continue
                item = slot.item
                self.state = "buffering"
                self.now = item
                self.clip = secrets.token_hex(12)
                self.pending.clear()
                self.published.clear()
                self.clip_duration = 0.0
                initial = self.clip_duration
                try:
                    resolving = time.monotonic()
                    info, formats = await self.sources.resolve(item["url"])
                    elapsed = round(time.monotonic() - resolving, 3)
                    self.metrics['last_resolve_seconds'] = elapsed
                    if self.metrics['resolve_seconds'] is None:
                        self.metrics['resolve_seconds'] = elapsed
                    self.event(f"Source resolved in {elapsed:.2f}s")
                    self.event("Input formats: " + ", ".join(
                        f"{f.get('format_id', '?')} ({f.get('protocol', '?')}, {f.get('vcodec', '?')})"
                        for f in formats))
                    if known_duration(info.get("duration")) and not info.get("is_live"):
                        self.db.execute("UPDATE media SET duration=? WHERE url=?", (info["duration"], item["url"]))
                    current = self.scheduled()
                    # Extraction may cross a programme boundary. Never play a stale slot.
                    if not current or current.key != slot.key:
                        continue
                    slot = current
                    offset = slot.offset(time.time())
                    remaining = slot.ends_at - time.time()
                    if remaining < 0.25:
                        completed = slot.key
                        continue
                    self.clip_time = slot.starts_at + offset
                    destination = f"http://127.0.0.1:{config.PORT}/internal/{self.secret}/{self.clip}"
                    command = ffmpeg_command(formats, info, destination, offset=offset, duration=remaining)
                    self.event(f"On air: {item['title']} · joining at {int(offset)}s")
                    self.ffmpeg_started = time.monotonic()
                    self.startup_started = started
                    self.process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE, start_new_session=True)
                    tail = deque(maxlen=12)
                    async for line in self.process.stderr:
                        message = re.sub(r"https?://\S+", "[URL]", line.decode(errors="replace").strip())
                        tail.append(message)
                        logger.warning("channel=%s ffmpeg: %s", self.id, message)
                    code = await self.process.wait()
                    if code or self.clip_duration == initial:
                        raise RuntimeError("FFmpeg: " + ("\n".join(tail)[-1200:] or f"No segments produced (exit {code}, seek {offset:.1f}s)"))
                    if slot.item['estimated'] and not info.get('is_live'):
                        # Some generic extractors do not report duration. Learn it at EOF,
                        # without probing/downloading anything while there are no viewers.
                        self.db.execute("UPDATE media SET duration=? WHERE url=?",
                                        (offset + self.clip_duration, item['url']))
                    completed = slot.key
                    self.scheduled()
                    failures = 0
                    if self.clip == self.retained_clip and not self.db.media(self.id):
                        self.state = "ended"
                        self.now = None
                        self.event("Retained video finished · final HLS segments available")
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    self.error = str(exc)[-1800:]
                    self.state = "error"
                    self.event("Source unavailable; retrying at the current channel time: " + item["title"] + " · " + self.error)
                    await asyncio.sleep(min(failures * 2, 20))
                finally:
                    if self.process:
                        await stop_process(self.process)
                        self.process = None
        finally:
            if self.loading:
                await self.loading.close()
                self.loading = None
            self.clip = None
            self.pending.clear()

    def begin_ingest(self, clip):
        if self.ingest_clip != clip:
            self.ingest_clip = clip
            self.last_input_segment = -1
            self.gap_pending = False
            self.announced.clear()
            self.aborted.clear()
            self.window_start = 0
            self.clip_duration = 0.0

    async def upload_aborted(self, clip, filename):
        if clip != self.clip or not filename.endswith('.ts'):
            return
        self.begin_ingest(clip)
        self.aborted.add(int(filename.removesuffix('.ts')))
        await self.publish_ready()

    async def ingest(self, clip, filename, body):
        if self.loading and clip == self.loading.clip:
            return await self.loading.ingest(filename, body)
        if clip != self.clip:
            return False
        self.begin_ingest(clip)
        if filename.endswith(".ts"):
            index = int(filename.removesuffix('.ts'))
            if index <= self.last_input_segment:
                return True
            if len(body) > MAX_SEGMENT or len(self.pending) >= 8:
                raise ValueError("Segment buffer limit exceeded")
            self.pending[filename] = body
            self.aborted.discard(index)
        elif filename == "index.m3u8":
            duration = None
            indices = []
            for line in body.decode().splitlines():
                if line.startswith("#EXTINF:"):
                    duration = float(line.split(":", 1)[1].split(",")[0])
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError('Invalid segment duration')
                elif line and not line.startswith("#") and duration is not None:
                    name = line.rsplit("/", 1)[-1]
                    index = int(name.removesuffix('.ts'))
                    indices.append(index)
                    if index > self.last_input_segment:
                        self.announced[index] = (name, duration)
                    duration = None
            if indices:
                self.window_start = max(self.window_start, min(indices))
        else:
            return False
        await self.publish_ready()
        return True

    async def publish_ready(self):
        # Upload and playlist use separate HTTP connections. Their handlers may
        # complete in either order: never treat an announced, in-flight upload
        # as missing, and never publish later segments ahead of it.
        while True:
            index = self.last_input_segment + 1
            entry = self.announced.get(index)
            data = self.pending.get(entry[0]) if entry else None
            lost = (index in self.aborted and entry is not None) or (index < self.window_start and data is None)
            if lost:
                duration = entry[1] if entry else 4.0
                self.clip_duration += duration
                self.last_input_segment = index
                self.announced.pop(index, None)
                self.aborted.discard(index)
                self.gap_pending = True
                self.metrics['lost_segments'] = self.metrics.get('lost_segments', 0) + 1
                self.event(f'HLS segment {index} lost; preserving its time and marking a discontinuity')
                continue
            if entry is None or data is None:
                return
            name, duration = self.announced.pop(index)
            self.pending.pop(name)
            self.aborted.discard(index)
            self.last_input_segment = index
            segment = Segment(0, self.clip, duration, data, 0,
                              self.clip_time + self.clip_duration if self.clip_time is not None else None,
                              self.now['url'] if self.now else None)
            self.clip_duration += duration
            if self.segment_sink:
                await self.segment_sink(segment)
                continue
            if self.loading:
                self.loading.disable()
            self.append_segment(segment, self.gap_pending)
            self.gap_pending = False
            self.state = "live"
            self.error = None
            self.metrics['last_segment_at'] = time.time()
            if self.ffmpeg_started is not None and self.metrics.get('first_segment_seconds') is None:
                elapsed = round(time.monotonic() - self.ffmpeg_started, 3)
                self.metrics['first_segment_seconds'] = elapsed
                self.event(f'First HLS segment ready {elapsed:.2f}s after FFmpeg launch')
            if self.ffmpeg_started is not None and sum(s.source_url is not None for s in self.segments) >= 2 and self.metrics.get('ready_seconds') is None:
                elapsed = round(time.monotonic() - self.startup_started, 3)
                self.metrics['ready_seconds'] = elapsed
                self.event(f'Stream ready in {elapsed:.2f}s (two segments in RAM)')
            async with self.changed:
                self.changed.notify_all()

    def append_segment(self, segment, gap=False):
        if self.last_clip is not None and (self.last_clip != segment.clip or gap):
            self.discontinuity += 1
        segment.sequence = self.sequence
        segment.discontinuity = self.discontinuity
        self.sequence += 1
        self.last_clip = segment.clip
        self.segments.append(segment)
        self.bytes += len(segment.data)
        while len(self.segments) > 12 or self.bytes > MAX_BUFFER:
            self.bytes -= len(self.segments.popleft().data)

    def manifest(self, viewer, token=""):
        segments = list(self.segments)[-6:]
        if not segments:
            return None
        suffix = "?viewer=" + quote(viewer) + ("&token=" + quote(token) if token else "")
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS",
                 f"#EXT-X-TARGETDURATION:{max(4, math.ceil(max(s.duration for s in segments)))}",
                 f"#EXT-X-MEDIA-SEQUENCE:{segments[0].sequence}",
                 f"#EXT-X-DISCONTINUITY-SEQUENCE:{segments[0].discontinuity}"]
        previous = segments[0].discontinuity
        for segment in segments:
            if segment.discontinuity != previous:
                lines.append("#EXT-X-DISCONTINUITY")
            previous = segment.discontinuity
            if segment.program_time is not None:
                timestamp = datetime.fromtimestamp(segment.program_time, timezone.utc).isoformat(timespec="milliseconds")
                lines.append("#EXT-X-PROGRAM-DATE-TIME:" + timestamp)
            lines += [f"#EXTINF:{segment.duration:.6f},", f"segments/{segment.sequence}.ts{suffix}"]
        if self.state == "ended":
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def status(self):
        slot = self.scheduled()
        now = ({**slot.item, "starts_at": slot.starts_at, "ends_at": slot.ends_at,
                "offset": slot.offset(time.time())} if slot else None)
        return {"state": self.state, "viewers": len(self.viewers), "now": now,
                "buffer_bytes": self.bytes, "segments": len(self.segments), "error": self.error,
                "events": list(self.events), "encoder": config.ENCODER,
                "finish_current_on_remove": self.finish_current_on_remove,
                "stream_revision": self.stream_revision, "stream_available": self.available() if self.db else False,
                "diagnostics": self.metrics}
