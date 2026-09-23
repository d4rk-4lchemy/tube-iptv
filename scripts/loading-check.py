"""Cold-start browser test with delayed HTTP media, never touches production sources."""
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import httpx
from media_fixture import MediaHandler

BASE = os.getenv('TUBE_URL', 'http://127.0.0.1:8001')
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')

class SlowMedia(MediaHandler):
    def do_GET(self):
        time.sleep(3)
        super().do_GET()

async def main():
    with tempfile.TemporaryDirectory(prefix='tube-loading-') as directory:
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=640x480:r=25',
                        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '90',
                        '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                        '-movflags', '+faststart', str(Path(directory)/'four-three.mp4')], check=True)
        server = ThreadingHTTPServer(('0.0.0.0', 8769), partial(SlowMedia, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sid = None
        async with httpx.AsyncClient(timeout=100) as client:
            try:
                assert not (await client.get(BASE+'/api/status')).json()['sources'], 'Use an empty disposable instance'
                r = await client.post(BASE+'/api/sources', json={'url':f'http://{HOST}:8769/four-three.mp4'})
                r.raise_for_status(); sid = r.json()['id']
                for _ in range(80):
                    s = (await client.get(BASE+'/api/status')).json()
                    if s['media_count']: break
                    await asyncio.sleep(.5)
                assert s['media_count'] == 1
                proc = await asyncio.create_subprocess_exec('node', 'scripts/loading-check.mjs', env={**os.environ,'TUBE_URL':BASE})
                assert await proc.wait() == 0
                s = (await client.get(BASE+'/api/status')).json()
                print('DIAGNOSTICS:', s['diagnostics'], flush=True)
                assert s['diagnostics']['loading_ready_seconds'] < s['diagnostics']['ready_seconds']
                assert s['diagnostics']['lost_segments'] == 0
                for _ in range(40):
                    s = (await client.get(BASE+'/api/status')).json()
                    if s['state'] == 'idle': break
                    await asyncio.sleep(1)
                assert s['state'] == 'idle' and s['buffer_bytes'] == 0
                print('PASS: slate and video buffers released after disconnect', flush=True)
            finally:
                if sid: await client.delete(BASE+'/api/sources/'+sid)
                server.shutdown()

asyncio.run(main())
