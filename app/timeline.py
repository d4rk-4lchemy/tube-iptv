"""A persistent, wall-clock schedule. Computing a position never opens media URLs."""
from dataclasses import dataclass
from copy import copy, deepcopy
import math
import random
import secrets
import time

UNKNOWN_DURATION = 3600.0


def known_duration(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class Slot:
    item: dict
    starts_at: float
    ends_at: float

    def offset(self, now):
        return max(0.0, now - self.starts_at)

    @property
    def key(self):
        return self.item['url'], self.starts_at


class Timeline:
    def __init__(self, db, channel_id, clock=time.time):
        self.db, self.clock = db, clock
        self.key = f'timeline:{channel_id}'
        self.state = db.setting(self.key) or None

    def snapshot(self):
        """Detach a schedule for read-only use outside the application thread."""
        snapshot = copy(self)
        snapshot.state = deepcopy(self.state)
        snapshot.db = None
        return snapshot

    def slots(self, starts_at, ends_at):
        """Yield complete slots intersecting [starts_at, ends_at), without writes.

        Walk each shuffled cycle once, rather than re-shuffling for every slot.
        Call on a snapshot when the iterator outlives the current event-loop turn.
        """
        if not self.state or ends_at <= starts_at:
            return
        lead = self.state['lead']
        if lead and lead['starts_at'] < ends_at and lead['ends_at'] > starts_at:
            yield Slot(**lead)
        total = sum(item['duration'] for item in self.state['pool'])
        if not total:
            return
        epoch = self.state['epoch']
        cycle = int(max(0.0, starts_at - epoch) // total)
        while (start := epoch + cycle * total) < ends_at:
            for item in self._order(cycle):
                end = start + item['duration']
                if start >= ends_at:
                    return
                if end > starts_at:
                    yield Slot(item, start, end)
                start = end
            cycle += 1

    def sync(self, items, now=None, preserve_removed=False):
        now = self.clock() if now is None else now
        pool = {}
        for item in items:
            duration = item.get('duration')
            pool[item['url']] = {'url': item['url'], 'title': item['title'],
                'duration': float(duration) if known_duration(duration) else UNKNOWN_DURATION,
                'estimated': not known_duration(duration)}
        pool = sorted(pool.values(), key=lambda item: item['url'])
        current = self.position(now)
        keep_current = current and (preserve_removed or any(item['url'] == current.item['url'] for item in pool))
        if self.state and pool == self.state['pool'] and (not current or keep_current):
            return current
        if not keep_current:
            current = None
        lead = None
        if current:
            updated = next((item for item in pool if item['url'] == current.item['url']), current.item)
            lead = {'item': updated, 'starts_at': current.starts_at,
                    'ends_at': current.starts_at + updated['duration']}
        self.state = {'pool': pool, 'seed': secrets.token_hex(16),
                      'epoch': lead['ends_at'] if lead else now, 'lead': lead}
        self.db.set_setting(self.key, self.state)
        return self.position(now)

    def advance(self, current, now=None):
        """Make the programme following ``current`` the active wall-clock slot.

        Producers call this when media reaches EOF or becomes unavailable before
        its scheduled boundary.  The schedule is rebased at that point so a
        replacement can be encoded while the previous programme's RAM reserve
        is still being aired.
        """
        now = self.clock() if now is None else now
        if not self.state or now >= current.ends_at:
            return self.position(now)
        active = self.position(now)
        if not active or active.key != current.key:
            return active
        following = self.position(current.ends_at + .001)
        if not following:
            return active
        # Translate the existing rotation instead of restarting cycle zero.
        # Restarting it repeatedly alternates its first two videos forever.
        shift = now - current.ends_at
        self.state['epoch'] += shift
        if self.state['lead']:
            self.state['lead']['starts_at'] += shift
            self.state['lead']['ends_at'] += shift
        self.db.set_setting(self.key, self.state)
        return self.position(now)

    def _raw_order(self, cycle):
        items = list(self.state['pool'])
        # Two entries must alternate; for larger pools shuffle every cycle.
        random.Random(f"{self.state['seed']}:{cycle if len(items) > 2 else 0}").shuffle(items)
        if len(items) == 2 and self.state["lead"] and items[0]["url"] == self.state["lead"]["item"]["url"]:
            items.reverse()
        return items

    def _order(self, cycle):
        items = self._raw_order(cycle)
        if len(items) > 2 and cycle > 0:
            # Swapping only the first two preserves the previous cycle's last item.
            previous_last = self._raw_order(cycle - 1)[-1]['url']
            if items[0]['url'] == previous_last:
                items[0], items[1] = items[1], items[0]
        if cycle == 0 and len(items) > 2 and self.state['lead']:
            previous = self.state['lead']['item']['url']
            if items[0]['url'] == previous:
                # First two only, for the same stable-boundary property.
                items[0], items[1] = items[1], items[0]
        return items

    def position(self, now=None):
        now = self.clock() if now is None else now
        if not self.state:
            return None
        lead = self.state['lead']
        if lead and now < lead['ends_at']:
            return Slot(**lead)
        total = sum(item['duration'] for item in self.state['pool'])
        if not total:
            return None
        elapsed = max(0.0, now - self.state['epoch'])
        cycle = int(elapsed // total)
        start = self.state['epoch'] + cycle * total
        for item in self._order(cycle):
            end = start + item['duration']
            if now < end:
                return Slot(item, start, end)
            start = end
        # Floating-point rounding at the boundary: use the next cycle.
        item = self._order(cycle + 1)[0]
        return Slot(item, start, start + item['duration'])
