"""Programme HLS checks. Use only a disposable EMPTY instance on TUBE_URL.
Generates local fixtures, creates/deletes a test programme and changes settings.
"""
import asyncio
from datetime import datetime, timedelta
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import httpx
from PIL import Image, ImageChops
from media_fixture import MediaHandler

BASE = os.getenv('TUBE_URL', 'http://127.0.0.1:8003')
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')


async def main():
    with tempfile.TemporaryDirectory(prefix='programme-fixture-') as directory:
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=red:s=320x180:r=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '9', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
            '-movflags', '+faststart', str(Path(directory) / 'Fixture Artist - Fixture Song.mp4')], check=True)
        server = ThreadingHTTPServer(('0.0.0.0', 8773), partial(MediaHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        async with httpx.AsyncClient(timeout=100) as client:
            async def get(path):
                r = await client.get(BASE + path); r.raise_for_status(); return r
            async def write(path, body):
                r = await client.post(BASE + path, json=body); r.raise_for_status(); return r.json()
            status = (await get('/api/status')).json()
            assert not status['sources'] and not (await get('/api/channels/main/programmes')).json(), 'Use an empty disposable instance'
            await client.patch(BASE + '/api/settings', json={'fps': 24, 'resolution': '480p'})
            local = datetime.now(ZoneInfo(status['timezone']))
            at = local.replace(second=0, microsecond=0) - timedelta(minutes=1)
            programme = await write('/api/channels/main/programmes', {'name': 'Integration programme', 'duration_minutes': 3, 'music': True,
                'rules': [{'weekdays': [at.weekday()], 'time': at.strftime('%H:%M')}]})
            path = '/api/channels/main/programmes/' + programme['id']
            stream = '/channels/main/index.m3u8?viewer=programme-check'
            started = time.monotonic()
            seen = {}
            async def sample():
                response = await get(stream)
                stamp = length = None
                for line in response.text.splitlines():
                    if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
                        stamp = datetime.fromisoformat(line.split(':', 1)[1]).timestamp()
                    elif line.startswith('#EXTINF:'):
                        length = float(line.split(':', 1)[1].split(',')[0])
                    elif line.startswith('segments/') and stamp is not None:
                        index = int(line.split('/')[1].split('.')[0])
                        pair = (stamp, length)
                        assert index not in seen or seen[index] == pair
                        seen[index] = pair
                return response
            while time.monotonic() - started < 25:
                await sample()
                s = (await get('/api/status')).json()
                if s['state'] == 'live' and s['now']['kind'] == 'black' and s['diagnostics'].get('first_segment_seconds') is not None:
                    break
                await asyncio.sleep(1)
            assert s['now']['kind'] == 'black' and s['state'] == 'live', s
            assert s['buffer_bytes'] < 192 * 1024 * 1024
            manifest = (await sample()).text
            segment_url = [line for line in manifest.splitlines() if line.startswith('segments/')][-1]
            segment = await client.get(urljoin(BASE + stream, segment_url))
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'json', '-i', 'pipe:0'], input=segment.content, capture_output=True, check=True)
            codecs = {s['codec_name'] for s in json.loads(probe.stdout)['streams']}
            assert {'h264', 'aac'} <= codecs
            pixels = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-map', '0:v:0', '-vf', 'scale=1:1',
                '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], input=segment.content, capture_output=True, check=True).stdout
            assert len(pixels) == 3 and max(pixels) <= 3, pixels
            sound = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-map', '0:a:0', '-t', '0.1',
                '-f', 's16le', 'pipe:1'], input=segment.content, capture_output=True, check=True).stdout
            assert sound and not any(sound), 'Black output must have silent audio'
            print('PASS: empty programme produces decoded black pixels and silent H.264/AAC in RAM', flush=True)
            source = await write(path + '/sources', {'url': f'http://{HOST}:8773/Fixture%20Artist%20-%20Fixture%20Song.mp4'})
            seen.clear()  # Source activation intentionally replaces the empty slate.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                await sample()
                s = (await get('/api/status')).json()
                if s['now']['kind'] == 'video' and s['state'] == 'live' and s['diagnostics'].get('first_segment_seconds') is not None:
                    break
                await asyncio.sleep(1)
            assert s['now']['kind'] == 'video' and s['state'] == 'live', s
            assert s['now']['programme_title'] == 'Integration programme'
            print('PASS: newly available programme source replaces black; programme identity remains', flush=True)
            deadline = time.monotonic() + 35
            caption_seen = False
            checked_segments = set()
            while time.monotonic() < deadline:
                manifest = (await sample()).text
                segment_url = [line for line in manifest.splitlines() if line.startswith('segments/')][-1]
                if not caption_seen and segment_url not in checked_segments:
                    checked_segments.add(segment_url)
                    segment = await client.get(urljoin(BASE + stream, segment_url))
                    segment.raise_for_status()
                    pixels = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-map', '0:v:0',
                        '-vf', 'fps=2', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
                        input=segment.content, capture_output=True, check=True).stdout
                    frame_size = 854 * 480 * 3
                    for offset in range(0, len(pixels), frame_size):
                        image = Image.frombytes('RGB', (854, 480), pixels[offset:offset + frame_size])
                        red, green, blue = image.split()
                        white = ImageChops.darker(ImageChops.darker(red, green), blue).point(lambda v: 255 if v > 190 else 0)
                        bounds = white.getbbox()
                        if bounds and image.getpixel((0, 0))[0] > 150:
                            assert bounds[0] >= 854 * .05 - 1 and bounds[2] <= 854 * .45 + 1, bounds
                            assert bounds[1] > 480 * .65 and bounds[3] < 480 * .9 + 1, bounds
                            Path('artifacts').mkdir(exist_ok=True)
                            image.save('artifacts/music-hls-caption.png')
                            caption_seen = True
                            break
                s = (await get('/api/status')).json()
                assert s['now']['kind'] == 'video', s
                assert s['buffer_bytes'] <= 192 * 1024 * 1024
                assert s['diagnostics']['reserve_segments'] <= 3
                await asyncio.sleep(1)
            ordered = sorted(seen.items())
            for (left, (start, duration)), (right, (next_start, _)) in zip(ordered, ordered[1:]):
                if right == left + 1:
                    assert next_start >= start + duration - .002, (left, right, start, duration, next_start)
            assert len(seen) >= 8
            assert caption_seen, 'Music caption was not found in decoded HLS frames'
            print('PASS: Music artist/title caption decoded from HLS in the lower-left safe area', flush=True)
            xml = (await get('/epg.xml')).text
            assert '<title>Integration programme</title>' in xml and '<title>No planned programme</title>' in xml
            assert '<title>clip</title>' not in xml
            print('PASS: repeated films preserve HLS sequence, non-overlapping times, bounded reserve and programme-level EPG', flush=True)
            # Verify that a source failure/removal cannot leak general channel media into a programme.
            await client.delete(BASE + '/api/sources/' + source['id'])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                await sample()
                s = (await get('/api/status')).json()
                if s['now']['kind'] == 'black' and s['state'] == 'live':
                    break
                await asyncio.sleep(1)
            assert s['now']['kind'] == 'black'
            print('PASS: removing programme media returns to black without losing the programme', flush=True)
            await client.delete(BASE + path)
            assert (await get('/api/status')).json()['schedule_mode'] == 'rotation'
            print('PASS: deleting the last programme restores legacy rotation', flush=True)
        server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
