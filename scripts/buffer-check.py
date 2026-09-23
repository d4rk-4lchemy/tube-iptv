"""Simulate a six-second producer stall on an isolated container; media stays in RAM."""
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
CONTAINER = os.getenv('TEST_CONTAINER', 'tube-buffer-test')
HOST = os.getenv('FIXTURE_HOST', 'host.docker.internal')
assert CONTAINER != 'tube-iptv', 'Never suspend the production encoder'


def signal_encoder(signal_name):
    script = '''import os,signal
from pathlib import Path
for path in Path('/proc').iterdir():
 if not path.name.isdigit(): continue
 try: args=(path/'cmdline').read_bytes().split(b'\\0')
 except (OSError,ProcessLookupError): continue
 if args[0].endswith(b'ffmpeg') and not any(b'drawtext=' in x for x in args):
  os.kill(int(path.name),getattr(signal,"SIGNAL_NAME"))
  print(path.name)
'''.replace('SIGNAL_NAME', signal_name)
    result = subprocess.run(['sudo', 'docker', 'exec', '-i', CONTAINER, 'python', '-'], input=script.encode(), capture_output=True, check=True)
    assert result.stdout.strip(), 'No encoder found'


async def main():
    with tempfile.TemporaryDirectory(prefix='tube-buffer-') as directory:
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=25',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '90',
            '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-c:a', 'aac',
            '-movflags', '+faststart', str(Path(directory)/'test.mp4')], check=True)
        server = ThreadingHTTPServer(('0.0.0.0', 8770), partial(MediaHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sid = None
        paused = False
        async with httpx.AsyncClient(timeout=30) as client:
            async def tick():
                r = await client.get(BASE+'/channels/main/index.m3u8?viewer=reserve-test')
                r.raise_for_status()
                return (await client.get(BASE+'/api/status')).json()
            try:
                assert not (await client.get(BASE+'/api/status')).json()['sources'], 'Use an empty test instance'
                r = await client.post(BASE+'/api/sources', json={'url':f'http://{HOST}:8770/test.mp4'})
                r.raise_for_status(); sid=r.json()['id']
                for _ in range(60):
                    s=(await client.get(BASE+'/api/status')).json()
                    if s['media_count']: break
                    await asyncio.sleep(.5)
                for _ in range(120):
                    s=await tick()
                    if s['diagnostics'].get('reserve_segments')==3 and s['state']=='live': break
                    await asyncio.sleep(.25)
                assert s['diagnostics']['reserve_segments']==3, s['diagnostics']
                before=s['diagnostics']['last_segment_at']
                print('RESERVE READY:', s['diagnostics'], flush=True)
                signal_encoder('SIGSTOP');paused=True
                seen=[];start=time.monotonic()
                while time.monotonic()-start < 6:
                    s=await tick()
                    at=s['diagnostics']['last_segment_at']
                    if at>before and at not in seen: seen.append(at)
                    await asyncio.sleep(.2)
                signal_encoder('SIGCONT');paused=False
                assert seen, 'No publication while encoder was suspended'
                assert seen[0]-before < 4.5, seen
                print('PASS: real video segments continued during a six-second producer stall', flush=True)
                for _ in range(100):
                    s=await tick()
                    if s['diagnostics']['reserve_segments']==3 and s['diagnostics']['last_segment_at'] >= before+12: break
                    await asyncio.sleep(.25)
                assert s['diagnostics']['reserve_segments']==3 and s['diagnostics']['last_segment_at'] >= before+12
                assert s['diagnostics']['max_segment_interval_seconds']<4.5, s['diagnostics']
                assert s['diagnostics']['lost_segments']==0 and s['diagnostics']['upload_aborts']==0
                print('PASS: reserve refilled; publication gap stayed below 4.5 seconds', s['diagnostics'], flush=True)
            finally:
                if paused: signal_encoder('SIGCONT')
                if sid: await client.delete(BASE+'/api/sources/'+sid)
                server.shutdown()

asyncio.run(main())
