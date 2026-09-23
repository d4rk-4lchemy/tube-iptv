"""Real yt-dlp -> HTTP input -> FFmpeg -> RAM HLS -> ffprobe integration.
Uses only self-generated test media. Run against a disposable empty instance.
"""
import asyncio
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer
from media_fixture import MediaHandler
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.parse import urljoin
import httpx

BASE = os.getenv('TUBE_URL', 'http://127.0.0.1:8001')
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')


async def main():
    with tempfile.TemporaryDirectory(prefix='tube-fixture-') as directory:
        for index, color in enumerate(('teal', 'orange')):
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            f'color=c={color}:s=320x180:r=25', '-f', 'lavfi', '-i',
                            f'sine=frequency={440 + index * 220}:sample_rate=48000', '-t', '9',
                            '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                            '-movflags', '+faststart', str(Path(directory) / f'clip{index}.mp4')], check=True)
        server = ThreadingHTTPServer(('0.0.0.0', 8766), partial(MediaHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ids = []
        async with httpx.AsyncClient(timeout=100) as client:
            try:
                for attempt in range(60):
                    try:
                        response = await client.get(BASE + '/api/status')
                        response.raise_for_status()
                        initial = response.json()
                        break
                    except httpx.HTTPError:
                        if attempt == 59:
                            raise
                        await asyncio.sleep(.5)
                assert not initial['sources'], 'Use an empty disposable instance'
                for index in range(2):
                    response = await client.post(BASE + '/api/sources', json={'url': f'http://{HOST}:8766/clip{index}.mp4'})
                    response.raise_for_status()
                    ids.append(response.json()['id'])
                for _ in range(60):
                    status = (await client.get(BASE + '/api/status')).json()
                    if all(s['state'] != 'pending' for s in status['sources']):
                        break
                    await asyncio.sleep(1)
                assert status['media_count'] == 2, status
                assert status['state'] == 'idle', status
                print('PASS: yt-dlp source extraction; no producer without viewers', flush=True)
                playlist = await client.get(BASE + '/playlist.m3u8')
                assert '#EXTINF:-1' in playlist.text
                url = BASE + '/channels/main/index.m3u8?viewer=smoke-one'
                response = await client.get(url)
                response.raise_for_status()
                first = response.text
                other = await client.get(BASE + '/channels/main/index.m3u8?viewer=smoke-two')
                assert first.split('#EXT-X-MEDIA-SEQUENCE:')[1].splitlines()[0] == other.text.split('#EXT-X-MEDIA-SEQUENCE:')[1].splitlines()[0]
                status = (await client.get(BASE + '/api/status')).json()
                assert status['viewers'] == 2
                print('PASS: two viewers share one timeline', flush=True)
                media_url = next(line for line in first.splitlines() if line and not line.startswith('#'))
                media = await client.get(urljoin(url, media_url))
                media.raise_for_status()
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,width,height', '-of', 'json', '-i', 'pipe:0'], input=media.content, capture_output=True, check=True)
                assert b'h264' in probe.stdout and b'aac' in probe.stdout and b'1920' in probe.stdout
                print('PASS: playable H.264/AAC 1080p MPEG-TS from RAM', flush=True)
                changed = False
                for _ in range(40):
                    response = await client.get(url)
                    if '#EXT-X-DISCONTINUITY\n' in response.text:
                        changed = True
                        break
                    await asyncio.sleep(1)
                assert changed, 'No transition to next clip'
                print('PASS: next clip uses HLS discontinuity', flush=True)
                seen = {}
                for _ in range(45):
                    manifest = (await client.get(url)).text
                    stamp = duration = None
                    for line in manifest.splitlines():
                        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
                            stamp = datetime.fromisoformat(line.split(':', 1)[1]).timestamp()
                        elif line.startswith('#EXTINF:'):
                            duration = float(line.split(':', 1)[1].split(',')[0])
                        elif line.startswith('segments/') and stamp is not None:
                            sequence = int(line.split('/')[1].split('.')[0])
                            value = (stamp, duration)
                            assert sequence not in seen or seen[sequence] == value, 'Segment identity changed'
                            seen[sequence] = value
                            stamp = None
                    ordered = sorted(seen.items())
                    for (left, (start, length)), (right, (end, _)) in zip(ordered, ordered[1:]):
                        if right == left + 1:
                            assert end >= start + length - .002, 'Overlapping media across a handoff'
                    if len(seen) >= 12:
                        break
                    await asyncio.sleep(1)
                assert len(seen) >= 12, 'Insufficient segments across multiple clips'
                print('PASS: stable segment identities and non-overlapping media across multiple clips', flush=True)
                if os.getenv('BROWSER_CHECK'):
                    browser = await asyncio.create_subprocess_exec('node', 'scripts/playback-check.mjs', env={**os.environ, 'TUBE_URL': BASE})
                    assert await browser.wait() == 0, 'Browser playback failed'
                for _ in range(40):
                    status = (await client.get(BASE + '/api/status')).json()
                    if status['state'] == 'idle':
                        break
                    await asyncio.sleep(1)
                assert status['state'] == 'idle' and status['buffer_bytes'] == 0 and status['viewers'] == 0, status
                print('PASS: idle shutdown and complete RAM buffer release', flush=True)
            finally:
                for source in ids:
                    await client.delete(BASE + '/api/sources/' + source)
                server.shutdown()


asyncio.run(main())
