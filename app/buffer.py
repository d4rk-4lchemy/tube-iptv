"""Bounded media read-ahead, independent of the public live playlist clock."""
import asyncio
import time


class MediaBuffer:
    def __init__(self, publish, max_segments=3, clock=time.time):
        self.publish = publish
        self.max_segments = max_segments
        self.clock = clock
        self.queue = asyncio.Queue()
        self.changed = asyncio.Condition()
        self.count = 0
        self.bytes = 0
        self.seconds = 0.0
        self.tail_deadline = None
        self.active = True
        self.task = asyncio.create_task(self.run())

    async def put(self, segment):
        async with self.changed:
            while self.active and self.count >= self.max_segments:
                await self.changed.wait()
            if not self.active:
                return False
            # A replacement encoder can finish its first segment before the
            # previous clip's read-ahead has aired. Keep that segment behind
            # the reserve in media time as well as FIFO order; otherwise its
            # program-date-time would move backwards at the discontinuity.
            if segment.program_time is not None:
                if self.tail_deadline is not None:
                    segment.program_time = max(segment.program_time, self.tail_deadline)
                self.tail_deadline = segment.program_time + segment.duration
            self.count += 1
            self.bytes += len(segment.data)
            self.seconds += segment.duration
            self.queue.put_nowait(segment)
            return True

    async def run(self):
        while True:
            segment = await self.queue.get()
            try:
                # Publish only once this segment's programme time has elapsed.
                # Future media stays private, so joining players cannot consume
                # the server's reserve simply by starting near the live edge.
                if segment.program_time is not None:
                    deadline = segment.program_time + segment.duration
                    while deadline > self.clock():
                        await asyncio.sleep(min(.25, deadline - self.clock()))
                await self.publish(segment)
            finally:
                async with self.changed:
                    self.count -= 1
                    self.bytes -= len(segment.data)
                    self.seconds -= segment.duration
                    self.queue.task_done()
                    self.changed.notify_all()

    async def drain(self):
        await self.queue.join()

    async def close(self):
        async with self.changed:
            self.active = False
            self.changed.notify_all()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.count = self.bytes = 0
        self.seconds = 0.0
