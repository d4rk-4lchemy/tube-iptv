"""On-demand synthetic HLS, uploaded to RAM with the same ordered ingest as video."""
import asyncio
import secrets
import time
from . import config
from .process import stop_process
from .video import RESOLUTIONS


class LoadingSlate:
    def __init__(self, channel):
        from .engine import Channel
        self.channel = channel
        self.clip = 'loading-' + secrets.token_hex(12)
        self.ingester = Channel(channel.id + '-loading', None, None)
        self.ingester.clip = self.clip
        self.ingester.segment_sink = self.publish
        self.lock = asyncio.Lock()
        self.active = True
        self.task = None
        self.count = 0
        self.next_at = None
        self.started = time.monotonic()

    def start(self):
        self.task = asyncio.create_task(self.run())

    def disable(self):
        self.active = False
        if self.task and not self.task.done():
            self.task.cancel()

    async def close(self):
        self.disable()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        self.ingester.pending.clear()

    async def ingest(self, filename, body):
        async with self.lock:
            if not self.active:
                return False
            return await self.ingester.ingest(self.clip, filename, body)

    async def publish(self, segment):
        # Encode the initial two segments at full speed, then pace uploads.
        # HTTP backpressure bounds the producer without writing a media file.
        if self.count >= 2:
            await asyncio.sleep(max(0, self.next_at - time.monotonic()))
        if not self.active:
            return
        self.channel.append_segment(segment)
        self.count += 1
        self.next_at = time.monotonic() + segment.duration
        if self.count == 2:
            elapsed = round(time.monotonic() - self.started, 3)
            self.channel.metrics['loading_ready_seconds'] = elapsed
            self.channel.event(f'LOADING slate ready in {elapsed:.2f}s')
        async with self.channel.changed:
            self.channel.changed.notify_all()

    async def run(self):
        destination = f'{self.channel.upload_base}/{self.clip}'
        width, height, _, _ = RESOLUTIONS[self.channel.resolution]
        fps = 60 if self.channel.fps == 'original' else self.channel.fps
        command = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}',
                   '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                   '-vf', f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='LOADING...':fontcolor=white:fontsize={height // 20}:x=(w-tw)/2:y=(h-th)/2",
                   '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                   '-threads', '2', '-filter_threads', '1', '-g', str(fps * 4), '-keyint_min', str(fps * 4),
                   # HRD filler makes even a static black frame consume the requested bitrate.
                   '-b:v', '3000k', '-minrate', '3000k', '-maxrate', '3000k', '-bufsize', '3000k',
                   '-x264-params', 'nal-hrd=cbr:filler=1',
                   '-sc_threshold', '0', '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '48000',
                   '-f', 'hls', '-hls_time', '4', '-hls_list_size', '6',
                   '-hls_flags', 'independent_segments', '-hls_segment_filename', destination + '/%06d.ts',
                   '-method', 'PUT', '-http_persistent', '1', '-timeout', '10', destination + '/index.m3u8']
        process = None
        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                         stderr=asyncio.subprocess.PIPE, start_new_session=True)
            _, error = await process.communicate()
            if self.active:
                self.channel.event('Loading slate stopped: ' + error.decode(errors='replace')[-500:])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.channel.event('Loading slate unavailable: ' + str(exc))
        finally:
            if process:
                await stop_process(process)
