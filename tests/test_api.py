import asyncio
import pytest
import httpx
from app import config, db, versions
from app.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.setattr(versions, 'DATA', tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield client


async def test_playlist_does_not_start_engine_and_empty_stream_fails(client):
    response = await client.get('/playlist.m3u8')
    assert '#EXTINF:-1 tvg-id="main"' in response.text
    assert app.state.channel.task is None
    assert (await client.get('/channels/main/index.m3u8')).status_code == 503
    assert app.state.channel.task is None


async def test_sources_validate_deduplicate_refresh_and_delete(client, monkeypatch):
    def refresh(source):
        app.state.db.replace_media(source['id'], 'Example', [{"title": 'Video', "url": source['url']}])
    monkeypatch.setattr(app.state.sources, 'refresh', refresh)
    assert (await client.post('/api/sources', json={"url": "file:///etc/passwd"})).status_code == 422
    response = await client.post('/api/sources', json={"url": "https://example.com/video"})
    assert response.status_code == 202
    source = response.json()['id']
    assert (await client.post('/api/sources', json={"url": "https://example.com/video"})).status_code == 409
    assert (await client.get('/api/status')).json()['media_count'] == 1
    await client.patch(f'/api/sources/{source}', json={"enabled": False})
    assert (await client.get('/api/status')).json()['media_count'] == 0
    await client.delete(f'/api/sources/{source}')
    assert app.state.db.media() == []
    assert (await client.get('/api/status')).json()['sources'] == []


async def test_admin_stream_tokens_and_cross_origin_mutation(client, monkeypatch):
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    monkeypatch.setattr(config, 'STREAM_TOKEN', 'stream-secret')
    assert (await client.get('/api/status')).status_code == 401
    assert (await client.get('/api/status', auth=('admin', 'secret'))).status_code == 200
    assert (await client.get('/playlist.m3u8')).status_code == 403
    assert (await client.get('/playlist.m3u8?token=stream-secret')).status_code == 200
    response = await client.patch('/api/channel', json={"name": "Injected"}, auth=('admin', 'secret'), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


async def test_internal_ingest_is_private(client):
    response = await client.put('/internal/wrong/clip/000001.ts', content=b'video')
    assert response.status_code == 403


async def playing_fixture(monkeypatch, with_other=False):
    from types import SimpleNamespace
    from app.engine import Segment
    db = app.state.db
    channel = app.state.channel
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('playing','main','https://example.com/a')")
    db.replace_media('playing', 'A', [{'url': 'https://example.com/a', 'title': 'A', 'duration': 600}])
    slot = channel.scheduled()
    if with_other:
        db.execute("INSERT INTO sources(id,channel_id,url) VALUES('other','main','https://example.com/b')")
        db.replace_media('other', 'B', [{'url': 'https://example.com/b', 'title': 'B', 'duration': 600}])
        channel.scheduled()
    channel.now = slot.item
    channel.clip = 'playing-clip'
    process = SimpleNamespace(returncode=None)
    channel.process = process
    stopped = asyncio.Event()
    async def producer():
        try:
            await asyncio.Event().wait()
        finally:
            process.returncode = 0
            channel.process = None
            channel.clip = None
            stopped.set()
    channel.task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    # Next producer intentionally performs no network operations.
    async def next_producer():
        try:
            await asyncio.Event().wait()
        finally:
            channel.process = None
    monkeypatch.setattr(channel, 'run', next_producer)
    for index in range(2):
        channel.segments.append(Segment(index, channel.clip, 4, b'G' * 188, 0, source_url=slot.item['url']))
    channel.bytes = 376
    channel.sequence = 2
    channel.viewers['test-viewer'] = __import__('time').monotonic()
    return channel, stopped


async def test_delete_playing_source_stops_and_rebuilds_by_default(client, monkeypatch):
    channel, stopped = await playing_fixture(monkeypatch, with_other=True)
    response = await client.delete('/api/sources/playing')
    assert response.status_code == 204
    assert stopped.is_set()
    assert not channel.segments and not channel.pending and channel.bytes == 0
    assert channel.stream_revision == 1
    assert channel.scheduled().item['url'] == 'https://example.com/b'
    assert (await client.get('/channels/main/segments/0.ts')).status_code == 404


async def test_keep_only_current_after_last_source_removed_and_switch_off(client, monkeypatch):
    response = await client.patch('/api/settings', json={'finish_current_on_remove': True})
    assert response.status_code == 200
    channel, stopped = await playing_fixture(monkeypatch)
    assert (await client.delete('/api/sources/playing')).status_code == 204
    assert not stopped.is_set() and channel.retained_clip == 'playing-clip'
    assert app.state.db.media() == []
    assert channel.available() and len(channel.segments) == 2
    assert (await client.get('/channels/main/index.m3u8?viewer=tail')).status_code == 200
    assert (await client.patch('/api/settings', json={'finish_current_on_remove': False})).status_code == 200
    assert stopped.is_set() and channel.bytes == 0
    assert not channel.available()
    assert (await client.get('/channels/main/index.m3u8?viewer=tail')).status_code == 503


async def test_disable_playing_source_also_revokes_segments(client, monkeypatch):
    channel, stopped = await playing_fixture(monkeypatch)
    assert (await client.patch('/api/sources/playing', json={'enabled': False})).status_code == 200
    assert stopped.is_set() and channel.bytes == 0
    assert not channel.available()


async def test_refresh_pruning_current_material_revokes_it(client, monkeypatch):
    channel, stopped = await playing_fixture(monkeypatch)
    app.state.db.replace_media('playing', 'Changed playlist', [{'url': 'https://example.com/b', 'title': 'B', 'duration': 600}])
    await channel.reconcile_sources()
    assert stopped.is_set() and channel.bytes == 0
    assert channel.scheduled().item['url'] == 'https://example.com/b'


async def test_overlapping_source_can_still_supply_current_video(client, monkeypatch):
    channel, stopped = await playing_fixture(monkeypatch)
    app.state.db.execute("INSERT INTO sources(id,channel_id,url) VALUES('duplicate','main','https://example.com/playlist')")
    app.state.db.replace_media('duplicate', 'Duplicate', [{'url': 'https://example.com/a', 'title': 'A', 'duration': 600}])
    assert (await client.delete('/api/sources/playing')).status_code == 204
    assert not stopped.is_set() and channel.bytes == 376


async def test_retained_tail_remains_available_as_finished_hls(client, monkeypatch):
    await client.patch('/api/settings', json={'finish_current_on_remove': True})
    channel, stopped = await playing_fixture(monkeypatch)
    await client.delete('/api/sources/playing')
    channel.task.cancel()
    await asyncio.gather(channel.task, return_exceptions=True)
    channel.state = 'ended'
    channel.now = None
    await channel.reconcile_sources()
    response = await client.get('/channels/main/index.m3u8?viewer=tail')
    assert response.status_code == 200 and '#EXT-X-ENDLIST' in response.text
    assert len(channel.segments) == 2


async def test_upload_disconnect_is_reported_without_publishing_partial_media(client):
    from starlette.requests import ClientDisconnect
    channel = app.state.channel
    channel.clip = 'upload'
    async def body():
        yield b'partial'
        raise ClientDisconnect()
    response = await client.put(f'/internal/{channel.secret}/upload/000000.ts', content=body())
    assert response.status_code == 499
    assert not channel.pending and not channel.segments
    assert channel.metrics['upload_aborts'] == 1
