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
from . import config, gpu, music
from .process import stop_process
from .video import RESOLUTIONS
from .timeline import Timeline, known_duration
from .programme_playback import ProgrammeTimeline, BLACK_URL

MAX_SEGMENT = 24 * 1024 * 1024
MAX_BUFFER = 192 * 1024 * 1024
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


def ffmpeg_command(formats, info, destination, encoder=None, offset=0.0, duration=None, fps=60, resolution="1080p", device=None, target_bitrate=None, music_caption=None, music_end=None):
    width, height, bitrate, maxrate = RESOLUTIONS[resolution]
    if target_bitrate is not None:
        bitrate = target_bitrate
        maxrate = min(30000, (bitrate * 4 + 2) // 3)
    encoder = encoder or config.ENCODER
    device = device if device is not None else config.VAAPI_DEVICE
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-threads", "2", "-filter_threads", "2"]
    if encoder == "vaapi":
        cmd += ["-vaapi_device", device]
    if encoder == "qsv":
        cmd += ["-init_hw_device", f"qsv=hw,child_device={device},child_device_type=vaapi", "-filter_hw_device", "hw"]
    synthetic_fps = 60 if fps == "original" else fps
    video_index, audio_index = None, None
    for index, fmt in enumerate(formats):
        headers = {**info.get("http_headers", {}), **fmt.get("http_headers", {})}
        header_string = "".join(f"{k}: {v}\r\n" for k, v in headers.items()
                                if not any(c in str(k) + str(v) for c in "\r\n"))
        if info.get("is_live"):
            cmd += ["-re"]
        cmd += ["-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1",
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
        cmd += ["-f", "lavfi", "-i", f"color=c={'black' if info.get('synthetic_black') else '0x161a18'}:s={width}x{height}:r={synthetic_fps}"]
        video_index = len(formats)
    if audio_index is None:
        audio_index = len(formats) + (video_index == len(formats))
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    # Fit the display aspect ratio (including non-square source pixels), then
    # normalize to square pixels before padding the fixed output canvas.
    filters = (f"scale=w='trunc(min({width},{height}*dar)/2)*2':h='trunc(min({height},{width}/dar)/2)*2',"
               f"setsar=1,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    if fps != "original":
        filters += f",fps={fps}"
    if music_caption and not info.get('is_live') and not info.get('synthetic_black'):
        overlay = music.filters(music_caption, width, height, offset, music_end)
        if overlay:
            filters += ',' + overlay
    cmd += ["-map", f"{video_index}:v:0", "-map", f"{audio_index}:a:0", "-shortest"]
    if encoder == "vaapi":
        cmd += ["-vf", filters + ",format=nv12,hwupload", "-c:v", "h264_vaapi"]
    elif encoder == "qsv":
        cmd += ["-vf", filters + ",format=nv12,hwupload=extra_hw_frames=64", "-c:v", "h264_qsv"]
    elif encoder == "nvenc":
        cmd += ["-vf", filters + ",format=nv12", "-c:v", "h264_nvenc", "-gpu", device]
    else:
        cmd += ["-vf", filters, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", "2"]
    if fps == "original":
        # Preserve source timestamps, including VFR and fractional frame rates.
        # Use the MPEG-TS clock so encoder rounding does not flatten VFR timing.
        cmd += ["-fps_mode:v", "vfr", "-enc_time_base:v", "1:90000"]
    else:
        cmd += ["-fps_mode:v", "cfr"]
    if duration is not None:
        cmd += ["-t", f"{max(0.04, duration):.6f}"]
    # Bound VBV bursts so four-second segments fit the 24 MiB ingest cap.
    buffer_rate = min(30000, maxrate * 2) if target_bitrate is not None else (maxrate if resolution == "4k" else maxrate * 2)
    # With native timing, force keyframes by elapsed time, not frame count.
    cmd += ["-b:v", f"{bitrate}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{buffer_rate}k",
            "-g", str(10000 if fps == "original" else fps * 4), "-keyint_min", "1",
            "-sc_threshold", "0", "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-af", "aresample=async=1:first_pts=0", "-max_muxing_queue_size", "1024",
            "-f", "hls", "-hls_time", "4", "-hls_list_size", "6", "-hls_flags", "independent_segments",
            "-hls_segment_filename", destination + "/%06d.ts", "-method", "PUT",
            # Keep connections open; UploadServer's 100-continue handshake
            # gates segment bodies on RAM capacity, including final uploads.
            "-http_persistent", "1", "-timeout", "20", destination + "/index.m3u8"]
    return cmd


class Channel:
    def __init__(self, channel_id, db, sources):
        self.id, self.db, self.sources = channel_id, db, sources
        self.secret = secrets.token_urlsafe(32)
        self.upload_base = f'http://127.0.0.1:{config.PORT}/internal/{self.secret}'
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
        self.rotation = Timeline(db, channel_id) if db else None
        self.programme_timeline = ProgrammeTimeline(db, channel_id) if db else None
        self.timeline = self.rotation
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
        self.closed = False
        self.now = None
        self.producing_slot = None
        self.error = None
        self.events = deque(maxlen=30)
        self.changed = asyncio.Condition()
        self.lifecycle = asyncio.Lock()
        self.ingest_lock = asyncio.Lock()
        self.prefetch_task = None
        self.prefetch_key = None
        self.media_buffer = None
        self.ingest_finished = asyncio.Event()
        self.final_input_segment = None

    @property
    def resolution(self):
        return self.db.setting(f'resolution:{self.id}', '1080p') if self.db else '1080p'

    @property
    def target_bitrate(self):
        return self.db.setting(f'target_bitrate:{self.id}') if self.db else None

    @property
    def fps(self):
        return self.db.setting(f'fps:{self.id}', 60) if self.db else 60

    @property
    def finish_current_on_remove(self):
        return bool(self.db and self.db.setting(f'finish_current_on_remove:{self.id}',
                    self.db.setting('finish_current_on_remove', False) if self.id == 'main' else False))

    def retained_current(self):
        return (self.finish_current_on_remove and self.now is not None and (
            (self.process is not None and self.process.returncode is None)
            or (self.task is not None and not self.task.done()
                and any(s.clip == self.clip for s in self.segments))))

    def available(self):
        return (bool(self.db.programmes(self.id)) or bool(self.db.media(self.id)) or self.retained_current()
                or (self.finish_current_on_remove and any(s.clip == self.retained_clip for s in self.segments)))

    async def reconcile_sources(self):
        """Revoke removed media, including an in-flight extraction or encoder."""
        if not self.db:
            return
        async with self.lifecycle:
            if self.closed:
                return
            scheduled = self.scheduled()
            if self.timeline is self.programme_timeline and self.producing_slot:
                # A producer may already be encoding the next occurrence into RAM.
                # Compare at its media start, not at the earlier public wall clock.
                scheduled = self.timeline.position(max(time.time(), self.producing_slot.starts_at))
            programme_id = self.now.get('programme_id') if self.now else None
            allowed = {item['url'] for item in self.db.media(self.id, programme_id)}
            if self.db.programmes(self.id):
                allowed.add(BLACK_URL)
            if self.prefetch_key:
                next_slot = self.timeline.position(self.prefetch_key[0][1] + .001)
                if not next_slot or next_slot.key != self.prefetch_key[0]:
                    await self.cancel_prefetch()
            gap_changed = (self.now and self.timeline is self.programme_timeline
                           and self.now.get('programme_id') is None and scheduled
                           and (scheduled.item.get('programme_id') is not None
                                or self.now.get('programme_ends_at') != scheduled.item.get('programme_ends_at')))
            removed_current = self.now and (self.now['url'] not in allowed or gap_changed
                or (self.now['url'] == BLACK_URL and scheduled and scheduled.item['url'] != BLACK_URL))
            retain = removed_current and not gap_changed and self.now['url'] != BLACK_URL and self.retained_current()
            if retain:
                self.retained_clip = self.clip
            buffer_allowed = {m['url'] for m in self.db.media(self.id, all_sources=True)} | ({BLACK_URL} if self.db.programmes(self.id) else set())
            stale_buffer = any(s.source_url and s.source_url not in buffer_allowed
                               and not (self.finish_current_on_remove and s.clip == self.retained_clip) for s in self.segments)
            if (removed_current and not retain) or (self.state == "ended" and allowed):
                await self.stop()
                self.stream_revision += 1
                self.error = None
                self.event('Removed source stopped immediately · queue rebuilt')
                self.scheduled()
                if self.available() and (self.viewers or self.waiters):
                    self.task = asyncio.create_task(self.run())
            elif stale_buffer:
                self.segments.clear()
                self.bytes = 0
                self.stream_revision += 1
                self.event('Removed source segments discarded · queue rebuilt')
                self.scheduled()
            else:
                self.scheduled()

    def scheduled(self, now=None):
        if not self.db:
            return None
        self.timeline = self.programme_timeline if self.db.programmes(self.id) else self.rotation
        return self.timeline.sync(self.db.media(self.id), now=now, preserve_removed=self.retained_current())

    async def programmes_changed(self, deleted=None):
        async with self.lifecycle:
            was_programmed = self.timeline is self.programme_timeline
            self.scheduled()
            changed_mode = was_programmed != (self.timeline is self.programme_timeline)
            if changed_mode or (deleted and self.now and self.now.get('programme_id') == deleted):
                await self.stop()
                self.stream_revision += 1
                if self.available() and (self.viewers or self.waiters):
                    self.task = asyncio.create_task(self.run())
            await self.cancel_prefetch()

    def advance_after_source(self, slot, reason):
        """Rebase the schedule when a source ends before its allotted slot."""
        if not self.timeline:
            return False
        following = self.timeline.advance(slot)
        if not following or following.key == slot.key:
            return False
        self.event(f"{reason}; switching immediately to {following.item['title']}")
        return True

    async def clock_loop(self):
        while True:
            await self.reconcile_sources()
            await asyncio.sleep(1)

    def event(self, text):
        self.events.appendleft({"time": time.time(), "text": text})
        logger.info("channel=%s %s", self.id, text)

    async def touch(self, viewer):
        async with self.lifecycle:
            if self.closed:
                return
            self.viewers[viewer] = time.monotonic()
            if (self.task is None or self.task.done()) and not (self.state == "ended" and not self.db.programmes(self.id) and not self.db.media(self.id)):
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
        self.producing_slot = None
        self.retained_clip = None
        self.state = "idle"
        self.event("Stream stopped · RAM released · channel clock continues")
        async with self.changed:
            self.changed.notify_all()

    async def close(self):
        self.closed = True
        if self.clock_task:
            self.clock_task.cancel()
            await asyncio.gather(self.clock_task, return_exceptions=True)
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        async with self.lifecycle:
            await self.stop()

    async def cancel_prefetch(self):
        if self.prefetch_task:
            self.prefetch_task.cancel()
            await asyncio.gather(self.prefetch_task, return_exceptions=True)
        self.prefetch_task = None
        self.prefetch_key = None

    async def prefetch_next(self, slot):
        # Resolve only the next source, shortly before the boundary. No media
        # is fetched here, and cancellation reaps yt-dlp when viewers leave.
        await asyncio.sleep(max(0, slot.ends_at - time.time() - 20))
        current = self.scheduled()
        if not current or current.key != slot.key:
            return None
        next_slot = self.timeline.position(slot.ends_at + .001)
        if not next_slot or next_slot.item['url'] not in {m['url'] for m in self.db.media(self.id, next_slot.item.get('programme_id'))}:
            return None
        resolution = self.resolution
        self.prefetch_key = (next_slot.key, resolution)
        try:
            result = await self.sources.resolve(next_slot.item['url'], resolution=resolution)
            self.event('Next source resolved ahead of programme boundary')
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            # The normal resolution/retry path reports errors if still relevant.
            return None

    async def run(self):
        started = time.monotonic()
        self.metrics = {"resolve_seconds": None, "last_resolve_seconds": None,
                        "first_segment_seconds": None, "ready_seconds": None,
                        "lost_segments": 0, "upload_aborts": 0, "last_segment_at": None}
        self.event("Viewer connected · tuning into the channel clock")
        from .slate import LoadingSlate
        self.loading = LoadingSlate(self)
        self.loading.start()
        from .buffer import MediaBuffer
        self.media_buffer = MediaBuffer(self.publish_segment)
        failures = 0
        skipped_urls = set()
        recovery_attempts = {}
        resume = None
        completed = None
        try:
            while True:
                slot = self.scheduled()
                programmed = self.timeline is self.programme_timeline
                if programmed:
                    position = max(time.time(), self.media_buffer.tail_deadline or 0)
                    slot = self.timeline.position(position)
                if not programmed and completed is not None and self.clip == self.retained_clip and not self.db.media(self.id):
                    self.state = "ended"
                    self.now = None
                    self.event("Retained video finished · final HLS segments available")
                    return
                if not slot:
                    self.state = "empty"
                    self.error = "Add at least one available, enabled source"
                    await asyncio.sleep(2)
                    continue
                if slot.key == completed:
                    await asyncio.sleep(min(0.5, max(0.04, slot.ends_at - time.time())))
                    continue
                item = slot.item
                resume_offset = resume[1] if resume and resume[0] == slot.key else None
                resume = None
                offset = resume_offset if resume_offset is not None else slot.offset(max(time.time(), self.media_buffer.tail_deadline or 0) if programmed else time.time())
                self.state = "buffering"
                self.now = item
                self.producing_slot = slot
                self.clip = secrets.token_hex(12)
                self.ingest_finished.clear()
                self.pending.clear()
                self.published.clear()
                self.clip_duration = 0.0
                initial = self.clip_duration
                try:
                    resolving = time.monotonic()
                    resolution = self.resolution
                    resolved = None
                    if self.prefetch_task and self.prefetch_key == (slot.key, resolution):
                        resolved = await self.prefetch_task
                    await self.cancel_prefetch()
                    if item['url'] == BLACK_URL:
                        info, formats = {'synthetic_black': True}, []
                    elif resolved:
                        info, formats = resolved
                    else:
                        job = self.sources.resolve(item['url'], resolution=resolution)
                        info, formats = await asyncio.wait_for(job, max(.05, min(90, slot.ends_at - time.time()))) if programmed else await job
                    elapsed = round(time.monotonic() - resolving, 3)
                    self.metrics['last_resolve_seconds'] = elapsed
                    if self.metrics['resolve_seconds'] is None:
                        self.metrics['resolve_seconds'] = elapsed
                    self.event(f"Source resolved in {elapsed:.2f}s")
                    self.event("Input formats: " + ", ".join(
                        f"{f.get('format_id', '?')} ({f.get('protocol', '?')}, {f.get('vcodec', '?')})"
                        for f in formats))
                    if programmed and item['url'] != BLACK_URL:
                        self.programme_timeline.mark_live(item['url'], now=max(time.time(), self.media_buffer.tail_deadline or 0), is_live=bool(info.get('is_live')))
                    if known_duration(info.get("duration")) and not info.get("is_live"):
                        self.db.update_duration(self.id, item['url'], info['duration'])
                    current = self.scheduled()
                    if programmed:
                        current = self.timeline.position(max(time.time(), self.media_buffer.tail_deadline or 0))
                    # Extraction may cross a programme boundary. Never play a stale slot.
                    if not current or current.key != slot.key:
                        continue
                    slot = current
                    self.now = item = slot.item
                    self.producing_slot = slot
                    offset = resume_offset if resume_offset is not None else slot.offset(max(time.time(), self.media_buffer.tail_deadline or 0) if programmed else time.time())
                    remaining = slot.ends_at - max(time.time(), self.media_buffer.tail_deadline or 0) if programmed else slot.item['duration'] - offset
                    if programmed and info.get('is_live'):
                        remaining = min(remaining, slot.item['programme_ends_at'] - max(time.time(), self.media_buffer.tail_deadline or 0))
                    if remaining < 0.25:
                        completed = slot.key
                        continue
                    self.clip_time = max(time.time(), self.media_buffer.tail_deadline or 0)
                    self.prefetch_task = asyncio.create_task(self.prefetch_next(slot))
                    destination = f"{self.upload_base}/{self.clip}"
                    music_caption = (music.caption(info, item) if item.get('music')
                                     and item['url'] != BLACK_URL and not info.get('is_live') else None)
                    music_end = (slot.ends_at - slot.starts_at if not item.get('estimated')
                                 or slot.ends_at >= item.get('programme_ends_at', float('inf')) else None)
                    command = ffmpeg_command(formats, info, destination, offset=offset, duration=remaining,
                                             fps=self.fps, resolution=resolution, target_bitrate=self.target_bitrate,
                                             music_caption=music_caption, music_end=music_end, **gpu.settings(self.db))
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
                    if code == 0:
                        # Persistent HTTP uploads can still be in flight after
                        # the encoder exits. Only ENDLIST acknowledges that
                        # the final segment has entered our bounded queue.
                        try:
                            await asyncio.wait_for(self.ingest_finished.wait(), 20)
                        except asyncio.TimeoutError as exc:
                            raise RuntimeError(f'Final HLS upload was not acknowledged within 20s '
                                               f'(last={self.last_input_segment}, final={self.final_input_segment}, '
                                               f'pending={list(self.pending)}, announced={list(self.announced)})') from exc
                    if code or self.clip_duration == initial:
                        raise RuntimeError("FFmpeg: " + ("\n".join(tail)[-1200:] or f"No segments produced (exit {code}, seek {offset:.1f}s)"))
                    produced = self.clip_duration
                    shortfall = remaining - produced
                    self.event(f"FFmpeg finished after {produced:.2f}s of {remaining:.2f}s allocated")
                    if shortfall > 6 and not slot.item['estimated'] and not info.get('is_live'):
                        attempts = recovery_attempts.get(slot.key, 0)
                        if attempts < 1:
                            recovery_attempts[slot.key] = attempts + 1
                            resume = (slot.key, offset + produced)
                            self.event(f"Source ended {shortfall:.2f}s early; refreshing direct URLs and resuming")
                            continue
                        self.event(f"Source remained {shortfall:.2f}s short after recovery")
                    if slot.item['estimated'] and not info.get('is_live'):
                        # Some generic extractors do not report duration. Learn it at EOF,
                        # without probing/downloading anything while there are no viewers.
                        self.db.update_duration(self.id, item['url'], offset + self.clip_duration)
                    # Do not wait for the scheduled boundary or drain the RAM
                    # reserve.  Its final segments remain in FIFO order while
                    # the next encoder is already preparing a replacement.
                    if programmed:
                        boundary = max(time.time(), self.media_buffer.tail_deadline or 0)
                        if slot.item['estimated'] and not info.get('is_live'):
                            # EOF taught us the real duration. Refill with the now-known
                            # video instead of treating successful playback as a failure.
                            self.timeline.sync(now=boundary, preserve_removed=self.retained_current())
                        elif shortfall > 1:
                            self.timeline.advance(slot, now=boundary)
                    else:
                        self.advance_after_source(slot, "Source ended before its scheduled boundary")
                    following = self.scheduled()
                    if not programmed and following and following.key != slot.key:
                        resume = (following.key, 0.0)
                    completed = slot.key
                    self.scheduled()
                    failures = 0
                    skipped_urls.clear()
                    recovery_attempts.pop(slot.key, None)
                    if not programmed and self.clip == self.retained_clip and not self.db.media(self.id):
                        await self.media_buffer.drain()
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
                    self.event("Source unavailable: " + item["title"] + " · " + self.error)
                    if self.process:
                        await stop_process(self.process)
                        self.process = None
                    attempts = recovery_attempts.get(slot.key, 0)
                    if attempts < 1:
                        recovery_attempts[slot.key] = attempts + 1
                        resume = (slot.key, offset + self.clip_duration)
                        self.event("Refreshing direct URLs and retrying the current source")
                        continue
                    recovery_attempts.pop(slot.key, None)
                    skipped_urls.add(item['url'])
                    self.advance_after_source(slot, "Source failed")
                    following = self.scheduled()
                    if not programmed and following and following.key != slot.key:
                        resume = (following.key, 0.0)
                    completed = slot.key
                    # Continue through the remaining sources without delay. If
                    # every enabled source failed, retain the former backoff
                    # before beginning a fresh round.
                    available_urls = {media['url'] for media in self.db.media(self.id, item.get('programme_id'))}
                    if available_urls and available_urls <= skipped_urls:
                        self.event("All enabled sources failed; retrying the rotation shortly")
                        skipped_urls.clear()
                        await asyncio.sleep(min(failures * 2, 20))
                finally:
                    if self.process:
                        await stop_process(self.process)
                        self.process = None
        finally:
            if self.media_buffer:
                await self.media_buffer.close()
                self.media_buffer = None
            await self.cancel_prefetch()
            if self.loading:
                await self.loading.close()
                self.loading = None
            self.clip = None
            self.pending.clear()

    def begin_ingest(self, clip):
        if self.ingest_clip != clip:
            self.final_input_segment = None
            self.ingest_clip = clip
            self.last_input_segment = -1
            self.gap_pending = False
            self.announced.clear()
            self.aborted.clear()
            self.window_start = 0
            self.clip_duration = 0.0

    async def upload_aborted(self, clip, filename):
        async with self.ingest_lock:
            await self._upload_aborted(clip, filename)

    async def _upload_aborted(self, clip, filename):
        if clip != self.clip or not filename.endswith('.ts'):
            return
        self.begin_ingest(clip)
        self.aborted.add(int(filename.removesuffix('.ts')))
        await self.publish_ready()

    async def ingest(self, clip, filename, body):
        if self.loading and clip == self.loading.clip:
            return await self.loading.ingest(filename, body)
        async with self.ingest_lock:
            return await self._ingest(clip, filename, body)

    async def _ingest(self, clip, filename, body):
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
            if b'#EXT-X-ENDLIST' in body:
                self.final_input_segment = max(indices, default=self.last_input_segment)
                self.event(f'Final HLS playlist received: segment {self.final_input_segment}')
        else:
            return False
        await self.publish_ready()
        if self.final_input_segment is not None and self.last_input_segment >= self.final_input_segment:
            self.ingest_finished.set()
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
            segment.discontinuity = int(self.gap_pending)
            self.gap_pending = False
            if self.media_buffer:
                await self.media_buffer.put(segment)
            else:
                await self.publish_segment(segment)

    async def publish_segment(self, segment):
        if self.loading:
            self.loading.disable()
        self.append_segment(segment, bool(segment.discontinuity))
        self.state = "live"
        self.error = None
        now = time.time()
        previous = self.metrics.get('last_segment_at')
        if previous is not None:
            interval = round(now - previous, 3)
            self.metrics['segment_interval_seconds'] = interval
            self.metrics['max_segment_interval_seconds'] = max(interval, self.metrics.get('max_segment_interval_seconds', 0))
            if interval > max(6, segment.duration * 1.5):
                self.event(f'Segment delivery gap: {interval:.2f}s')
        self.metrics['last_segment_at'] = now
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
        programmed = self.timeline is self.programme_timeline
        actual = self.programme_timeline.held() if programmed else None
        nominal = next(self.programme_timeline.snapshot().slots(time.time(), time.time() + 1), None) if programmed else None
        return {"state": self.state, "viewers": len(self.viewers), "now": now,
                "schedule_mode": "programmes" if programmed else "rotation", "timezone": config.TZ,
                "gap_mode": self.db.setting(f'gap_mode:{self.id}', 'black') if self.db else 'black',
                "programme": ({"title": nominal.item['title'], "starts_at": nominal.starts_at, "ends_at": nominal.ends_at} if nominal else None),
                "actual_programme": actual,
                "buffer_bytes": self.bytes, "segments": len(self.segments), "error": self.error,
                "events": list(self.events), "encoder": gpu.settings(self.db)["encoder"],
                "finish_current_on_remove": self.finish_current_on_remove, "fps": self.fps, "resolution": self.resolution, "target_bitrate": self.target_bitrate,
                "stream_revision": self.stream_revision, "stream_available": self.available() if self.db else False,
                "diagnostics": {**self.metrics,
                    "reserve_segments": self.media_buffer.count if self.media_buffer else 0,
                    "reserve_seconds": round(self.media_buffer.seconds, 3) if self.media_buffer else 0,
                    "reserve_bytes": self.media_buffer.bytes if self.media_buffer else 0}}
