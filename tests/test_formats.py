from yt_dlp import YoutubeDL
from app.sources import FORMAT_SELECTOR


def choose(formats, height=1080):
    with YoutubeDL({'quiet': True, 'skip_download': True, 'format': FORMAT_SELECTOR.replace('1080', str(height))}) as ydl:
        info = ydl.process_ie_result({'id': 'fixture', 'title': 'fixture', 'formats': formats}, download=False)
    return [f['format_id'] for f in info.get('requested_formats', [info])]


def fmt(name, height=None, protocol='https', video='avc1', audio='none', preference=0):
    return {'format_id': name, 'url': f'https://example.com/{name}.mp4', 'ext': 'mp4',
            'height': height, 'protocol': protocol, 'vcodec': video, 'acodec': audio,
            'preference': preference}


def test_seekable_1080_preferred_over_higher_ranked_hls_and_4k():
    assert choose([fmt('720', 720), fmt('1080', 1080), fmt('4k', 2160),
                   fmt('hls-premium', 1080, 'm3u8_native', 'vp9', preference=10),
                   fmt('audio', video='none', audio='aac')]) == ['1080', 'audio']


def test_hls_only_sources_still_supported():
    assert choose([fmt('hls', 1080, 'm3u8_native', audio='aac')]) == ['hls']


def test_muxed_and_audio_only_sources_still_supported():
    assert choose([fmt('muxed', 480, audio='aac')]) == ['muxed']
    assert choose([fmt('audio', video='none', audio='aac')]) == ['audio']


def test_4k_channel_selects_native_4k_source():
    assert choose([fmt('1080', 1080), fmt('4k', 2160), fmt('8k', 4320),
                   fmt('audio', video='none', audio='aac')], 2160) == ['4k', 'audio']
