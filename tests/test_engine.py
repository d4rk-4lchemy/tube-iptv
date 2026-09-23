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
