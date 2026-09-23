import hashlib
import httpx
import pytest
from app import versions
from app.db import Database


async def test_bad_download_keeps_previous_version(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, 'DATA', tmp_path)
    manager = versions.Versions(Database(tmp_path / 'db'))
    previous = manager.current()
    real_client = httpx.AsyncClient
    def response(request):
        body = b'corrupt' if request.url.path.endswith('/yt-dlp') else b'0000  yt-dlp\n'
        return httpx.Response(200, content=body)
    monkeypatch.setattr(versions.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(response), **kw))
    await manager.install('nightly', '2026.01.01')
    assert manager.job['state'] == 'error'
    assert manager.current() == previous


async def test_nightly_to_stable_and_validation_before_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, 'DATA', tmp_path)
    manager = versions.Versions(Database(tmp_path / 'db'))
    binary = b'official zipapp fixture'
    real_client = httpx.AsyncClient
    def response(request):
        body = binary if request.url.path.endswith('/yt-dlp') else (hashlib.sha256(binary).hexdigest() + '  yt-dlp\n').encode()
        return httpx.Response(200, content=body)
    monkeypatch.setattr(versions.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(response), **kw))
    async def check(command, timeout):
        assert manager.current()['channel'] == 'stable'
        return '2026.01.01'
    monkeypatch.setattr(versions, 'capture', check)
    await manager.install('nightly', '2026.01.01')
    assert manager.current()['channel'] == 'nightly'
    async def check_stable(command, timeout):
        assert manager.current()['channel'] == 'nightly'
        return '2026.01.02'
    monkeypatch.setattr(versions, 'capture', check_stable)
    await manager.install('stable', '2026.01.02')
    assert manager.current()['channel'] == 'stable'
    assert manager.job['state'] == 'done'
