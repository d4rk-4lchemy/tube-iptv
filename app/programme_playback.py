"""Persistent media plans inside civil occurrences; never resolves network URLs."""
from copy import deepcopy
import json
import random
import secrets
import time
from . import config
from .schedule import DAY, ScheduleSnapshot, blocks
from .timeline import Slot, UNKNOWN_DURATION, known_duration

BLACK_URL = 'slate:black'


def catalogue(items):
    return sorted({item['url']: {'url': item['url'], 'title': item['title'],
        'duration': max(.04, float(item['duration'])) if known_duration(item.get('duration')) else UNKNOWN_DURATION,
        'estimated': not known_duration(item.get('duration'))} for item in items}.values(), key=lambda item: item['url'])


def content_slots(plan, starts_at):
    """Skip entire shuffle cycles arithmetically; only walk the relevant tail."""
    block = plan['block']
    end = block['ends_at']
    origin = plan.get('origin', plan['starts_at'])
    lead = plan.get('lead')
    if lead and lead['ends_at'] > starts_at:
        yield Slot(**lead)
    if origin >= end:
        return
    pool = plan['pool']
    if not pool:
        if end > starts_at:
            yield Slot({'url': BLACK_URL, 'title': block['title'], 'duration': end - origin,
                        'estimated': False, 'kind': 'black'}, origin, end)
        return
    total = sum(item['duration'] for item in pool)
    cycle = max(0, min(int((end - origin) // total), int(max(0, starts_at - origin) // total)))
    cursor = origin + cycle * total
    allowance = 0 if block['hard_end'] or not block['programme_id'] else 300
    allowance = min(allowance, max(0, block.get('playback_deadline', end + allowance) - end))

    def order(number):
        values = list(pool)
        random.Random(f"{plan['seed']}:{number if len(pool) > 2 else 0}").shuffle(values)
        return values

    while cursor < end - .000001:
        remaining = order(cycle)
        if cycle and len(pool) > 2 and remaining[0]['url'] == order(cycle - 1)[-1]['url']:
            remaining[0], remaining[1] = remaining[1], remaining[0]
        while remaining and cursor < end - .000001:
            fitting = next((i for i, item in enumerate(remaining)
                            if not item['estimated'] and item['duration'] <= end + allowance - cursor), None)
            item = dict(remaining.pop(fitting if fitting is not None else 0))
            stop = min(cursor + item['duration'], end + allowance) if fitting is not None else min(cursor + item['duration'], end)
            item['duration'] = stop - cursor
            item['kind'] = 'video'
            if stop > starts_at:
                yield Slot(item, cursor, stop)
            cursor = stop
        cycle += 1


def finish(plan):
    tail = list(content_slots(plan, max(plan['starts_at'], plan['block']['ends_at'] - .001)))
    if tail:
        return tail[-1].ends_at
    return max(plan['starts_at'], (plan.get('lead') or {}).get('ends_at', plan['block']['ends_at']))


class ProgrammeTimeline:
    def __init__(self, db, channel_id, clock=time.time):
        self.db, self.channel_id, self.clock = db, channel_id, clock
        self.key = f'programme_timeline:{channel_id}'
        self.state = db.setting(self.key) or {'seed': secrets.token_hex(16), 'plans': [], 'signature': None}
        if self.state.get('timezone', config.TZ) != config.TZ:
            self.state['plans'] = []
            self.state['signature'] = None
        self.state['timezone'] = config.TZ
        self.programmes = []

    def save(self):
        self.db.set_setting(self.key, self.state)

    def held(self, now=None):
        now = self.clock() if now is None else now
        return next((deepcopy(p['block']) for p in self.state['plans']
                     if p['block']['programme_id'] and p['starts_at'] <= now < p['ends_at']), None)

    def snapshot(self):
        return ScheduleSnapshot(self.programmes, self.held())

    def _pool(self, block, excluded=None, now=None):
        if block['programme_id'] or self.db.setting(f'gap_mode:{self.channel_id}', 'black') == 'sources':
            now = self.clock() if now is None else now
            pool = [item for item in catalogue(self.db.media(self.channel_id, block['programme_id']))
                    if (excluded or {}).get(item['url'], 0) <= now]
            for item in pool:
                if item['url'] in self.state.get('live_urls', []):
                    item.update(duration=UNKNOWN_DURATION, estimated=True)
            return pool
        return []

    def sync(self, items=None, now=None, preserve_removed=False):
        now = self.clock() if now is None else now
        self.programmes = self.db.programmes(self.channel_id)
        programme_signature = json.dumps([self.programmes, config.TZ], sort_keys=True)
        signature = json.dumps([self.programmes, self.db.media(self.channel_id, all_sources=True),
                                self.db.setting(f'gap_mode:{self.channel_id}', 'black'), config.TZ], sort_keys=True)
        if not self.programmes:
            if self.state['plans']:
                self.state['plans'] = []
                self.state['signature'] = signature
                self.save()
            return None
        retry_due = any(0 < until <= now for p in self.state['plans'] if p['starts_at'] <= now < p['ends_at'] for until in p.get('excluded', {}).values())
        current = self.position(now)
        revoked = (current and current.item['url'] != BLACK_URL and not preserve_removed
                   and current.item['url'] not in {m['url'] for m in self.db.media(self.channel_id, current.item.get('programme_id'))})
        changed = signature != self.state['signature'] or retry_due or revoked
        if not changed and self.state['plans'] and self.state['plans'][-1]['block']['ends_at'] > now + 3600:
            return self.position(now)
        active = next((p for p in self.state['plans'] if p['starts_at'] <= now < p['ends_at']), None)
        ids = {p['id'] for p in self.programmes}
        if active and active['block']['programme_id'] and active['block']['programme_id'] not in ids:
            active = None
        if active and not active['block']['programme_id'] and programme_signature != self.state.get('programme_signature'):
            active = None
        held = active['block'] if active and active['block']['programme_id'] else None
        previous = [p for p in self.state['plans'] if p['ends_at'] <= now]
        if changed:
            # Keep the current occurrence but refresh its remaining source pool.
            if active:
                old_slot = self._position(active, now)
                active['excluded'] = {url: until for url, until in active.get('excluded', {}).items() if until > now}
                pool = self._pool(active['block'], active['excluded'], now)
                if pool != active['pool'] or revoked:
                    keep = old_slot and old_slot.item['url'] != BLACK_URL and (
                        preserve_removed or any(i['url'] == old_slot.item['url'] for i in pool))
                    active = deepcopy(active)
                    active['pool'] = pool
                    if keep:
                        updated = next((i for i in pool if i['url'] == old_slot.item['url']), old_slot.item)
                        limit = active['block']['ends_at'] + (0 if active['block']['hard_end'] or updated['estimated'] else 300)
                        limit = min(limit, active['block'].get('playback_deadline', limit))
                        natural_end = old_slot.starts_at + updated['duration']
                        stop = natural_end if natural_end <= limit else min(natural_end, active['block']['ends_at'])
                        keep = stop > min(now, self.clock())
                        old_slot = Slot({**updated, 'duration': stop - old_slot.starts_at, 'kind': 'video'}, old_slot.starts_at, stop)
                    active['origin'] = old_slot.ends_at if keep else now
                    active['lead'] = {'item': old_slot.item, 'starts_at': old_slot.starts_at,
                                      'ends_at': old_slot.ends_at} if keep else None
                    active['ends_at'] = finish(active)
            plans = previous + ([active] if active else [])
        else:
            plans = list(self.state['plans'])
        # A saved checkpoint carries overrun forward even after a long idle period.
        boundary = plans[-1]['block']['ends_at'] if plans else now - 2 * DAY
        cursor = plans[-1]['ends_at'] if plans else boundary
        horizon = now + 2 * DAY
        while boundary < horizon:
            rows = blocks(self.programmes, boundary, min(horizon, boundary + 2 * DAY), held)
            progressed = False
            for block in rows:
                if block['ends_at'] <= boundary:
                    continue
                if plans and block['id'] == plans[-1]['block']['id']:
                    boundary = block['ends_at']
                    continue
                start = max(cursor, block['starts_at'])
                plan = {'block': block, 'starts_at': start, 'pool': self._pool(block, now=now),
                        'seed': f"{self.state['seed']}:{block['id']}"}
                plan['ends_at'] = finish(plan)
                if start < block['ends_at']:
                    if plan['ends_at'] > now - 2 * DAY:
                        plans.append(plan)
                    cursor = plan['ends_at']
                boundary = block['ends_at']
                progressed = True
            if not progressed:
                break
        self.state['plans'] = [p for p in plans if p['ends_at'] > now - 2 * DAY]
        self.state['signature'] = signature
        self.state['programme_signature'] = programme_signature
        self.save()
        return self.position(now)

    def _position(self, plan, now):
        return next(content_slots(plan, now), None)

    def position(self, now=None):
        now = self.clock() if now is None else now
        for plan in self.state['plans']:
            if plan['starts_at'] <= now < plan['ends_at']:
                slot = self._position(plan, now)
                if slot:
                    return Slot({**slot.item, 'programme_id': plan['block']['programme_id'],
                                 'music': bool(plan['block'].get('programme', {}).get('music', False)),
                                 'programme_title': plan['block']['title'], 'occurrence_id': plan['block']['id'],
                                 'programme_starts_at': plan['block']['starts_at'],
                                 'programme_ends_at': plan['block']['ends_at'],
                                 'hard_end': plan['block']['hard_end']}, slot.starts_at, slot.ends_at)
        return None

    def mark_live(self, url, now=None, is_live=True):
        live = self.state.setdefault('live_urls', [])
        if is_live and url not in live:
            live.append(url)
            self.state['signature'] = None
        elif not is_live and url in live:
            live.remove(url)
            self.state['signature'] = None
        return self.sync(now=now)

    def advance(self, current, now=None):
        now = self.clock() if now is None else now
        for index, plan in enumerate(self.state['plans']):
            if plan['starts_at'] <= now < plan['ends_at'] and current.item.get('occurrence_id') == plan['block']['id']:
                if now >= current.ends_at:
                    return self.position(now)
                # Refill only this occurrence. Do not translate the civil schedule.
                pool = [item for item in plan['pool'] if item['url'] != current.item['url']]
                plan['pool'] = pool
                plan.setdefault('excluded', {})[current.item['url']] = now + 30
                plan['origin'] = now
                plan['lead'] = ({'item': {**current.item, 'duration': now - current.starts_at},
                                 'starts_at': current.starts_at, 'ends_at': now}
                                if now > current.starts_at else None)
                plan['ends_at'] = finish(plan)
                self.state['plans'] = self.state['plans'][:index + 1]
                self.state['signature'] = None
                self.save()
                return self.sync(now=now)
        return self.position(now)
