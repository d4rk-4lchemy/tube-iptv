import json
import shutil
import subprocess

import pytest

from app.engine import ffmpeg_command


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
@pytest.mark.parametrize('resolution,width,height', [
    ('480p', 854, 480), ('720p', 1280, 720), ('1080p', 1920, 1080), ('4k', 3840, 2160),
])
def test_encoded_canvas(resolution, width, height):
    command = ffmpeg_command([], {}, 'http://localhost/unused', encoder='software',
                             duration=0.2, fps=24, resolution=resolution)
    index = next(i for i, value in enumerate(command) if value.startswith('color='))
    command[index] = 'testsrc2=s=320x240:r=24,setsar=4/3'
    command[command.index('hls') - 1:] = ['-f', 'mpegts', 'pipe:1']
    result = subprocess.run(command, capture_output=True, check=True, timeout=60)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height,sample_aspect_ratio',
                            '-of', 'json', 'pipe:0'], input=result.stdout,
                           capture_output=True, check=True, timeout=30)
    video = json.loads(probe.stdout)['streams'][0]
    assert (video['width'], video['height']) == (width, height)
    assert video['sample_aspect_ratio'] == '1:1'


@pytest.mark.parametrize('encoder', ['software', 'vaapi', 'qsv', 'nvenc'])
@pytest.mark.parametrize('bitrate', [100, 6000, 25000])
def test_custom_bitrate_and_segment_budget(encoder, bitrate):
    from app.engine import MAX_SEGMENT, MAX_BUFFER
    command = ffmpeg_command([], {}, 'http://localhost/unused', encoder=encoder,
                             resolution='4k', target_bitrate=bitrate)
    assert command[command.index('-b:v') + 1] == f'{bitrate}k'
    maxrate = int(command[command.index('-maxrate') + 1][:-1])
    buffer_rate = int(command[command.index('-bufsize') + 1][:-1])
    assert bitrate <= maxrate <= 30000
    # Four seconds, full VBV burst, AAC and generous transport overhead.
    assert ((maxrate + 128) * 4 + buffer_rate) * 1000 / 8 * 1.15 < MAX_SEGMENT
    assert MAX_BUFFER >= 6 * MAX_SEGMENT
