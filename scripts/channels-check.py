"""Real multi-channel playback. Run only against an empty disposable instance."""
import asyncio
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
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=100) as client:
        async def status(channel):
            response = await client.get(f'/api/channels/{channel}/status')
            response.raise_for_status()
            return response.json()

        channels = (await client.get('/api/channels')).json()
        assert len(channels) == 1 and channels[0]['id'] == 'main', 'Use an empty disposable instance'
        assert not (await status('main'))['sources'], 'Use an empty disposable instance'
        with tempfile.TemporaryDirectory(prefix='tube-channels-fixture-') as directory:
            for index, color in enumerate(('teal', 'orange')):
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', f'color=c={color}:s=320x180:r=25', '-f', 'lavfi', '-i',
                    f'sine=frequency={440 + index * 220}:sample_rate=48000', '-t', '90',
                    '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-movflags', '+faststart', str(Path(directory) / f'clip{index}.mp4')], check=True)
            server = ThreadingHTTPServer(('0.0.0.0', 8771), partial(MediaHandler, directory=directory))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            second = None
            sources = []
            try:
                response = await client.post('/api/channels', json={'name': 'Second test channel'})
                response.raise_for_status()
                second = response.json()['id']
                for index, channel in enumerate(('main', second)):
                    response = await client.post(f'/api/channels/{channel}/sources',
                        json={'url': f'http://{HOST}:8771/clip{index}.mp4'})
                    response.raise_for_status()
                    sources.append(response.json()['id'])
                for _ in range(60):
                    states = await asyncio.gather(status('main'), status(second))
                    if all(s['media_count'] == 1 for s in states):
                        break
                    await asyncio.sleep(1)
                assert all(s['media_count'] == 1 and s['state'] == 'idle' for s in states), states
                urls = [f'/channels/{channel}/index.m3u8?viewer=channel-test' for channel in ('main', second)]
                response = await client.get(urls[0])
                response.raise_for_status()
                assert (await status(second))['state'] == 'idle'
                print('PASS: watching one channel leaves the other idle', flush=True)
                response = await client.get(urls[1])
                response.raise_for_status()
                for _ in range(40):
                    responses = await asyncio.gather(*(client.get(url) for url in urls))
                    for response in responses:
                        response.raise_for_status()
                    states = await asyncio.gather(status('main'), status(second))
                    if all(s['state'] == 'live' for s in states):
                        break
                    await asyncio.sleep(1)
                assert all(s['state'] == 'live' and s['viewers'] == 1 for s in states), states
                for response in responses:
                    segment = next(line for line in response.text.splitlines() if line.startswith('segments/'))
                    media = await client.get(urljoin(str(response.url), segment))
                    media.raise_for_status()
                    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'stream=codec_name,width,height', '-of', 'json', '-i', 'pipe:0'],
                        input=media.content, capture_output=True, check=True)
                    assert b'h264' in probe.stdout and b'aac' in probe.stdout and b'1920' in probe.stdout
                assert states[0]['now']['url'] != states[1]['now']['url']
                assert states[0]['yt_dlp'] == states[1]['yt_dlp']
                print('PASS: simultaneous independent H.264/AAC streams with shared yt-dlp', flush=True)
                deleted = second
                response = await client.delete(f'/api/channels/{second}')
                response.raise_for_status()
                second = None
                assert (await client.get(f'/channels/{deleted}/index.m3u8')).status_code == 404
                assert (await client.get(urls[0])).status_code == 200
                assert (await status('main'))['state'] == 'live'
                print('PASS: deleting a watched channel leaves the other streaming', flush=True)
                before = await status('main')
                for _ in range(45):
                    await asyncio.sleep(1)
                    after = await status('main')
                    if after['state'] == 'idle':
                        break
                assert after['state'] == 'idle' and after['buffer_bytes'] == 0 and after['viewers'] == 0
                assert after['now']['starts_at'] >= before['now']['starts_at']
                print('PASS: idle shutdown releases media while the schedule continues', flush=True)
            finally:
                if second:
                    await client.delete(f'/api/channels/{second}')
                for source in sources:
                    await client.delete(f'/api/sources/{source}')
                server.shutdown()


asyncio.run(main())
