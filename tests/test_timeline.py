import asyncio
from app.db import Database
from app.engine import Channel, ffmpeg_command
from app.timeline import Timeline


def items(*durations):
    return [{'url': f'https://example.com/{i}', 'title': str(i), 'duration': d} for i, d in enumerate(durations)]


def test_join_two_minutes_later_and_restart_keep_position(tmp_path):
    db = Database(tmp_path / 'db')
    now = [1000.0]
    timeline = Timeline(db, 'main', clock=lambda: now[0])
    first = timeline.sync(items(600))
    now[0] += 120
    current = timeline.sync(items(600))
    assert current.key == first.key
    assert current.offset(now[0]) == 120
    restored = Timeline(db, 'main', clock=lambda: now[0])
    assert restored.position() == current
    assert restored.position().offset(now[0]) == 120


def test_crosses_programmes_and_many_cycles_without_any_producer(tmp_path):
    db = Database(tmp_path / 'db')
    timeline = Timeline(db, 'main', clock=lambda: 1000)
    first = timeline.sync(items(60, 90, 120))
    second = timeline.position(first.ends_at + 17)
    assert second.item['url'] != first.item['url']
    assert second.offset(first.ends_at + 17) == 17
    later = 1000 + 270 * 1_000_000 + 13
    slot = timeline.position(later)
    assert slot.starts_at <= later < slot.ends_at
    assert slot.offset(later) == 13


def test_early_source_end_rebases_schedule_to_the_following_programme(tmp_path):
    db = Database(tmp_path / 'db')
    timeline = Timeline(db, 'main')
    first = timeline.sync(items(60, 90), now=1000)
    expected_next = timeline.position(first.ends_at + .001)

    next_slot = timeline.advance(first, now=1025)

    assert next_slot.item['url'] == expected_next.item['url']
    assert next_slot.starts_at == 1025
    assert next_slot.ends_at == 1025 + expected_next.item['duration']
    assert timeline.position(1025) == next_slot
    assert Timeline(db, 'main', clock=lambda: 1025).position() == next_slot


def test_repeated_early_ends_preserve_the_entire_rotation(tmp_path):
    timeline = Timeline(Database(tmp_path / 'rotation'), 'main')
    current = timeline.sync(items(60, 60, 60, 60, 60), now=1000)
    expected = [timeline.position(1000 + i * 60).item['url'] for i in range(15)]
    actual = [current.item['url']]
    for i in range(1, 15):
        current = timeline.advance(current, now=1000 + i * 10)
        actual.append(current.item['url'])
    assert actual == expected


def test_refresh_and_source_changes_preserve_current_start(tmp_path):
    db = Database(tmp_path / 'db')
    timeline = Timeline(db, 'main')
    pool = items(600, 600)
    first = timeline.sync(pool, now=1000)
    # Source refresh changes generated media IDs, which are not schedule identities.
    refreshed = [{**item, 'id': 'new-id'} for item in pool]
    assert timeline.sync(refreshed, now=1120).key == first.key
    added = timeline.sync(pool + items(600, 600, 600)[2:], now=1130)
    assert added.key == first.key and added.offset(1130) == 130
    removed = timeline.sync([i for i in pool if i['url'] != first.item['url']], now=1140, preserve_removed=True)
    assert removed.key == first.key
    assert timeline.position(first.ends_at).item['url'] != first.item['url']


def test_no_immediate_repeat_at_cycle_boundaries(tmp_path):
    for count in (2, 3, 5):
        db = Database(tmp_path / f'db{count}')
        timeline = Timeline(db, 'main')
        pool = items(*([10] * count))
        timeline.sync(pool, now=0)
        previous = None
        for t in range(0, 10 * count * 30, 10):
            current = timeline.position(t).item['url']
            assert current != previous
            previous = current
        # Also check boundaries immediately after a source edit.
        timeline.sync(pool + [{'url': 'https://example.com/new', 'title': 'new', 'duration': 10}], now=5)
        previous = timeline.position(5).item['url']
        for t in range(10, 300, 10):
            current = timeline.position(t).item['url']
            assert current != previous
            previous = current


def test_unknown_duration_is_explicit_and_can_be_learned(tmp_path):
    db = Database(tmp_path / 'db')
    timeline = Timeline(db, 'main')
    initial = timeline.sync(items(None), now=1000)
    assert initial.item['estimated']
    corrected = timeline.sync(items(600), now=1120)
    assert corrected.starts_at == 1000 and corrected.ends_at == 1600
    assert corrected.offset(1120) == 120 and not corrected.item['estimated']


def test_seek_applies_to_both_remote_inputs_but_not_to_live():
    formats = [{'url': 'https://example.com/v', 'vcodec': 'h264', 'acodec': 'none'},
               {'url': 'https://example.com/a', 'vcodec': 'none', 'acodec': 'aac'}]
    command = ffmpeg_command(formats, {}, 'http://localhost/output', offset=120.5, duration=300)
    seeks = [i for i, value in enumerate(command) if value == '-ss']
    assert len(seeks) == 2
    for index in seeks:
        assert command[index + 1:index + 3] == ['120.500000', '-i']
    assert command[command.index('-t') + 1] == '300.000000'
    assert command[command.index('-http_persistent') + 1] == '1'
    assert '-ss' not in ffmpeg_command(formats, {'is_live': True}, 'http://localhost/output', offset=120)


async def test_idle_clock_never_resolves_or_launches_media(tmp_path):
    db = Database(tmp_path / 'db')
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com')")
    db.replace_media('s', 'Test', items(600))
    class NoMedia:
        async def resolve(self, url):
            raise AssertionError('Idle channels must not resolve media')
    channel = Channel('main', db, NoMedia())
    channel.timeline.clock = lambda: 1000
    channel.scheduled()
    channel.timeline.clock = lambda: 1120
    await channel.stop()
    assert channel.scheduled().offset(1120) == 120
    assert channel.task is None and channel.process is None
    assert channel.bytes == 0 and not channel.segments


def test_removing_idle_source_does_not_leave_a_ghost_programme(tmp_path):
    timeline = Timeline(Database(tmp_path / 'db'), 'main')
    timeline.sync(items(None), now=1000)
    assert timeline.sync([], now=1100) is None
    added = timeline.sync([{'url': 'https://example.com/new', 'title': 'New', 'duration': 600}], now=1120)
    assert added.item['title'] == 'New' and added.offset(1120) == 0


def test_removed_active_programme_disappears_when_producer_stops(tmp_path):
    timeline = Timeline(Database(tmp_path / 'db'), 'main')
    first = timeline.sync(items(600), now=1000)
    assert timeline.sync([], now=1100, preserve_removed=True).key == first.key
    assert timeline.sync([], now=1101, preserve_removed=False) is None


def test_refresh_keeps_previously_learned_durations(tmp_path):
    db = Database(tmp_path / 'db')
    db.execute("INSERT INTO sources(id,channel_id,url) VALUES('s','main','https://example.com')")
    db.replace_media('s', 'Test', items(600))
    db.replace_media('s', 'Refreshed', items(None))
    assert db.media()[0]['duration'] == 600
