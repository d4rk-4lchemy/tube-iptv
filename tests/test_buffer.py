import asyncio
import time
from app.buffer import MediaBuffer
from app.engine import Segment


async def test_reserve_is_bounded_and_not_published_ahead_of_clock():
    published = []
    clock = [1000.]
    async def publish(segment):
        published.append(segment.sequence)
    buffer = MediaBuffer(publish, clock=lambda: clock[0])
    try:
        for i in range(3):
            await buffer.put(Segment(i, 'a', 4, b'data', 0, 1000+i*4))
        blocked = asyncio.create_task(buffer.put(Segment(3, 'a', 4, b'data', 0, 1012)))
        await asyncio.sleep(.01)
        assert not published and not blocked.done()
        assert buffer.count == 3 and buffer.seconds == 12 and buffer.bytes == 12
        clock[0] = 1004
        await asyncio.wait_for(blocked, 1)
        assert published == [0]
        assert buffer.count == 3
        # An upstream stall needs no additional puts: stored media still airs.
        clock[0] = 1016
        await asyncio.wait_for(buffer.drain(), 1)
        assert published == [0, 1, 2, 3]
        assert buffer.count == buffer.bytes == buffer.seconds == 0
    finally:
        await buffer.close()


async def test_close_unblocks_upload_and_discards_future_media():
    published = []
    async def publish(segment):
        published.append(segment)
    buffer = MediaBuffer(publish, max_segments=1)
    future = Segment(0, 'a', 4, b'data', 0, time.time()+60)
    await buffer.put(future)
    blocked = asyncio.create_task(buffer.put(future))
    await asyncio.sleep(.01)
    await buffer.close()
    assert await asyncio.wait_for(blocked, 1) is False
    assert not published and buffer.bytes == buffer.count == 0


async def test_replacement_segment_does_not_move_program_time_backwards():
    published = []
    clock = [1000.]
    async def publish(segment):
        published.append((segment.clip, segment.program_time))
    buffer = MediaBuffer(publish, clock=lambda: clock[0])
    try:
        await buffer.put(Segment(0, 'old', 4, b'a', 0, 1000))
        await buffer.put(Segment(1, 'old', 4, b'b', 0, 1004))
        # The next encoder starts before the old reserve has reached the
        # viewer. Its requested programme time must not go backwards.
        await buffer.put(Segment(0, 'new', 4, b'c', 0, 1002))
        clock[0] = 1008
        await asyncio.sleep(.3)
        assert published == [('old', 1000), ('old', 1004)]
        clock[0] = 1012
        await asyncio.wait_for(buffer.drain(), 1)
        assert published == [('old', 1000), ('old', 1004), ('new', 1008)]
    finally:
        await buffer.close()
