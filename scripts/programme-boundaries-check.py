"""Real gap -> programme -> gap transitions. EMPTY disposable instance only.
Needs up to three minutes, local FFmpeg and yt-dlp in the test container.
"""
import asyncio
from datetime import datetime, timedelta
from functools import partial
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from zoneinfo import ZoneInfo
import httpx
from media_fixture import MediaHandler

BASE = os.getenv('TUBE_URL', 'http://127.0.0.1:8003')
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')


async def main():
    with tempfile.TemporaryDirectory(prefix='boundary-fixture-') as directory:
        for name, seconds, color in [('gap', 90, 'blue'), ('programme', 9, 'red')]:
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=320x180:r=24',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', str(seconds), '-c:v', 'libx264',
                '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(Path(directory) / f'{name}.mp4')], check=True)
        server = ThreadingHTTPServer(('0.0.0.0', 8774), partial(MediaHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async with httpx.AsyncClient(timeout=100) as client:
                async def get(path):
                    r = await client.get(BASE + path); r.raise_for_status(); return r
                async def post(path, data):
                    r = await client.post(BASE + path, json=data); r.raise_for_status(); return r.json()
                status = (await get('/api/status')).json()
                assert not status['sources'] and not (await get('/api/channels/main/programmes')).json(), 'Use an empty disposable instance'
                await client.patch(BASE + '/api/settings', json={'fps': 24, 'resolution': '480p', 'gap_mode': 'sources'})
                local = datetime.now(ZoneInfo(status['timezone']))
                start = local.replace(second=0, microsecond=0) + timedelta(minutes=2)
                begin, end = start.timestamp(), start.timestamp() + 60
                programme = await post('/api/channels/main/programmes', {'name': 'Boundary programme', 'duration_minutes': 1,
                    'rules': [{'weekdays': list(range(7)), 'time': start.strftime('%H:%M')}]})
                path = '/api/channels/main/programmes/' + programme['id']
                general = await post('/api/sources', {'url': f'http://{HOST}:8774/gap.mp4'})
                await post(path + '/sources', {'url': f'http://{HOST}:8774/programme.mp4'})
                phases, seen, gap_before_end = set(), {}, None
                stable_revision = None
                deadline = end + 25
                while time.time() < deadline:
                    response = await get('/channels/main/index.m3u8?viewer=boundaries')
                    stamp = duration = None
                    for line in response.text.splitlines():
                        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
                            stamp = datetime.fromisoformat(line.split(':', 1)[1]).timestamp()
                        elif line.startswith('#EXTINF:'):
                            duration = float(line.split(':', 1)[1].split(',')[0])
                        elif line.startswith('segments/') and stamp is not None:
                            index = int(line.split('/')[1].split('.')[0])
                            pair = stamp, duration
                            assert index not in seen or seen[index] == pair
                            seen[index] = pair
                    s = (await get('/api/status')).json()
                    now = time.time()
                    phase = 'before' if now < begin else 'programme' if now < end else 'after'
                    if s['state'] == 'live':
                        if phase == 'programme':
                            assert s['now']['programme_id'] == programme['id'], s
                        if phase == 'before' and s['now']['kind'] == 'video':
                            assert s['now']['programme_id'] is None
                            stable_revision = s['stream_revision'] if stable_revision is None else stable_revision
                            gap_before_end = s['now']['ends_at']
                            assert gap_before_end <= begin
                        if phase == 'after' and now > end + 12:
                            assert s['now']['programme_id'] is None, s
                        if phase not in phases:
                            phases.add(phase)
                            print('PASS: live phase ' + phase, flush=True)
                    if stable_revision is not None:
                        assert s['stream_revision'] == stable_revision, 'Natural boundaries must not revoke the HLS stream'
                    assert s['diagnostics']['reserve_segments'] <= 3
                    await asyncio.sleep(1)
                assert phases == {'before', 'programme', 'after'}
                assert gap_before_end is not None
                ordered = sorted(seen.items())
                for (left, (start_at, length)), (right, (next_at, _)) in zip(ordered, ordered[1:]):
                    if right == left + 1:
                        assert next_at >= start_at + length - .002
                timestamps = [value[0] for value in seen.values()]
                assert any(begin <= t < begin + 8 for t in timestamps), ('programme start missing', begin, timestamps)
                assert any(end <= t < end + 15 for t in timestamps), ('programme end missing', end, timestamps)
                xml = (await get('/epg.xml')).text
                assert '<title>Boundary programme</title>' in xml and '<title>No planned programme</title>' in xml
                await client.delete(BASE + path)
                await client.delete(BASE + '/api/sources/' + general['id'])
                print('PASS: hard gap cut, programme overrun bound, gap resumption and continuous HLS media times', flush=True)
        finally:
            server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
