"""Exercise the software encoder with media kept in pipes, never on disk."""
import json
import math
import shutil
import subprocess
from fractions import Fraction

import pytest

from app.engine import ffmpeg_command
from app.music import Caption


def encode_frames(command):
    # Capture the transport stream directly instead of uploading HLS.
    output = command.index('hls') - 1
    command[output:] = ['-f', 'mpegts', 'pipe:1']
    encoded = subprocess.run(command, capture_output=True, timeout=60, check=True)
    probed = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
         '-show_entries', 'frame=best_effort_timestamp_time,key_frame',
         '-of', 'json', 'pipe:0'],
        input=encoded.stdout, capture_output=True, timeout=30, check=True)
    return json.loads(probed.stdout)['frames']


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='FFmpeg and ffprobe are required')
@pytest.mark.parametrize('source_fps,fps', [
    (25, 60), (60, 60), (60, 24), (60, 25), (60, 30), (60, 50),
    (25, 'original'), ('30000/1001', 'original'), ('variable', 'original'),
])
def test_frame_timing_and_four_second_keyframes(source_fps, fps):
    command = ffmpeg_command([], {}, 'http://localhost/unused',
                             encoder='software', duration=4.2, fps=fps,
                             music_caption=Caption('Artist', 'Song'), music_end=30)
    # Substitute a synthetic input; retain the production filters and encoder.
    index = next(i for i, value in enumerate(command) if value.startswith('color='))
    if source_fps == 'variable':
        command[index] = "testsrc2=s=320x180:r=60,select='if(lt(t,2),not(mod(n,2)),not(mod(n,3)))'"
        expected = [n / 60 for n in range(252) if n % (2 if n < 120 else 3) == 0]
    else:
        command[index] = f'testsrc2=s=320x180:r={source_fps}'
        rate = Fraction(source_fps if fps == 'original' else fps)
        expected = [float(n / rate) for n in range(math.ceil(Fraction('4.2') * rate))]
    frames = encode_frames(command)
    timestamps = [float(frame['best_effort_timestamp_time']) for frame in frames]
    # MPEG-TS timestamps are quantized to 1/90000 second.
    relative = [stamp - timestamps[0] for stamp in timestamps]
    # -shortest and the duration limit can trim the final partial frame.
    assert len(expected) - 1 <= len(relative) <= len(expected)
    assert relative == pytest.approx(expected[:len(relative)], abs=0.00002)
    keyframes = [stamp for stamp, frame in zip(relative, frames) if frame['key_frame']]
    assert keyframes == pytest.approx([0, next(t for t in expected if t >= 4)], abs=0.00002)
