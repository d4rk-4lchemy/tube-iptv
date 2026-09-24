"""Persistent uploaded cookies, with private per-extraction working copies."""
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar
from io import StringIO
import os
from pathlib import Path
import tempfile
import warnings

from . import config

MAX_COOKIE_BYTES = 2 * 1024 * 1024


def cookie_path():
    return config.DATA / 'cookies.txt'


def save_cookies(raw):
    error = 'Upload a valid Netscape cookies.txt file containing at least one cookie.'
    try:
        text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        if '\x00' in text or text.split('\n', 1)[0] not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
            raise ValueError(error)
        for line in text.splitlines()[1:]:
            if line.startswith('#HttpOnly_'):
                line = line[len('#HttpOnly_'):]
            if not line.strip() or line.startswith('#'):
                continue
            fields = line.split('\t')
            if (len(fields) != 7 or not fields[0] or fields[1] not in ('TRUE', 'FALSE')
                    or fields[3] not in ('TRUE', 'FALSE')
                    or (fields[4] and not fields[4].isascii())
                    or (fields[4] and not fields[4].isdigit())):
                raise ValueError(error)
        jar = MozillaCookieJar()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            jar._really_load(StringIO(text), '<upload>', True, True)
        if not len(jar):
            raise ValueError(error)
    except Exception:
        # Parser errors may contain credentials. Never return them to the API/log.
        raise ValueError(error) from None
    target = cookie_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.cookies-', delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(text.encode('utf-8'))
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def extraction_cookies():
    try:
        raw = cookie_path().read_bytes()
    except FileNotFoundError:
        yield []
        return
    # yt-dlp writes back to its cookie jar. Isolate concurrent jobs and uploads.
    with tempfile.NamedTemporaryFile(prefix='tube-cookies-', suffix='.txt') as file:
        file.write(raw)
        file.flush()
        yield ['--cookies', file.name]
