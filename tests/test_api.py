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


async def test_channels_isolate_sources_settings_streams_and_cleanup(client, monkeypatch):
    from app.engine import Segment
    def refresh(source):
        app.state.db.replace_media(source['id'], 'Video', [
            {'url': source['url'], 'title': 'Video', 'duration': 600}])
    monkeypatch.setattr(app.state.sources, 'refresh', refresh)
    response = await client.post('/api/channels', json={'name': 'Second'})
    assert response.status_code == 201
    second = response.json()['id']
    other = app.state.channels[second]
    main = app.state.channels['main']
    assert other.sources is main.sources is app.state.sources
    assert other.upload_base != main.upload_base
    url = {'url': 'https://example.com/shared'}
    a = (await client.post('/api/sources', json=url)).json()['id']
    b = (await client.post(f'/api/channels/{second}/sources', json=url)).json()['id']
    assert a != b
    app.state.db.update_duration(second, url['url'], 900)
    assert app.state.db.media(second)[0]['duration'] == 900
    assert app.state.db.media('main')[0]['duration'] == 600
    assert (await client.post(f'/api/channels/{second}/sources', json=url)).status_code == 409
    await client.patch(f'/api/channels/{second}/settings', json={'finish_current_on_remove': True})
    assert other.finish_current_on_remove and not main.finish_current_on_remove
    first_status = (await client.get('/api/status')).json()
    second_status = (await client.get(f'/api/channels/{second}/status')).json()
    assert [s['id'] for s in first_status['sources']] == [a]
    assert [s['id'] for s in second_status['sources']] == [b]
    assert main.timeline.key != other.timeline.key
    assert main.task is None and other.task is None
    playlist = (await client.get('/playlist.m3u8')).text
    assert playlist.count('#EXTINF:') == 2 and f'/channels/{second}/index.m3u8' in playlist
    assert (await client.get(f'/channels/{second}/index.m3u8')).status_code == 307
    assert other.task is None
    async def producer():
        await asyncio.Event().wait()
    monkeypatch.setattr(other, 'run', producer)
    other.segments.append(Segment(0, 'clip', 4, b'second', 0))
    assert (await client.get('/channels/main/segments/0.ts')).status_code == 404
    assert (await client.get(f'/channels/{second}/segments/0.ts')).content == b'second'
    assert other.task is not None and main.task is None
    task, clock, uploads = other.task, other.clock_task, app.state.uploads[second]
    assert (await client.delete(f'/api/channels/{second}')).status_code == 204
    assert task.done() and clock.done() and not uploads.server.is_serving()
    assert not other.segments and not app.state.db.sources(second)
    assert app.state.db.setting(f'timeline:{second}') is None
    assert len(app.state.db.sources('main')) == 1
    assert (await client.get(f'/channels/{second}/index.m3u8')).status_code == 404
    assert (await client.delete('/api/channels/main')).status_code == 409


async def test_channel_validation_and_auth(client, monkeypatch):
    for name in ['', '   ', 'bad\nname', '<script>']:
        assert (await client.post('/api/channels', json={'name': name})).status_code == 422
    assert (await client.get('/api/channels/missing/status')).status_code == 404
    assert (await client.post('/api/channels/missing/sources', json={'url': 'https://example.com/v'})).status_code == 404
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    assert (await client.post('/api/channels', json={'name': 'Private'})).status_code == 401
    assert (await client.delete('/api/channels/main')).status_code == 401
    assert (await client.post('/api/channels', json={'name': 'Private'}, auth=('admin', 'secret'),
                              headers={'Origin': 'https://evil.example'})).status_code == 403


async def test_delete_channel_cancels_refresh_and_waiting_manifest(client, monkeypatch):
    second = (await client.post('/api/channels', json={'name': 'Waiting'})).json()['id']
    channel = app.state.channels[second]
    started = asyncio.Event()
    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(app.state.sources, '_refresh', blocked)
    response = await client.post(f'/api/channels/{second}/sources', json={'url': 'https://example.com/v'})
    source = response.json()['id']
    await started.wait()
    refresh_task = app.state.sources.tasks[source]
    app.state.db.replace_media(source, 'V', [{'url': 'https://example.com/v', 'title': 'V', 'duration': 600}])
    monkeypatch.setattr(channel, 'run', blocked)
    manifest = asyncio.create_task(client.get(f'/channels/{second}/index.m3u8?viewer=waiting'))
    try:
        async with asyncio.timeout(2):
            while not channel.waiters:
                await asyncio.sleep(.01)
        assert (await client.delete(f'/api/channels/{second}')).status_code == 204
        assert (await asyncio.wait_for(manifest, 2)).status_code == 404
        assert refresh_task.cancelled() and channel.waiters == 0 and channel.task is None
        await channel.touch('late-viewer')
        assert channel.task is None
    finally:
        manifest.cancel()
        await asyncio.gather(manifest, return_exceptions=True)


async def test_legacy_removal_setting_only_applies_to_main(client):
    app.state.db.set_setting('finish_current_on_remove', True)
    second = (await client.post('/api/channels', json={'name': 'New'})).json()['id']
    assert app.state.channels['main'].finish_current_on_remove
    assert not app.state.channels[second].finish_current_on_remove
    await client.patch('/api/settings', json={'finish_current_on_remove': False})
    assert not app.state.channels['main'].finish_current_on_remove


async def test_channels_and_timeline_survive_restart_without_restoring_deleted_main(client):
    second = (await client.post('/api/channels', json={'name': 'Persisted'})).json()['id']
    app.state.db.execute('INSERT INTO sources(id,channel_id,url) VALUES(?,?,?)',
                         ('persisted', second, 'https://example.com/v'))
    app.state.db.replace_media('persisted', 'V', [{'url': 'https://example.com/v', 'title': 'V', 'duration': 600}])
    app.state.channels[second].scheduled()
    saved = app.state.db.setting(f'timeline:{second}')
    assert (await client.delete('/api/channels/main')).status_code == 204
    reopened = db.Database()
    try:
        assert reopened.rows('SELECT id FROM channels') == [{'id': second}]
        from app.engine import Channel
        restored = Channel(second, reopened, app.state.sources)
        assert restored.timeline.state == saved
        assert restored.scheduled().starts_at == saved['epoch']
        assert restored.task is None
    finally:
        reopened.conn.close()


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


async def test_channel_fps_is_partial_persistent_and_isolated(client):
    from app.engine import Channel
    main = app.state.channel
    assert (await client.get('/api/status')).json()['fps'] == 60
    second = (await client.post('/api/channels', json={'name': 'Native'})).json()['id']
    path = f'/api/channels/{second}/settings'
    for fps in [24, 25, 30, 50, 60, 'original']:
        response = await client.patch(path, json={'fps': fps})
        assert response.status_code == 200
        assert response.json()['fps'] == fps
        assert (await client.get(f'/api/channels/{second}/status')).json()['fps'] == fps
        assert main.fps == 60
    await client.patch(path, json={'finish_current_on_remove': True})
    assert app.state.channels[second].fps == 'original'
    await client.patch(path, json={'fps': 25})
    assert app.state.channels[second].finish_current_on_remove
    assert app.state.channels[second].task is None
    reopened = db.Database()
    try:
        restored = Channel(second, reopened, app.state.sources)
        assert restored.fps == 25
    finally:
        reopened.conn.close()
    await client.delete(f'/api/channels/{second}')
    assert app.state.db.setting(f'fps:{second}') is None


@pytest.mark.parametrize('fps', [None, True, 0, -1, 26, 1000, '60', 'auto', 'nan'])
async def test_invalid_fps_is_rejected_without_changing_settings(client, fps):
    response = await client.patch('/api/settings', json={'fps': fps, 'finish_current_on_remove': True})
    assert response.status_code == 422
    assert app.state.channel.fps == 60
    assert not app.state.channel.finish_current_on_remove


async def test_fps_update_keeps_current_producer_and_schedule(client, monkeypatch):
    channel, stopped = await playing_fixture(monkeypatch)
    task, slot = channel.task, channel.scheduled()
    response = await client.patch('/api/settings', json={'fps': 'original'})
    assert response.status_code == 200 and channel.fps == 'original'
    assert channel.task is task and not stopped.is_set()
    assert len(channel.segments) == 2
    assert channel.scheduled().key == slot.key


async def test_fps_settings_require_auth_and_same_origin(client, monkeypatch):
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    path = '/api/channels/main/settings'
    assert (await client.patch(path, json={'fps': 25})).status_code == 401
    response = await client.patch(path, json={'fps': 25}, auth=('admin', 'secret'),
                                  headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    assert app.state.channel.fps == 60


async def test_resolution_is_persistent_isolated_and_removed_with_channel(client):
    from app.engine import Channel
    second = (await client.post('/api/channels', json={'name': '4K'})).json()['id']
    path = f'/api/channels/{second}/settings'
    assert (await client.get('/api/status')).json()['resolution'] == '1080p'
    for resolution in ['480p', '720p', '1080p', '4k']:
        response = await client.patch(path, json={'resolution': resolution})
        assert response.status_code == 200
        assert response.json()['resolution'] == resolution
        assert (await client.get(f'/api/channels/{second}/status')).json()['resolution'] == resolution
        assert app.state.channel.resolution == '1080p'
    await client.patch(path, json={'fps': 25})
    reopened = db.Database()
    try:
        restored = Channel(second, reopened, app.state.sources)
        assert restored.resolution == '4k' and restored.fps == 25
    finally:
        reopened.conn.close()
    await client.delete(f'/api/channels/{second}')
    assert app.state.db.setting(f'resolution:{second}') is None


@pytest.mark.parametrize('resolution', [None, True, 480, '2160p', '8k', 'original'])
async def test_invalid_resolution_is_atomic(client, resolution):
    response = await client.patch('/api/settings', json={'resolution': resolution, 'fps': 25})
    assert response.status_code == 422
    assert app.state.channel.resolution == '1080p' and app.state.channel.fps == 60


async def test_resolution_change_preserves_current_playback(client, monkeypatch):
    channel, stopped = await playing_fixture(monkeypatch)
    task, slot = channel.task, channel.scheduled()
    response = await client.patch('/api/settings', json={'resolution': '4k'})
    assert response.status_code == 200
    assert channel.task is task and not stopped.is_set()
    assert len(channel.segments) == 2 and channel.scheduled().key == slot.key


async def test_gpu_settings_validate_before_saving_and_apply_globally(client, monkeypatch):
    from app import gpu
    monkeypatch.setattr(gpu, 'inventory', lambda: [
        {'id': '/dev/dri/renderD129', 'label': 'AMD', 'encoders': ['vaapi'], 'accessible': True}])
    assert (await client.get('/api/gpu')).json()['devices'][0]['label'] == 'AMD'
    original = gpu.settings(app.state.db)
    for payload in [{'encoder': 'qsv', 'device': '/dev/dri/renderD129'},
                    {'encoder': 'vaapi', 'device': '/tmp/device'}, {'encoder': 'invalid'}]:
        assert (await client.patch('/api/gpu', json=payload)).status_code == 422
        assert gpu.settings(app.state.db) == original
    checked = []
    monkeypatch.setattr(gpu, 'validate', lambda encoder, device: checked.append((encoder, device)))
    payload = {'encoder': 'vaapi', 'device': '/dev/dri/renderD129'}
    assert (await client.patch('/api/gpu', json=payload)).json() == payload
    assert checked == [('vaapi', '/dev/dri/renderD129')]
    assert app.state.db.setting('gpu_engine') == payload
    second = (await client.post('/api/channels', json={'name': 'Second'})).json()['id']
    for channel_id in ['main', second]:
        assert (await client.get(f'/api/channels/{channel_id}/status')).json()['encoder'] == 'vaapi'
    assert (await client.patch('/api/gpu', json={'encoder': 'software'})).json()['device'] == ''


async def test_gpu_requires_auth_and_same_origin(client, monkeypatch):
    assert (await client.patch('/api/gpu', json={'encoder': 'software'},
                              headers={'origin': 'https://other.example'})).status_code == 403
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    assert (await client.get('/api/gpu')).status_code == 401
    assert (await client.patch('/api/gpu', json={'encoder': 'software'})).status_code == 401
