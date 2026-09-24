from copy import deepcopy
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app import config, epg
from app.db import Database
from app.main import app
from app.timeline import Timeline
from test_api import client, playing_fixture
from test_timeline import items


def programmes(response):
    return ET.fromstring(response.content).findall('programme')


def seconds(value):
    return datetime.strptime(value, '%Y%m%d%H%M%S %z').timestamp()


@pytest.mark.parametrize('count', [1, 2, 5])
def test_slots_match_clock_across_cycles_and_restart(tmp_path, count):
    db = Database(tmp_path / 'db')
    timeline = Timeline(db, 'main')
    timeline.sync(items(*[10.25 + i for i in range(count)]), now=1000)
    before = deepcopy(timeline.state)
    for start in [1000, 1003, 1200, 1_000_000]:
        slots = list(timeline.slots(start, start + 120))
        assert slots[0] == timeline.position(start)
        assert slots[-1].starts_at < start + 120 <= slots[-1].ends_at
        for slot in slots:
            assert timeline.position((slot.starts_at + slot.ends_at) / 2) == slot
        assert all(a.ends_at == b.starts_at for a, b in zip(slots, slots[1:]))
    assert timeline.state == before == db.setting(timeline.key)
    restored = Timeline(db, 'main')
    assert list(restored.slots(1003, 1300)) == list(timeline.slots(1003, 1300))
    assert list(timeline.slots(1000, 1000)) == []


def test_slots_handle_lead_removal_correction_and_early_end(tmp_path):
    timeline = Timeline(Database(tmp_path / 'db'), 'main')
    first = timeline.sync(items(600, 600), now=1000)
    other = [item for item in items(600, 600) if item['url'] != first.item['url']]
    timeline.sync(other, now=1100, preserve_removed=True)
    slots = list(timeline.slots(1100, 2300))
    assert slots[0] == first
    assert slots[1].item['url'] == other[0]['url']
    assert slots[1].starts_at == first.ends_at
    advanced = timeline.advance(first, now=1150)
    assert list(timeline.slots(1150, 1160)) == [advanced]
    timeline.sync([], now=1151, preserve_removed=True)
    assert len(list(timeline.slots(1151, 10000))) == 1
    timeline.sync([], now=1152)
    assert list(timeline.slots(1152, 10000)) == []
    timeline.sync(items(None), now=2000)
    assert list(timeline.slots(2100, 2200))[0].ends_at == 5600
    timeline.sync(items(600), now=2100)
    assert list(timeline.slots(2100, 2200))[0].ends_at == 2600


def test_xml_snapshot_unicode_precision_and_no_source_urls(tmp_path):
    timeline = Timeline(Database(tmp_path / 'db'), 'main')
    timeline.sync([{'url': 'https://example.com/private', 'title': 'Żółć & <film> 😀\x00\ufffe', 'duration': .25}], now=1000)
    snapshot = timeline.snapshot()
    timeline.sync([], now=1001)
    body = b''.join(epg.xmltv([({'id': 'main', 'name': 'TV & <ż>\x01'}, snapshot)], 1000, 1002))
    root = ET.fromstring(body)
    assert root.findtext('channel/display-name') == 'TV & <ż>'
    rows = root.findall('programme')
    assert len(rows) == 2
    assert all(row.findtext('title') == 'Żółć & <film> 😀' for row in rows)
    assert all(seconds(row.get('stop')) - seconds(row.get('start')) == 1 for row in rows)
    assert all(row.get('start').endswith(' +0000') for row in rows)
    assert b'private' not in body


async def test_export_horizon_full_boundaries_and_no_media_activity(client, monkeypatch):
    channel = app.state.channel
    async def forbidden(*args, **kwargs):
        raise AssertionError('EPG must not start media or viewer activity')
    monkeypatch.setattr(channel, 'touch', forbidden)
    monkeypatch.setattr(channel, 'run', forbidden)
    monkeypatch.setattr(app.state.sources, 'resolve', forbidden)
    db = app.state.db
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com/list')")
    db.replace_media('s', 'List', items(10000))
    channel.scheduled(now=1000)
    monkeypatch.setattr('app.main.time.time', lambda: 1120)
    before = deepcopy(channel.timeline.state)
    response = await client.get('/epg.xml')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/xml; charset=utf-8'
    assert response.headers['cache-control'] == 'no-store'
    rows = programmes(response)
    assert seconds(rows[0].get('start')) == 1000
    horizon = 1120 + 2 * 86400
    assert seconds(rows[-1].get('start')) < horizon <= seconds(rows[-1].get('stop'))
    assert all(a.get('stop') == b.get('start') for a, b in zip(rows, rows[1:]))
    assert channel.timeline.state == before == db.setting(channel.timeline.key)
    assert channel.task is None and channel.process is None and not channel.viewers
    assert not channel.segments and channel.bytes == 0
    await client.patch('/api/epg/settings', json={'days': 7})
    longer = programmes(await client.get('/epg.xml'))
    assert len(longer) > len(rows)
    assert seconds(longer[-1].get('stop')) >= 1120 + 7 * 86400


async def test_channels_empty_renamed_deleted_and_urls(client, monkeypatch):
    monkeypatch.setattr(config, 'PUBLIC_URL', 'https://tv.example/base')
    monkeypatch.setattr(config, 'STREAM_TOKEN', 'a+b&c?/')
    second = (await client.post('/api/channels', json={'name': 'Second'})).json()['id']
    await client.patch('/api/channels/main', json={'name': 'Changed & TV'})
    status = (await client.get('/api/status')).json()
    assert status['epg_url'] == 'https://tv.example/base/epg.xml?token=a%2Bb%26c%3F/'
    assert status['epg_days'] == 2
    response = await client.get('/epg.xml', params={'token': config.STREAM_TOKEN})
    root = ET.fromstring(response.content)
    assert [c.get('id') for c in root.findall('channel')] == ['main', second]
    assert root.findtext('channel/display-name') == 'Changed & TV'
    assert not root.findall('programme')
    playlist = await client.get('/playlist.m3u8', params={'token': config.STREAM_TOKEN})
    assert playlist.text.splitlines()[0] == f'#EXTM3U x-tvg-url="{status["epg_url"]}"'
    await client.delete(f'/api/channels/{second}')
    root = ET.fromstring((await client.get('/epg.xml', params={'token': config.STREAM_TOKEN})).content)
    assert len(root.findall('channel')) == 1


async def test_epg_settings_validation_auth_and_persistence(client, monkeypatch):
    path = '/api/epg/settings'
    assert (await client.get(path)).json() == {'days': 2}
    for value in [0, 8, 1.5, 2.0, '2', True, None]:
        assert (await client.patch(path, json={'days': value})).status_code == 422
    assert (await client.patch(path, json={})).status_code == 422
    assert (await client.patch(path, json={'days': 7})).json() == {'days': 7}
    db_path = app.state.db.rows('PRAGMA database_list')[0]['file']
    reopened = Database(Path(db_path))
    try:
        assert epg.settings(reopened) == {'days': 7}
    finally:
        reopened.conn.close()
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    monkeypatch.setattr(config, 'STREAM_TOKEN', 'token')
    assert (await client.get(path)).status_code == 401
    assert (await client.patch(path, json={'days': 1})).status_code == 401
    assert (await client.get('/epg.xml')).status_code == 403
    assert (await client.get('/epg.xml?token=wrong')).status_code == 403
    assert (await client.get('/epg.xml?token=token')).status_code == 200
    auth = ('admin', 'secret')
    assert (await client.patch(path, auth=auth, json={'days': 1}, headers={'Origin': 'https://evil.example'})).status_code == 403
    assert (await client.patch(path, auth=auth, json={'days': 1})).json() == {'days': 1}
    assert (await client.get(path, auth=auth)).json() == {'days': 1}


@pytest.mark.parametrize('finish', [False, True])
async def test_export_respects_removed_current_programme(client, monkeypatch, finish):
    await client.patch('/api/settings', json={'finish_current_on_remove': finish})
    channel, stopped = await playing_fixture(monkeypatch, with_other=True)
    title = channel.now['title']
    await client.delete('/api/sources/playing')
    rows = programmes(await client.get('/epg.xml'))
    assert (rows[0].findtext('title') == title) == finish
    assert stopped.is_set() != finish


def test_stream_keeps_snapshot_when_live_schedule_changes(tmp_path):
    timeline = Timeline(Database(tmp_path / 'db'), 'main')
    timeline.sync(items(60, 90, 120), now=1000)
    snapshot = timeline.snapshot()
    channels = [({'id': 'main', 'name': 'Original'}, snapshot)]
    expected = b''.join(epg.xmltv(channels, 1000, 9000))
    stream = epg.xmltv(channels, 1000, 9000)
    prefix = next(stream)
    timeline.advance(timeline.position(1000), now=1010)
    timeline.sync([], now=1011)
    timeline.db.conn.close()
    assert prefix + b''.join(stream) == expected


async def test_export_while_channel_deletion_waits_for_shutdown(client, monkeypatch):
    import asyncio
    second = (await client.post('/api/channels', json={'name': 'Deleting'})).json()['id']
    channel = app.state.channels[second]
    original_close = channel.close
    closing, release = asyncio.Event(), asyncio.Event()
    async def blocked_close():
        closing.set()
        await release.wait()
        await original_close()
    monkeypatch.setattr(channel, 'close', blocked_close)
    deletion = asyncio.create_task(client.delete(f'/api/channels/{second}'))
    try:
        await asyncio.wait_for(closing.wait(), 2)
        response = await client.get('/epg.xml')
        assert response.status_code == 200
        assert [c.get('id') for c in ET.fromstring(response.content).findall('channel')] == ['main']
    finally:
        release.set()
        await deletion
