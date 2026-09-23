"""Exercise immediate source removal, retained final video, and large HLS uploads."""
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import json
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
    with tempfile.TemporaryDirectory(prefix='tube-removal-') as directory:
        # Moving test pattern gives realistic segment sizes, unlike a solid frame.
        path = Path(directory) / 'a.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '30',
                        '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-b:v', '2500k',
                        '-g', '100', '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True)
        (Path(directory) / 'b.mp4').symlink_to(path)
        server = ThreadingHTTPServer(('0.0.0.0', 8768), partial(MediaHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ids = []
        async with httpx.AsyncClient(timeout=100) as client:
            async def status():
                return (await client.get(BASE + '/api/status')).json()
            async def manifest():
                result = await client.get(BASE + '/channels/main/index.m3u8?viewer=removal-test')
                result.raise_for_status()
                return result.text
            try:
                assert not (await status())['sources'], 'Use an empty disposable instance'
                await client.patch(BASE + '/api/settings', json={'finish_current_on_remove': False})
                for name in ['a', 'b']:
                    response = await client.post(BASE + '/api/sources', json={'url': f'http://{HOST}:8768/{name}.mp4'})
                    response.raise_for_status()
                    ids.append(response.json()['id'])
                for _ in range(60):
                    s = await status()
                    if len(s['sources']) == 2 and all(x['state'] == 'ready' for x in s['sources']):
                        break
                    await asyncio.sleep(.5)
                first = await manifest()
                for _ in range(40):
                    s = await status()
                    if s['diagnostics'].get('ready_seconds') is not None:
                        break
                    await manifest()
                    await asyncio.sleep(.5)
                old_url = s['now']['url']
                removed = next(x for x in s['sources'] if x['url'] == old_url)['id']
                old_segment = next(x for x in first.splitlines() if x and not x.startswith('#'))
                revision = s['stream_revision']
                response = await client.delete(BASE + '/api/sources/' + removed)
                response.raise_for_status()
                ids.remove(removed)
                s = await status()
                assert s['stream_revision'] > revision and s['buffer_bytes'] == 0
                assert s['now']['url'] != old_url and s['media_count'] == 1
                assert (await client.get(urljoin(BASE + '/channels/main/index.m3u8', old_segment))).status_code == 404
                print('PASS: deleting active source cancels producer, revokes old segments, and rebuilds queue', flush=True)
                for _ in range(40):
                    await manifest()
                    s = await status()
                    if s['diagnostics'].get('ready_seconds') is not None:
                        break
                    await asyncio.sleep(.5)
                assert s['diagnostics']['lost_segments'] == 0 and s['diagnostics']['upload_aborts'] == 0, s['diagnostics']
                print('MEASURED STARTUP:', json.dumps(s['diagnostics']), flush=True)
                await client.patch(BASE + '/api/settings', json={'finish_current_on_remove': True})
                remaining = ids.pop()
                await client.delete(BASE + '/api/sources/' + remaining)
                s = await status()
                assert s['media_count'] == 0 and s['stream_available']
                final = ''
                for _ in range(50):
                    final = await manifest()
                    if '#EXT-X-ENDLIST' in final:
                        break
                    await asyncio.sleep(1)
                assert '#EXT-X-ENDLIST' in final
                last = next(x for x in reversed(final.splitlines()) if x and not x.startswith('#'))
                assert (await client.get(urljoin(BASE + '/channels/main/index.m3u8', last))).status_code == 200
                print('PASS: optional retention keeps only current video, including its final segments', flush=True)
                for _ in range(35):
                    s = await status()
                    if s['state'] == 'idle':
                        break
                    await asyncio.sleep(1)
                assert s['buffer_bytes'] == 0 and not s['stream_available']
                assert s['diagnostics']['lost_segments'] == 0 and s['diagnostics']['upload_aborts'] == 0
                print('PASS: realistic-size uploads are ordered, complete, and released when idle', flush=True)
            finally:
                for source in ids:
                    await client.delete(BASE + '/api/sources/' + source)
                await client.patch(BASE + '/api/settings', json={'finish_current_on_remove': False})
                server.shutdown()


asyncio.run(main())
