import shutil
import sqlite3
import subprocess

import pytest
from PIL import Image

from app import music
from app.db import Database
from app.engine import ffmpeg_command
from app.programme_playback import ProgrammeTimeline
from test_api import client
from test_programmes import programme, insert, source, stamp


@pytest.mark.parametrize('info,title,expected', [
    ({'artists': ['Björk', 'Gość'], 'track': 'Song'}, 'Wrong - Title', ('Björk, Gość', 'Song')),
    ({'artists': [None, '', 'A', 'A'], 'artist': 'Other', 'track': 'Song'}, '', ('A', 'Song')),
    ({'artist': 'A', 'track': 'Song (Official Video)'}, '', ('A', 'Song (Official Video)')),
    ({}, 'Artysta – Utwór (Official Video) [4K]', ('Artysta', 'Utwór')),
    ({}, 'A — Song - Remix (Live) feat. B', ('A', 'Song - Remix (Live) feat. B')),
    ({'artist': 'A'}, 'a - Song', ('A', 'Song')),
    ({'track': 'Song'}, 'A - song', ('A', 'Song')),
    ({'track': 'Song'}, 'A - Different', ('', 'Song')),
    ({'artist': 'A'}, 'B - Song', ('A', 'B - Song')),
    ({'uploader': 'Label', 'channel': 'Channel'}, 'Unknown', ('', 'Unknown')),
    ({'title': 'Current'}, 'Stale', ('', 'Current')),
    ({}, 'Zażółć\nGęślą\x00', ('', 'Zażółć Gęślą')),
])
def test_metadata(info, title, expected):
    result = music.caption(info, {'title': title})
    assert (result.artist, result.title) == expected


def test_filename_fallback_never_uses_resolved_url():
    assert music.caption({'url': 'https://cdn.example/secret.mp4?token=secret'}, {}) is None
    assert music.caption({}, {'title': 'https://example.com/watch?id=123', 'url': 'https://example.com/watch?id=123'}) is None
    assert music.caption({}, {'url': 'https://example.com/Artist%20-%20Song.mp4?token=secret'}) == music.Caption('Artist', 'Song')


@pytest.mark.parametrize('end,expected', [
    (None, [(3, 13)]), (180, [(3, 13), (167, 177)]), (120, [(3, 13), (107, 117)]),
    (26, [(3, 23)]), (20, [(3, 17)]), (9, [(3, 6)]), (6.2, [(3, 3.2)]), (6, []), (2, []),
])
def test_caption_windows(end, expected):
    assert music.windows(end) == expected


def test_old_programme_migration_is_repeatable(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE programmes(id TEXT PRIMARY KEY,channel_id TEXT,name TEXT,duration_minutes INTEGER,rules TEXT)')
        conn.execute('INSERT INTO programmes VALUES(?,?,?,?,?)', ('old', 'main', 'Existing', 60, '[]'))
    for _ in range(2):
        db = Database(path)
        assert db.programmes()[0]['music'] is False
        assert db.programmes()[0]['name'] == 'Existing'
        db.conn.close()


async def test_music_api_defaults_preservation_and_validation(client):
    body = {k: v for k, v in programme().items() if k not in ('id', 'channel_id')}
    result = await client.post('/api/channels/main/programmes', json=body)
    assert result.status_code == 201
    assert result.json()['music'] is False
    path = '/api/channels/main/programmes/' + result.json()['id']
    result = await client.put(path, json={**body, 'music': True})
    assert result.json()['music'] is True
    result = await client.patch(path, json={**body, 'name': 'Renamed'})
    assert result.json()['music'] is True
    assert (await client.get(path)).json()['music'] is True
    for invalid in ('true', 1, None):
        assert (await client.put(path, json={**body, 'music': invalid})).status_code == 422
    assert (await client.put(path, json={**body, 'music': False})).json()['music'] is False


def test_music_change_waits_for_next_emission_and_survives_restart(tmp_path):
    db = Database(tmp_path / 'state.db')
    insert(db, programme())
    source(db)
    now = stamp('2026-09-24T18:10')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    first = timeline.sync()
    assert first.item['music'] is False
    db.execute('UPDATE programmes SET music=1')
    assert timeline.sync().item['music'] is False
    restored = ProgrammeTimeline(db, 'main', clock=lambda: now)
    assert restored.sync().item['music'] is False
    assert restored.sync(now=now + 86400).item['music'] is True
    # Checkpoints written before this feature have no music field.
    for plan in restored.state['plans']:
        plan['block'].get('programme', {}).pop('music', None)
    restored.save()
    assert ProgrammeTimeline(db, 'main', clock=lambda: now + 86400).position().item['music'] is False
    db.conn.close()


@pytest.mark.parametrize('encoder', ['software', 'vaapi', 'qsv', 'nvenc'])
def test_filters_precede_hardware_conversion(encoder):
    cmd = ffmpeg_command([], {}, 'http://unused', encoder=encoder,
                         music_caption=music.Caption('A', 'B'), music_end=30)
    vf = cmd[cmd.index('-vf') + 1]
    assert vf.index('pad=') < vf.index('drawtext=')
    if encoder != 'software':
        assert vf.index('drawtext=') < vf.index('format=nv12')
    for info in ({'is_live': True}, {'synthetic_black': True}):
        cmd = ffmpeg_command([], info, 'http://unused', encoder=encoder,
                             music_caption=music.Caption('A', 'B'), music_end=30)
        assert 'drawtext=' not in cmd[cmd.index('-vf') + 1]


def render_frames(times, end=30, offset=0, resolution='480p', fps=20, text=None):
    """Exercise production filters, capturing decoded pixels entirely in pipes."""
    from app.video import RESOLUTIONS
    width, height = RESOLUTIONS[resolution][:2]
    cmd = ffmpeg_command([], {}, 'http://unused', encoder='software', fps=fps, resolution=resolution,
                         music_caption=text or music.Caption('Zażółć', 'Song'), music_end=end, offset=offset)
    # Audio-only output starts with a solid background; make that background black.
    index = next(i for i, v in enumerate(cmd) if v.startswith('color='))
    rate = 60 if fps == 'original' else fps
    cmd[index] = f'color=black:s={width}x{height}:r={rate}'
    vf = cmd[cmd.index('-vf') + 1]
    select = '+'.join(f'eq(n,{round(t * rate)})' for t in times)
    cmd = ['ffmpeg', '-v', 'error', '-threads', '1', '-filter_threads', '1',
           '-f', 'lavfi', '-i', cmd[index], '-vf', vf + f",select='{select}'",
           '-frames:v', str(len(times)), '-fps_mode', 'passthrough', '-pix_fmt', 'gray',
           '-f', 'rawvideo', 'pipe:1']
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    size = width * height
    assert len(result.stdout) == size * len(times)
    return [Image.frombytes('L', (width, height), result.stdout[i * size:(i + 1) * size]) for i in range(len(times))]


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required')
def test_real_frames_timing_fades_seek_and_short_clip():
    times = [0, 3, 3.15, 3.3, 12.7, 12.85, 13, 17, 17.3, 26.85, 27]
    frames = render_frames(times)
    energy = [sum(image.histogram()[i] * i for i in range(256)) for image in frames]
    assert energy[0] == energy[1] == energy[6] == energy[7] == energy[10] == 0
    assert 0 < energy[2] < energy[3]
    assert energy[3] == energy[4] == energy[8]
    assert 0 < energy[5] < energy[4]
    assert 0 < energy[9] < energy[8]
    assert render_frames([0], offset=3.3)[0].tobytes() == frames[3].tobytes()
    assert render_frames([0], offset=17.3)[0].tobytes() == frames[8].tobytes()
    short = render_frames([3, 3.1, 3.2], end=6.2)
    assert short[0].getbbox() is None and short[1].getbbox() and short[2].getbbox() is None


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required')
@pytest.mark.parametrize('resolution,fps', [('480p', 24), ('720p', 25), ('1080p', 'original'), ('4k', 60)])
def test_real_frames_layout_unicode_and_filter_characters(resolution, fps):
    text = music.Caption("Gość O'Connor: 100% \\ [x],; %{n}",
                         'Bardzo długi tytuł utworu ' * 12)
    image = render_frames([0], offset=4, resolution=resolution, fps=fps, text=text)[0]
    bounds = image.getbbox()
    assert bounds
    width, height = image.size
    assert bounds[0] >= int(width * .55) - 1
    assert bounds[2] <= width * .95 + 1
    assert bounds[1] > height * .65
    assert bounds[3] <= height * .9 + 1
    face = music.font(music.BOLD_FONT, round(40 * height / 1080))
    lines = music.wrap(text.title, face, width * .4 - 4 * height / 1080)
    assert len(lines) == 2 and lines[-1].endswith('…')


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required')
def test_filter_escaping_renders_the_exact_literal_text(tmp_path):
    text = "Zażółć: O'Connor \\ [x],; 100% %{n}"
    reference = tmp_path / 'caption.txt'
    reference.write_text(text, encoding='utf-8')
    frames = []
    for option in (f'textfile={reference}', f'text={music.escape_text(text)}'):
        result = subprocess.run(['ffmpeg', '-v', 'error', '-filter_threads', '1',
            '-f', 'lavfi', '-i', 'color=black:s=854x480:r=1', '-vf',
            f'drawtext=fontfile={music.FONT}:{option}:expansion=none:fontcolor=white:fontsize=20',
            '-frames:v', '1', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'],
            capture_output=True, timeout=10, check=True)
        frames.append(result.stdout)
    assert frames[0] == frames[1]
