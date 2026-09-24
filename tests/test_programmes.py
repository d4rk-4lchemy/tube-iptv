from datetime import datetime
from zoneinfo import ZoneInfo
import json
import sqlite3
import pytest
from app.db import Database
from app import config
from app.schedule import blocks, occurrences, validate_conflicts
from app.programme_playback import ProgrammeTimeline, content_slots, finish, BLACK_URL
from app.main import app
from test_api import client


def stamp(value, fold=0, zone='Europe/Warsaw'):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone), fold=fold).timestamp()


def programme(name='Show', at='18:00', minutes=60, days=None, id='p'):
    return {'id': id, 'channel_id': 'main', 'name': name, 'duration_minutes': minutes,
            'rules': [{'id': f'r-{id}', 'time': at, 'weekdays': list(range(7)) if days is None else days}]}


def insert(db, value):
    db.execute('INSERT INTO programmes VALUES(?,?,?,?,?)', (value['id'], value['channel_id'], value['name'], value['duration_minutes'], json.dumps(value['rules'])))


def source(db, id='s', owner='p', durations=(600, 700)):
    db.execute('INSERT INTO sources(id,channel_id,url,programme_id) VALUES(?,?,?,?)', (id, 'main', 'https://example.com/' + id, owner))
    db.replace_media(id, 'Pool', [{'url': f'https://example.com/{id}/{i}', 'title': f'Video {i}', 'duration': d} for i, d in enumerate(durations)])


def test_migrate_legacy_database_preserves_media_and_unique_scopes(tmp_path):
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    conn.executescript('''CREATE TABLE channels(id TEXT PRIMARY KEY,name TEXT NOT NULL);
        INSERT INTO channels VALUES('main','Existing');
        CREATE TABLE sources(id TEXT PRIMARY KEY,channel_id TEXT REFERENCES channels(id),url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'pending',error TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(channel_id,url));
        CREATE TABLE media(id TEXT PRIMARY KEY,source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,url TEXT NOT NULL,title TEXT NOT NULL,duration REAL);
        INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com');
        INSERT INTO media VALUES('m','s','https://example.com','Video',60);''')
    conn.close()
    db = Database(path)
    insert(db, programme())
    db.execute("INSERT INTO sources(id,channel_id,url,programme_id) VALUES('ps','main','https://example.com','p')")
    assert len(db.media()) == 1
    assert len(db.sources()) == 1
    assert len(db.sources('main', 'p')) == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO sources(id,channel_id,url,programme_id) VALUES('dup','main','https://example.com','p')")
    assert not db.rows('PRAGMA foreign_key_check')
    db.execute("DELETE FROM sources WHERE id='s'")
    assert db.media() == []
    db.conn.close()
    reopened = Database(path)
    assert len(reopened.sources('main', 'p')) == 1
    reopened.conn.close()


def test_weekly_conflicts_wrap_midnight_and_sunday():
    a = programme(at='23:30', days=[6])
    b = programme(at='00:00', days=[0], id='b')
    with pytest.raises(ValueError, match='Conflict'):
        validate_conflicts([a, b])
    b['rules'][0]['time'] = '00:30'
    validate_conflicts([a, b])
    with pytest.raises(ValueError):
        validate_conflicts([programme(), programme(id='b')])


def test_spring_missing_start_and_nonexistent_end():
    start, end = stamp('2026-03-29T00:00'), stamp('2026-03-30T00:00')
    assert occurrences([programme(at='02:30')], start, end) == []
    row = occurrences([programme(at='01:00', minutes=180)], start, end)[0]
    assert row['ends_at'] - row['starts_at'] == 7200
    row = occurrences([programme(at='01:00', minutes=90)], start, end)[0]
    assert row['ends_at'] == stamp('2026-03-29T03:00')


def test_autumn_two_emissions_and_hard_collision():
    start, end = stamp('2026-10-25T00:00'), stamp('2026-10-26T00:00')
    rows = occurrences([programme(at='02:30')], start, end)
    assert len(rows) == 2
    assert rows[0]['ends_at'] == rows[1]['starts_at'] == stamp('2026-10-25T02:30', fold=1)
    assert rows[0]['hard_end'] and rows[0]['repeated']
    assert rows[1]['ends_at'] == stamp('2026-10-25T03:30')
    row = occurrences([programme(at='01:00', minutes=180)], start, end)[0]
    assert row['ends_at'] - row['starts_at'] == 14400


def test_half_hour_dst_and_gap_coverage():
    zone = ZoneInfo('Australia/Lord_Howe')
    start, end = stamp('2026-04-05T00:00', zone=zone.key), stamp('2026-04-06T00:00', zone=zone.key)
    rows = occurrences([programme(at='01:45', minutes=15)], start, end, zone=zone)
    assert len(rows) == 2
    assert rows[1]['starts_at'] - rows[0]['starts_at'] == 1800
    rows = blocks([programme(at='10:00')], start, end, zone=zone)
    assert all(a['ends_at'] == b['starts_at'] for a, b in zip(rows, rows[1:]))
    assert rows[0]['starts_at'] <= start < rows[0]['ends_at']
    assert rows[-1]['starts_at'] < end <= rows[-1]['ends_at']


def plan(durations, end=3600, hard=False):
    return {'block': {'id': 'p:0', 'programme_id': 'p', 'title': 'Show', 'ends_at': end, 'hard_end': hard},
            'starts_at': 0, 'seed': 'stable', 'pool': [{'url': str(i), 'title': str(i), 'duration': d or 3600, 'estimated': d is None} for i, d in enumerate(durations)]}


def test_fitting_overrun_cut_and_unknown_duration():
    assert finish(plan([2000])) == 3600  # Second film cannot fit by +5: cut.
    assert finish(plan([1900])) == 3800
    assert finish(plan([1900], hard=True)) == 3600
    assert finish(plan([None], end=120)) == 120
    assert list(content_slots(plan([], end=120), 0))[0].item['url'] == BLACK_URL
    p = plan([10, 20, 30], end=1000000)
    all_tail = list(content_slots(p, 999980))
    assert all_tail[0].starts_at <= 999980 < all_tail[0].ends_at
    assert all(a.ends_at == b.starts_at for a, b in zip(all_tail, all_tail[1:]))


def test_persistent_playback_overrun_next_end_and_idle_restart(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme(at='18:00'))
    insert(db, programme(at='19:00', id='b'))
    source(db, durations=(1900,))
    source(db, id='b-source', owner='b', durations=(600,))
    now = stamp('2026-09-24T18:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    slot = timeline.sync(now=now)
    assert slot.item['programme_id'] == 'p'
    overrun = timeline.position(now + 3601)
    assert overrun.item['programme_id'] == 'p'
    following = timeline.position(now + 3800)
    assert following.item['programme_id'] == 'b'
    assert following.offset(now + 3800) == 0
    assert following.item['programme_ends_at'] == now + 7200
    expected = timeline.position(now + 4000)
    restored = ProgrammeTimeline(db, 'main', clock=lambda: now + 4000)
    assert restored.sync(now=now + 4000) == expected
    assert restored.sync(now=now + 20 * 86400) is not None
    assert len(restored.state['plans']) < 30


def test_current_emission_kept_but_sources_are_isolated(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db)
    source(db, id='general', owner=None)
    now = stamp('2026-09-24T18:10')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    first = timeline.sync(now=now)
    db.execute("UPDATE programmes SET name='New',duration_minutes=30 WHERE id='p'")
    current = timeline.sync(now=now + 1)
    assert current.item['programme_title'] == 'Show'
    assert current.item['programme_ends_at'] == stamp('2026-09-24T19:00')
    db.execute("DELETE FROM sources WHERE id='s'")
    current = timeline.sync(now=now + 2)
    assert current.item['url'] == BLACK_URL
    assert 'general' not in current.item['url']
    assert timeline.snapshot().programmes[0]['name'] == 'New'


async def test_programme_crud_sources_calendar_epg_and_validation(client, monkeypatch):
    def refresh(src):
        app.state.db.replace_media(src['id'], 'Media', [{'url': src['url'], 'title': 'A video', 'duration': 60}])
    monkeypatch.setattr(app.state.sources, 'refresh', refresh)
    payload = {k: v for k, v in programme().items() if k not in ('id', 'channel_id')}
    created = await client.post('/api/channels/main/programmes', json=payload)
    assert created.status_code == 201, created.text
    id = created.json()['id']
    path = f'/api/channels/main/programmes/{id}'
    src = {'url': 'https://example.com/video'}
    assert (await client.post(path + '/sources', json=src)).status_code == 202
    assert (await client.post('/api/sources', json=src)).status_code == 202
    assert (await client.post(path + '/sources', json=src)).status_code == 409
    assert len((await client.get(path)).json()['sources']) == 1
    assert len((await client.get('/api/status')).json()['sources']) == 1
    assert (await client.post('/api/channels/main/programmes', json=payload)).status_code == 409
    assert (await client.post('/api/channels/main/programmes', json={**payload, 'duration_minutes': True})).status_code == 422
    calendar = (await client.get('/api/channels/main/schedule?week=2026-10-25')).json()
    assert calendar['days'][-1]['ends_at'] - calendar['days'][-1]['starts_at'] == 90000
    xml = (await client.get('/epg.xml')).text
    assert '<title>Show</title>' in xml and '<title>No planned programme</title>' in xml
    assert '<title>A video</title>' not in xml
    assert (await client.get('/api/status')).json()['stream_available']
    assert (await client.patch('/api/settings', json={'gap_mode': 'sources'})).json()['gap_mode'] == 'sources'
    assert (await client.delete(path)).status_code == 204
    assert app.state.db.media('main')
    assert not app.state.db.programmes()
    assert (await client.get('/api/status')).json()['schedule_mode'] == 'rotation'


async def test_programme_auth_and_channel_isolation(client, monkeypatch):
    payload = {k: v for k, v in programme().items() if k not in ('id', 'channel_id')}
    other = (await client.post('/api/channels', json={'name': 'Other'})).json()['id']
    row = (await client.post('/api/channels/main/programmes', json=payload)).json()
    assert (await client.get(f'/api/channels/{other}/programmes/{row["id"]}')).status_code == 404
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'secret')
    assert (await client.get('/api/channels/main/programmes')).status_code == 401
    assert (await client.post('/api/channels/main/programmes', json=payload, auth=('admin', 'secret'), headers={'Origin': 'https://evil.example'})).status_code == 403


def test_new_programme_replaces_an_existing_gap(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme(at='18:00'))
    now = stamp('2026-09-24T12:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    assert timeline.sync(now=now).item['programme_id'] is None
    insert(db, programme(at='12:00', id='b'))
    assert timeline.sync(now=now).item['programme_id'] == 'b'


def test_failed_source_is_skipped_then_retried(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(600,))
    now = stamp('2026-09-24T18:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    first = timeline.sync(now=now)
    assert timeline.advance(first, now=now + 1).item['url'] == BLACK_URL
    assert timeline.sync(now=now + 2).item['url'] == BLACK_URL
    assert timeline.sync(now=now + 32).item['url'] == first.item['url']


def test_live_discovery_removes_overrun_and_survives_restart(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(1900,))
    now = stamp('2026-09-24T18:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    first = timeline.sync(now=now)
    assert timeline.position(now + 3601).item['programme_id'] == 'p'
    timeline.mark_live(first.item['url'], now=now)
    assert timeline.position(now + 3601).item['programme_id'] is None
    restored = ProgrammeTimeline(db, 'main', clock=lambda: now)
    assert restored.sync(now=now).item['estimated']
    assert restored.position(now + 3601).item['programme_id'] is None


def test_unknown_duration_learned_at_eof_repeats_without_black(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(None,))
    now = stamp('2026-09-24T18:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    first = timeline.sync(now=now)
    db.update_duration('main', first.item['url'], 9)
    following = timeline.sync(now=now + 9)
    assert following.item['url'] == first.item['url']
    assert not following.item['estimated']
    assert following.offset(now + 9) == 0
    assert timeline.position(now + 18).item['url'] == first.item['url']


@pytest.mark.parametrize('zone,day', [('Europe/Warsaw', '2026-10-25'), ('Europe/Warsaw', '2026-03-29'), ('Australia/Lord_Howe', '2026-04-05')])
def test_schedule_snapshot_complete_non_overlapping_and_detached(zone, day):
    # Explicit zone expansion checks, independent of the application's configured TZ.
    z = ZoneInfo(zone)
    start = stamp(day + 'T00:00', zone=zone)
    end = start + 2 * 86400
    programmes = [programme(at='00:00', minutes=90, id='early'), programme(at='01:30', minutes=60), programme(at='02:30', minutes=60, id='late')]
    rows = blocks(programmes, start, end, zone=z)
    assert rows[0]['starts_at'] <= start
    assert rows[-1]['ends_at'] >= end
    assert all(a['ends_at'] == b['starts_at'] for a, b in zip(rows, rows[1:]))
    assert all(r['ends_at'] > r['starts_at'] for r in rows)


def test_source_removal_retains_only_current_video_within_boundary(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(1900,))
    now = stamp('2026-09-24T18:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    slot = timeline.sync(now=now)
    db.execute("DELETE FROM sources WHERE id='s'")
    current = timeline.sync(now=now + 1, preserve_removed=True)
    assert current.key == slot.key and current.ends_at == slot.ends_at
    assert timeline.position(slot.ends_at).item['url'] == BLACK_URL
    # Turning off the preference must revoke the retained lead even if the pool has not changed.
    revoked = timeline.sync(now=now + 2, preserve_removed=False)
    assert revoked.item['url'] == BLACK_URL


def test_autumn_playback_never_overruns_into_second_start(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme(at='02:30'))
    source(db, durations=(1900,))
    now = stamp('2026-10-25T02:30')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    timeline.sync(now=now)
    second = stamp('2026-10-25T02:30', fold=1)
    assert timeline.position(second - 1).ends_at <= second
    assert timeline.position(second).item['programme_starts_at'] == second


async def test_programme_deletion_cancels_pending_extraction_and_cascades(client, monkeypatch):
    import asyncio
    began, ended = asyncio.Event(), asyncio.Event()
    async def blocked(_):
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()
    monkeypatch.setattr(app.state.sources, '_refresh', blocked)
    payload = {k: v for k, v in programme().items() if k not in ('id', 'channel_id')}
    row = (await client.post('/api/channels/main/programmes', json=payload)).json()
    path = '/api/channels/main/programmes/' + row['id']
    src = (await client.post(path + '/sources', json={'url': 'https://example.com/video'})).json()
    await asyncio.wait_for(began.wait(), 1)
    app.state.db.replace_media(src['id'], 'Video', [{'url': 'https://example.com/video', 'title': 'Video', 'duration': 60}])
    assert (await client.delete(path)).status_code == 204
    assert ended.is_set()
    assert not app.state.db.media(all_sources=True)
    assert not app.state.db.rows('PRAGMA foreign_key_check')


async def test_prefetched_black_gap_is_not_revoked_before_programme_ends(tmp_path, monkeypatch):
    from app.engine import Channel
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(600,))
    now = stamp('2026-09-24T18:59:55')
    monkeypatch.setattr('app.engine.time.time', lambda: now)
    channel = Channel('main', db, None)
    channel.scheduled(now=now)
    upcoming = channel.timeline.position(stamp('2026-09-24T19:00'))
    assert upcoming.item['url'] == BLACK_URL
    channel.now = upcoming.item
    channel.producing_slot = upcoming
    async def forbidden():
        raise AssertionError('The next gap is legitimate read-ahead, not a removed source')
    monkeypatch.setattr(channel, 'stop', forbidden)
    await channel.reconcile_sources()


def test_eof_learning_keeps_already_buffered_tail_on_public_clock(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(None,))
    start = stamp('2026-09-24T18:00')
    clock = [start]
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: clock[0])
    current = timeline.sync(now=start)
    clock[0] = start + 1  # Producer has read to EOF while most data is still in RAM.
    db.update_duration('main', current.item['url'], 9)
    timeline.sync(now=start + 9)
    buffered = timeline.position(start + 5)
    assert buffered.starts_at == start
    assert buffered.ends_at == start + 9
    following = timeline.position(start + 9)
    assert following.starts_at == start + 9
    assert following.item['url'] == current.item['url']


def test_autumn_next_start_interrupts_overrun_even_across_a_short_gap(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme(at='02:00', minutes=30))
    insert(db, programme(at='02:30', minutes=29, id='b'))
    source(db, durations=(1800,))
    source(db, id='b-source', owner='b', durations=(1950,))
    now = stamp('2026-10-25T02:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    timeline.sync(now=now)
    second = stamp('2026-10-25T02:00', fold=1)
    previous = timeline.position(second - 1)
    assert previous.ends_at <= second
    assert timeline.position(second).item['programme_starts_at'] == second


def test_pool_refresh_cannot_extend_a_film_that_still_does_not_fit(tmp_path):
    db = Database(tmp_path / 'db')
    insert(db, programme())
    source(db, durations=(7200,))
    now = stamp('2026-09-24T18:00')
    timeline = ProgrammeTimeline(db, 'main', clock=lambda: now)
    initial = timeline.sync(now=now)
    assert initial.ends_at == now + 3600
    source(db, id='new', durations=(600,))
    updated = timeline.sync(now=now + 1)
    assert updated.key == initial.key
    assert updated.ends_at == initial.ends_at
