"""Music captions: metadata, media-relative timing and RAM-only text layout."""
from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from PIL import ImageFont

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
SUFFIX = re.compile(
    r'\s*[\[(]\s*(?:official\s+(?:music\s+)?video|official\s+audio|lyrics|hd|4k)\s*[\])]\s*$',
    re.IGNORECASE)
SEPARATOR = re.compile(r'\s+[-–—]\s+')


@dataclass(frozen=True)
class Caption:
    artist: str
    title: str


def clean(value):
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFC', value[:4096])
    value = ''.join(' ' if c.isspace() else c for c in value
                    if c.isspace() or not unicodedata.category(c).startswith('C'))
    return ' '.join(value.split())


def clean_title(value):
    value = clean(value)
    while SUFFIX.search(value):
        value = SUFFIX.sub('', value).strip()
    return value


def caption(info, item):
    """Never infer the artist from an uploader, or display a signed media URL."""
    artists = info.get('artists')
    artist = ', '.join(dict.fromkeys(filter(None, map(clean, artists)))) if isinstance(artists, list) else ''
    artist = clean(artist) or clean(info.get('artist'))
    track = clean(info.get('track'))
    title = next((clean_title(v) for v in (info.get('title'), item.get('title'))
                  if clean_title(v) and not clean(v).lower().startswith(('http://', 'https://'))), '')
    if not title:
        filename = unquote(urlsplit(item.get('url', '')).path.rsplit('/', 1)[-1])
        if re.search(r'\.(?:mp4|mkv|webm|mov|m4v|mp3|m4a|ogg|opus|flac|wav)$', filename, re.I):
            title = clean_title(filename.rsplit('.', 1)[0])
    parts = SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and all(parts):
        candidate_artist, candidate_track = parts
        if not artist and not track:
            artist, track = candidate_artist, candidate_track
        elif artist and not track and artist.casefold() == candidate_artist.casefold():
            track = candidate_track
        elif track and not artist and track.casefold() == candidate_track.casefold():
            artist = candidate_artist
    track = track or title
    return Caption(artist, track) if track else None


def windows(end=None):
    """Half-open intervals on the clip clock, clipped and merged before fading."""
    if end is None:
        return [(3.0, 13.0)]
    if end <= 6:
        return []
    result = []
    for start, stop in sorted(((3.0, 13.0), (end - 13.0, end - 3.0))):
        start, stop = max(3.0, start), min(end - 3.0, stop)
        if stop <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], stop))
        else:
            result.append((start, stop))
    return result


@lru_cache(maxsize=16)
def font(path, size):
    return ImageFont.truetype(path, size)


def ellipsize(text, face, width):
    if face.getlength(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if face.getlength(text[:middle].rstrip() + '…') <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + '…'


def wrap(text, face, width):
    if face.getlength(text) <= width:
        return [text]
    words = text.split()
    first = ''
    while words and face.getlength((first + ' ' + words[0]).strip()) <= width:
        first = (first + ' ' + words.pop(0)).strip()
    if not first:
        # A single unbreakable word is truncated rather than overflowing.
        return [ellipsize(text, face, width)]
    return [first, ellipsize(' '.join(words), face, width)]


def escape_text(text):
    # FFmpeg consumes two levels: filter options, then the filtergraph.
    # No shell is involved. expansion=none also disables %{...} evaluation.
    for special in ("\\:'", "\\'[],;"):
        text = ''.join('\\' + c if c in special else c for c in text)
    return text


def filters(value, width, height, offset=0.0, end=None):
    intervals = windows(end)
    if not value or not intervals:
        return ''
    scale = height / 1080
    regular_size, bold_size = max(1, round(32 * scale)), max(1, round(40 * scale))
    border, gap = max(1, round(2 * scale)), max(1, round(8 * scale))
    available = width * .4 - 2 * border
    lines = []
    if value.artist:
        lines.append((ellipsize(value.artist, font(FONT, regular_size), available), FONT, regular_size))
    lines.extend((line, BOLD_FONT, bold_size) for line in wrap(value.title, font(BOLD_FONT, bold_size), available))
    # Use font metrics for stable spacing, including accents and descenders.
    heights = [sum(font(path, size).getmetrics()) for _, path, size in lines]
    y = round(height * .9) - sum(heights) - gap * (len(lines) - 1) - border
    clock = f'(t+{offset:.6f})'
    alpha = []
    for start, stop in intervals:
        fade = min(.3, (stop - start) / 2)
        alpha.append(f'if(gte({clock},{start:.6f})*lt({clock},{stop:.6f}),'
                     f'min(1,min(({clock}-{start:.6f})/{fade:.9f},({stop:.6f}-{clock})/{fade:.9f})),0)')
    expression = '+'.join(alpha)
    output = []
    for (text, path, size), line_height in zip(lines, heights):
        output.append(f"drawtext=fontfile={path}:text={escape_text(text)}:expansion=none:"
                      f"fontcolor=white:fontsize={size}:borderw={border}:bordercolor=black:"
                      f"x={round(width * .55) + border}:y={y}:alpha='{expression}'")
        y += line_height + gap
    return ','.join(output)
