import asyncio
from pathlib import Path

import httpx
import pytest

from app import config, sources
from app.cookies import MAX_COOKIE_BYTES, cookie_path, extraction_cookies, save_cookies
from app.main import app

COOKIES = b'# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsession\tprivate-value\n'


@pytest.fixture(autouse=True)
def isolated_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', '')


async def test_upload_validation_auth_and_persistence():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.put('/api/cookies', content=COOKIES.replace(b'\n', b'\r\n'))
        assert response.json() == {'cookies_uploaded': True}
        assert cookie_path().read_bytes() == COOKIES
        assert cookie_path().stat().st_mode & 0o777 == 0o600
        for invalid in [b'{}', b'', b'# Netscape HTTP Cookie File\n', COOKIES + b'bad\tprivate-value\n', COOKIES.replace(b'TRUE', b'INVALID'), b'\xff']:
            response = await client.put('/api/cookies', content=invalid)
            assert response.status_code == 422
            assert 'private-value' not in response.text
            assert cookie_path().read_bytes() == COOKIES
        assert (await client.put('/api/cookies', content=b'x' * (MAX_COOKIE_BYTES + 1))).status_code == 413
        assert cookie_path().read_bytes() == COOKIES
        # A fresh store reader sees persisted data without any process state.
        with extraction_cookies() as args:
            assert Path(args[1]).read_bytes() == COOKIES
        assert not Path(args[1]).exists()


async def test_upload_requires_admin_and_same_origin(monkeypatch):
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.put('/api/cookies', content=COOKIES)).status_code == 401
        assert (await client.put('/api/cookies', content=COOKIES, auth=('admin', 'secret'), headers={'Origin': 'https://evil.example'})).status_code == 403
        assert not cookie_path().exists()
        assert (await client.put('/api/cookies', content=COOKIES, auth=('admin', 'secret'))).status_code == 200


async def test_extraction_snapshot_and_cancellation(monkeypatch):
    save_cookies(COOKIES)
    paths = []
    started = asyncio.Event()

    async def capture(command, timeout):
        path = Path(command[command.index('--cookies') + 1])
        paths.append(path)
        assert path.read_bytes() == COOKIES
        assert path.stat().st_mode & 0o777 == 0o600
        save_cookies(COOKIES.replace(b'private-value', b'replacement'))
        path.write_text('extractor writeback')
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sources, 'capture', capture)
    source = sources.Sources(None, None)
    monkeypatch.setattr(source, 'base', lambda: ['yt-dlp'])
    task = asyncio.create_task(source.extract(['--dump-single-json', '--', 'https://example.com'], 90))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not path.exists() for path in paths)
    assert b'replacement' in cookie_path().read_bytes()


async def test_no_cookies_and_http_only_session_cookies():
    with extraction_cookies() as args:
        assert args == []
    save_cookies(COOKIES.replace(b'.example.com', b'#HttpOnly_.example.com').replace(b'\t0\t', b'\t\t'))
    with extraction_cookies() as first, extraction_cookies() as second:
        assert first[1] != second[1]
        assert Path(first[1]).read_bytes() == Path(second[1]).read_bytes()


async def test_delete_cookies_and_active_snapshot(monkeypatch):
    save_cookies(COOKIES)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
        assert (await client.delete('/api/cookies')).status_code == 401
        assert (await client.delete('/api/cookies', auth=('admin', 'secret'), headers={'Origin': 'https://evil.example'})).status_code == 403
        assert cookie_path().exists()
        with extraction_cookies() as active:
            response = await client.delete('/api/cookies', auth=('admin', 'secret'))
            assert response.json() == {'cookies_uploaded': False}
            assert not cookie_path().exists()
            assert Path(active[1]).read_bytes() == COOKIES
            with extraction_cookies() as new:
                assert new == []
        assert not Path(active[1]).exists()
        assert (await client.delete('/api/cookies', auth=('admin', 'secret'))).status_code == 200
