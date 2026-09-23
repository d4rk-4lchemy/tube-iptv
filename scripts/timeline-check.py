"""Integration: persist + advance an idle schedule by 120s, then verify decoded frames.
Runs against a disposable Docker container; never use on your real channel database.
"""
import asyncio
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.parse import urljoin
import httpx
from media_fixture import MediaHandler

BASE = os.getenv('TUBE_URL', 'http://127.0.0.1:8001')
CONTAINER = os.getenv('TEST_CONTAINER', 'tube-timeline-test')
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')


def docker(*args, input=None):
    return subprocess.run(['sudo', 'docker', *args], input=input, capture_output=True, check=True).stdout


async def wait_ready(client):
    for _ in range(60):
        try:
            response = await client.get(BASE + '/api/status')
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            await asyncio.sleep(.5)
    raise AssertionError('Container did not become ready')


async def check_green(client, viewer):
    url = BASE + '/channels/main/index.m3u8?viewer=' + viewer
    for _ in range(60):
        response = await client.get(url)
        response.raise_for_status()
        lines = response.text.splitlines()
        programme = next((i for i, s in enumerate(lines) if s.startswith('#EXT-X-PROGRAM-DATE-TIME:')), None)
        if programme is not None:
            break
        await asyncio.sleep(.5)
    assert programme is not None, 'No real video after loading slate'
    first_time = datetime.fromisoformat(lines[programme].split(':', 1)[1]).timestamp()
    uri = next(s for s in lines[programme:] if s and not s.startswith('#'))
    segment = await client.get(urljoin(url, uri))
    segment.raise_for_status()
    frame = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-frames:v', '1',
        '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
        input=segment.content, capture_output=True, check=True).stdout
    red, green, blue = frame[:3]
    assert green > 180 and red < 60 and blue < 60, (red, green, blue)
    return first_time


async def idle(client):
    for _ in range(50):
        status = (await client.get(BASE + '/api/status')).json()
        if status['state'] == 'idle':
            assert status['buffer_bytes'] == 0
            processes = docker('top', CONTAINER, '-eo', 'pid,comm').decode()
            assert 'ffmpeg' not in processes and 'yt_dlp' not in processes
            return status
        await asyncio.sleep(1)
    raise AssertionError('Stream did not stop')


async def main():
    with tempfile.TemporaryDirectory(prefix='tube-clock-') as directory:
        # First minute is red; every frame from minute two onwards is green.
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=red:s=320x180:r=25', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '180', '-vf', "drawbox=color=lime:t=fill:enable='gte(t,60)'", '-c:v', 'libx264',
            '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart',
            str(Path(directory) / 'clock.mp4')], check=True)
        server = ThreadingHTTPServer(('0.0.0.0', 8767), partial(MediaHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sid = None
        async with httpx.AsyncClient(timeout=100) as client:
            try:
                initial = await wait_ready(client)
                assert not initial['sources'], 'Use an empty disposable container'
                response = await client.post(BASE + '/api/sources', json={'url': f'http://{HOST}:8767/clock.mp4'})
                response.raise_for_status()
                sid = response.json()['id']
                for _ in range(60):
                    status = (await client.get(BASE + '/api/status')).json()
                    if status['sources'][0]['state'] == 'ready' and status['now']:
                        break
                    await asyncio.sleep(.5)
                assert status['now'] and status['state'] == 'idle'
                # Move only the persisted clock. This
                # simulates two minutes of idle time, including a full app restart.
                docker('exec', '-i', CONTAINER, 'python', '-', input=b'''
import json, sqlite3, time
c=sqlite3.connect('/data/tube.sqlite3')
s=json.loads(c.execute("SELECT value FROM settings WHERE key='timeline:main'").fetchone()[0])
s['lead']=None
s['epoch']=time.time()-120
c.execute("UPDATE settings SET value=? WHERE key='timeline:main'",(json.dumps(s),))
c.commit()
''')
                docker('restart', CONTAINER)
                restored = await wait_ready(client)
                assert 120 <= restored['now']['offset'] < 135, restored
                assert restored['state'] == 'idle' and restored['buffer_bytes'] == 0
                assert 'ffmpeg' not in docker('top', CONTAINER, '-eo', 'pid,comm').decode()
                first = await check_green(client, 'clock-first')
                assert 120 <= first - restored['now']['starts_at'] < 145
                print('PASS: join after 120 idle seconds seeks to green frames, not the red beginning', flush=True)
                stopped = await idle(client)
                await asyncio.sleep(2)
                second = await check_green(client, 'clock-reconnect')
                assert second > first + 8, (first, second)
                assert (await client.get(BASE + '/api/status')).json()['now']['starts_at'] == restored['now']['starts_at']
                print('PASS: reconnect continues the same wall-clock slot after producer shutdown', flush=True)
                await idle(client)
            finally:
                if sid:
                    await client.delete(BASE + '/api/sources/' + sid)
                server.shutdown()


asyncio.run(main())
