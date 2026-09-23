import asyncio
from pathlib import Path
import pytest
from app.db import Database
from app.engine import Channel
from app import config


def test_source_replacement_is_atomic_and_delete_cascades(tmp_path):
    db = Database(tmp_path / 'test.sqlite')
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com')")
    db.replace_media('s', 'playlist', [{"url": 'https://example.com/1', "title": 'one'}])
    assert len(db.media()) == 1
    db.replace_media('s', 'playlist', [{"url": 'https://example.com/2', "title": 'two'}])
    assert [m['title'] for m in db.media()] == ['two']
    db.execute("DELETE FROM sources WHERE id='s'")
    assert db.media() == []
    db.conn.close()


async def upload(channel, clip, index, duration=4):
    channel.clip = clip
    name = f'{index:06d}.ts'
    await channel.ingest(clip, name, bytes([0x47]) * 188)
    await channel.ingest(clip, 'index.m3u8', f'#EXTM3U\n#EXTINF:{duration},\n{name}\n'.encode())


async def test_hls_discontinuity_eviction_and_stale_upload():
    channel = Channel('main', None, None)
    await upload(channel, 'a', 0)
    await upload(channel, 'a', 1)
    channel.published.clear()
    await upload(channel, 'b', 0, 1.4)
    manifest = channel.manifest('viewer')
    assert '#EXT-X-DISCONTINUITY\n' in manifest
    assert '#EXTINF:1.400000' in manifest
    assert '#EXT-X-DISCONTINUITY-SEQUENCE:0' in manifest
    assert not await channel.ingest('a', '000010.ts', b'x')
    for i in range(1, 30):
        await upload(channel, 'b', i)
    assert len(channel.segments) == 12
    assert channel.bytes == 188 * 12
    assert '#EXT-X-DISCONTINUITY-SEQUENCE:1' in channel.manifest('viewer')
    assert '#EXT-X-DISCONTINUITY\n' not in channel.manifest('viewer')


async def test_last_viewer_stops_producer_and_frees_buffer(monkeypatch):
    channel = Channel('main', None, None)
    cancelled = asyncio.Event()
    async def producer():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()
    channel.run = producer
    monkeypatch.setattr(config, 'IDLE_SECONDS', .05)
    await channel.touch('one')
    await upload(channel, 'a', 0)
    await asyncio.wait_for(cancelled.wait(), 3)
    await channel.monitor
    assert channel.bytes == 0 and not channel.segments
    assert channel.task is None and channel.state == 'idle'


async def test_missing_upload_marks_gap_and_preserves_program_time():
    channel = Channel('main', None, None)
    channel.clip_time = 1000
    await upload(channel, 'a', 0)
    await channel.ingest('a', '000002.ts', b'G' * 188)
    await channel.ingest('a', 'index.m3u8', b'#EXTM3U\n#EXTINF:4,\n000000.ts\n#EXTINF:4,\n000001.ts\n#EXTINF:4,\n000002.ts\n')
    assert len(channel.segments) == 1  # The missing body may still be in flight.
    await channel.upload_aborted('a', '000001.ts')
    assert len(channel.segments) == 2
    first, second = channel.segments
    assert second.program_time - first.program_time == 8
    assert second.discontinuity == first.discontinuity + 1
    assert '#EXT-X-DISCONTINUITY\n' in channel.manifest('test')
    assert channel.metrics['lost_segments'] == 1
    assert channel.clip_duration == 12


async def test_manifest_before_upload_waits_without_losing_or_reordering():
    channel = Channel('main', None, None)
    channel.clip = 'a'
    channel.clip_time = 1000
    await channel.ingest('a', 'index.m3u8', b'#EXTM3U\n#EXTINF:4,\n000000.ts\n#EXTINF:4,\n000001.ts\n')
    assert not channel.segments
    await channel.ingest('a', '000001.ts', b'B' * 188)
    assert not channel.segments
    await channel.ingest('a', '000000.ts', b'A' * 188)
    assert [s.data[:1] for s in channel.segments] == [b'A', b'B']
    assert [s.program_time for s in channel.segments] == [1000, 1004]
    assert channel.metrics.get('lost_segments', 0) == 0
    assert '#EXT-X-DISCONTINUITY\n' not in channel.manifest('test')


async def test_endlist_waits_for_its_final_segment_body():
    channel = Channel('main', None, None)
    channel.clip = 'final'
    await channel.ingest('final', 'index.m3u8', b'#EXTM3U\n#EXTINF:1,\n000000.ts\n#EXT-X-ENDLIST\n')
    assert not channel.ingest_finished.is_set()
    await channel.ingest('final', '000000.ts', b'G' * 188)
    assert channel.ingest_finished.is_set()
    assert len(channel.segments) == 1


async def test_missing_segment_expires_when_it_leaves_upstream_window():
    channel = Channel('main', None, None)
    channel.clip_time = 1000
    await upload(channel, 'a', 0)
    await channel.ingest('a', '000002.ts', b'B' * 188)
    await channel.ingest('a', 'index.m3u8', b'#EXTM3U\n#EXTINF:4,\n000002.ts\n')
    assert [s.program_time for s in channel.segments] == [1000, 1008]
    assert channel.metrics['lost_segments'] == 1
    assert '#EXT-X-DISCONTINUITY\n' in channel.manifest('test')


async def test_loading_handover_keeps_order_and_rejects_late_slate():
    from app.slate import LoadingSlate
    channel = Channel('main', None, None)
    loading = channel.loading = LoadingSlate(channel)
    await upload(loading.ingester, loading.clip, 0)
    await upload(loading.ingester, loading.clip, 1)
    assert len(channel.segments) == 2
    assert channel.metrics['loading_ready_seconds'] >= 0
    assert 'first_segment_seconds' not in channel.metrics
    channel.now = {'url': 'https://example.com/video'}
    await upload(channel, 'video', 0)
    assert not loading.active
    assert not await loading.ingest('000002.ts', b'G' * 188)
    await upload(channel, 'video', 1)
    assert [s.sequence for s in channel.segments] == [0, 1, 2, 3]
    assert [s.source_url for s in channel.segments] == [None, None] + [channel.now['url']] * 2
    assert channel.manifest('test').count('#EXT-X-DISCONTINUITY\n') == 1
    await loading.close()


async def test_pending_loading_publication_cannot_follow_real_video():
    from app.slate import LoadingSlate
    channel = Channel('main', None, None)
    loading = channel.loading = LoadingSlate(channel)
    await upload(loading.ingester, loading.clip, 0)
    await upload(loading.ingester, loading.clip, 1)
    loading.next_at = asyncio.get_running_loop().time() + .05
    pending = asyncio.create_task(upload(loading.ingester, loading.clip, 2))
    await asyncio.sleep(.01)
    await upload(channel, 'real', 0)
    await pending
    assert [s.clip for s in channel.segments] == [loading.clip, loading.clip, 'real']
    await loading.close()


async def test_next_source_prefetch_is_cancelled_and_reaped(tmp_path):
    import time
    db = Database(tmp_path / 'prefetch.sqlite')
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com/list')")
    db.replace_media('s', 'Test', [{'url': 'https://example.com/a', 'title': 'A', 'duration': 10},
                                 {'url': 'https://example.com/b', 'title': 'B', 'duration': 10}])
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = []
    class Source:
        async def resolve(self, url):
            calls.append(url)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    channel = Channel('main', db, Source())
    slot = channel.scheduled()
    channel.prefetch_task = asyncio.create_task(channel.prefetch_next(slot))
    await asyncio.wait_for(started.wait(), 1)
    assert calls == [channel.timeline.position(slot.ends_at + .001).item['url']]
    assert calls[0] != slot.item['url']
    await channel.cancel_prefetch()
    assert stopped.is_set() and channel.prefetch_task is None and channel.prefetch_key is None
    db.conn.close()


def test_source_end_advances_channel_clock_without_waiting_for_boundary(tmp_path):
    db = Database(tmp_path / 'advance.sqlite')
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com/list')")
    db.replace_media('s', 'Test', [{'url': 'https://example.com/a', 'title': 'A', 'duration': 60},
                                   {'url': 'https://example.com/b', 'title': 'B', 'duration': 90}])
    now = [1000.]
    channel = Channel('main', db, None)
    channel.timeline.clock = lambda: now[0]
    current = channel.scheduled()
    expected_next = channel.timeline.position(current.ends_at + .001)

    now[0] = 1025.
    assert channel.advance_after_source(current, 'Source ended before its scheduled boundary')

    active = channel.scheduled()
    assert active.item['url'] == expected_next.item['url']
    assert active.starts_at == now[0]
    assert any('switching immediately' in event['text'] for event in channel.events)
    db.conn.close()


@pytest.mark.parametrize('exit_code,duration', [(0, 100), (1, 100), (0, 12)])
async def test_recovery_and_handoff_keep_media_cursor_without_draining(tmp_path, monkeypatch, exit_code, duration):
    from app.slate import LoadingSlate
    db = Database(tmp_path / 'recovery.sqlite')
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com/list')")
    db.replace_media('s', 'Test', [{'url': 'https://example.com/a', 'title': 'A', 'duration': duration}])
    class Sources:
        async def resolve(self, url):
            return {'duration': duration}, [{'url': url, 'vcodec': 'h264', 'acodec': 'aac'}]
    channel = Channel('main', db, Sources())
    monkeypatch.setattr(LoadingSlate, 'start', lambda self: None)
    launched = asyncio.Event()
    final_upload = asyncio.Event()
    commands = []
    class Process:
        returncode = None
        def __init__(self, first):
            self.first = first
            self.stderr = self.lines()
        async def lines(self):
            if self.first:
                for i in range(3):
                    await upload(channel, channel.clip, i)
                async def finish_upload():
                    await asyncio.sleep(.03)
                    await channel.ingest(channel.clip, 'index.m3u8', b'#EXTM3U\n#EXT-X-ENDLIST\n')
                    final_upload.set()
                if exit_code == 0:
                    asyncio.create_task(finish_upload())
                self.returncode = exit_code
            else:
                if exit_code == 0:
                    assert final_upload.is_set(), 'Encoder advanced before its final upload was accepted'
                launched.set()
                await asyncio.Event().wait()
            if False:
                yield b''
        async def wait(self):
            return self.returncode
    async def launch(*command, **kwargs):
        commands.append(command)
        return Process(len(commands) == 1)
    async def stop(process):
        process.returncode = 0
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr('app.engine.stop_process', stop)
    task = asyncio.create_task(channel.run())
    try:
        await asyncio.wait_for(launched.wait(), 2)
        def seek(command):
            return float(command[command.index('-ss') + 1]) if '-ss' in command else 0
        expected = seek(commands[0]) + 12 if duration == 100 else 0
        assert seek(commands[1]) == pytest.approx(expected, abs=0.001)
        assert channel.media_buffer.count == 3
        assert not channel.segments
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        db.conn.close()
