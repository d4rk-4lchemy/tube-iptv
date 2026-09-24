"""Civil weekly rules and UTC occurrences, independent of media and producers."""
from copy import deepcopy
from datetime import datetime, time, timedelta
from . import config
from .timeline import Slot

GAP_TITLE = 'No planned programme'
DAY = 86400
WEEK_MINUTES = 7 * 1440


def instants(local, zone=None):
    """Return both folds, or no instants for a nonexistent local minute."""
    zone = zone or config.TIMEZONE
    values = set()
    for fold in (0, 1):
        value = local.replace(tzinfo=zone, fold=fold).timestamp()
        if datetime.fromtimestamp(value, zone).replace(tzinfo=None) == local:
            values.add(value)
    return sorted(values)


def end_instant(local, start, zone=None):
    # A gap can be longer than an hour (including skipped calendar dates).
    for _ in range(2 * 1440 + 1):
        values = [value for value in instants(local, zone) if value > start]
        if values:
            return values[0]
        local += timedelta(minutes=1)
    raise ValueError('Could not resolve programme end in the configured timezone')


def validate_conflicts(programmes):
    intervals = []
    for programme in programmes:
        for rule in programme['rules']:
            hour, minute = map(int, rule['time'].split(':'))
            for weekday in rule['weekdays']:
                start = weekday * 1440 + hour * 60 + minute
                for shift in (-WEEK_MINUTES, 0, WEEK_MINUTES):
                    intervals.append((start + shift, start + shift + programme['duration_minutes'], programme['name']))
    intervals.sort()
    for left, right in zip(intervals, intervals[1:]):
        if left[1] > right[0] and 0 <= right[0] < WEEK_MINUTES:
            day, minute = divmod(right[0], 1440)
            raise ValueError(f'Conflict: {left[2]} overlaps {right[2]} on '
                             f'{["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][day]} '
                             f'at {minute // 60:02d}:{minute % 60:02d}')


def occurrences(programmes, starts_at, ends_at, held=None, zone=None):
    zone = zone or config.TIMEZONE
    first = datetime.fromtimestamp(starts_at - 2 * DAY, zone).date()
    last = datetime.fromtimestamp(ends_at + 2 * DAY, zone).date()
    result = []
    day = first
    while day <= last:
        for programme in programmes:
            for rule in programme['rules']:
                if day.weekday() not in rule['weekdays']:
                    continue
                hour, minute = map(int, rule['time'].split(':'))
                local = datetime.combine(day, time(hour, minute))
                starts = instants(local, zone)
                for start in starts:
                    end = end_instant(local + timedelta(minutes=programme['duration_minutes']), start, zone)
                    result.append({'id': f"{programme['id']}:{int(start)}", 'programme_id': programme['id'],
                                   'title': programme['name'], 'starts_at': start, 'ends_at': end,
                                   'rule_id': rule['id'], 'weekday': day.weekday(),
                                   'repeated': len(starts) > 1, 'hard_end': False,
                                   'programme': deepcopy(programme)})
        day += timedelta(days=1)
    if held and any(p['id'] == held['programme_id'] for p in programmes):
        # An edited active programme keeps its original civil occurrence.
        result = [row for row in result if row['id'] != held['id'] and not (
            row['programme_id'] == held['programme_id'] and
            row['starts_at'] < held['ends_at'] and row['ends_at'] > held['starts_at'])]
        result.append(deepcopy(held))
    result.sort(key=lambda row: (row['starts_at'], row['id']))
    for row, following in zip(result, result[1:]):
        if following.get('repeated') and datetime.fromtimestamp(following['starts_at'], zone).fold == 1:
            row['playback_deadline'] = following['starts_at']
        if row['ends_at'] > following['starts_at']:
            row['ends_at'] = following['starts_at']
            row['hard_end'] = True
    return [row for row in result if row['starts_at'] < ends_at and row['ends_at'] > starts_at]


def blocks(programmes, starts_at, ends_at, held=None, zone=None):
    rows = occurrences(programmes, starts_at - 8 * DAY, ends_at + 8 * DAY, held, zone)
    cursor = starts_at
    output = []
    for row in rows:
        if row['ends_at'] <= starts_at:
            cursor = max(cursor if output else row['ends_at'], row['ends_at'])
            continue
        if row['starts_at'] >= ends_at:
            if cursor < ends_at:
                output.append(gap(cursor, row['starts_at']))
            break
        if row['starts_at'] > cursor:
            output.append(gap(cursor, row['starts_at']))
        output.append(row)
        cursor = row['ends_at']
    else:
        if cursor < ends_at:
            output.append(gap(cursor, ends_at))
    return output


def gap(start, end):
    return {'id': f'gap:{int(start)}', 'programme_id': None, 'title': GAP_TITLE,
            'starts_at': start, 'ends_at': end, 'hard_end': True}


class ScheduleSnapshot:
    def __init__(self, programmes, held=None):
        self.programmes = deepcopy(programmes)
        self.held = deepcopy(held)

    def slots(self, starts_at, ends_at):
        if not self.programmes or ends_at <= starts_at:
            return
        for row in blocks(self.programmes, starts_at, ends_at, self.held):
            yield Slot({'title': row['title']}, row['starts_at'], row['ends_at'])
