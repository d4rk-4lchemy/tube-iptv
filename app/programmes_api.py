"""Programme administration and detached weekly calendar API."""
from datetime import date, datetime, time, timedelta
import asyncio
import json
import secrets
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from . import config
from .schedule import blocks, instants, end_instant, occurrences, validate_conflicts

router = APIRouter(prefix='/api/channels/{channel_id}')


def channel_for(request, channel_id):
    channel = request.app.state.channels.get(channel_id)
    if channel is None:
        raise HTTPException(404, 'Channel not found')
    return channel


def programme_for(request, channel_id, programme_id):
    channel_for(request, channel_id)
    return next((p for p in request.app.state.db.programmes(channel_id) if p['id'] == programme_id), None) or missing()


def missing():
    raise HTTPException(404, 'Programme not found')


class RuleInput(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=80)
    weekdays: list[int] = Field(min_length=1, max_length=7)
    time: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')

    @field_validator('weekdays', mode='before')
    @classmethod
    def days(cls, values):
        if not isinstance(values, list) or any(type(v) is not int or v < 0 or v > 6 for v in values):
            raise ValueError('Weekdays must be integers from 0 (Monday) to 6 (Sunday)')
        if len(set(values)) != len(values):
            raise ValueError('Weekdays must not repeat')
        return sorted(values)


class ProgrammeInput(BaseModel):
    music: bool = Field(default=False, strict=True)
    name: str = Field(min_length=1, max_length=120)
    duration_minutes: int = Field(strict=True, ge=1, le=1440)
    rules: list[RuleInput] = Field(min_length=1, max_length=32)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Enter a programme name without control characters')
        return value


def prepare(request, channel_id, payload, programme_id):
    channel = channel_for(request, channel_id)
    programme = {'id': programme_id, 'channel_id': channel_id, **payload.model_dump()}
    if 'music' not in payload.model_fields_set:
        existing = next((p for p in request.app.state.db.programmes(channel_id) if p['id'] == programme_id), None)
        programme['music'] = existing['music'] if existing else False
    for rule in programme['rules']:
        rule['id'] = rule['id'] or secrets.token_hex(12)
    if len({r['id'] for r in programme['rules']}) != len(programme['rules']):
        raise HTTPException(422, 'Rule IDs must not repeat')
    proposed = [p for p in request.app.state.db.programmes(channel_id) if p['id'] != programme_id] + [programme]
    try:
        validate_conflicts(proposed)
        held = channel.programme_timeline.held()
        if held:
            for row in occurrences(proposed, held['starts_at'], held['ends_at']):
                if row['programme_id'] != held['programme_id'] and row['starts_at'] < held['ends_at'] and row['ends_at'] > held['starts_at']:
                    raise ValueError(f"Conflict with current emission: {held['title']}. Changes apply after it finishes.")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return programme


def with_sources(request, programme):
    return {**programme, 'sources': request.app.state.db.sources(programme['channel_id'], programme['id'])}


@router.get('/programmes')
async def list_programmes(channel_id: str, request: Request):
    channel_for(request, channel_id)
    return [with_sources(request, p) for p in request.app.state.db.programmes(channel_id)]


@router.post('/programmes', status_code=201)
async def create_programme(channel_id: str, payload: ProgrammeInput, request: Request):
    """Create a weekly programme. Empty/pending source pools air black with silence."""
    programme = prepare(request, channel_id, payload, secrets.token_hex(12))
    request.app.state.db.execute('INSERT INTO programmes(id,channel_id,name,duration_minutes,rules,music) VALUES(?,?,?,?,?,?)',
        (programme['id'], channel_id, programme['name'], programme['duration_minutes'], json.dumps(programme['rules']), programme['music']))
    await channel_for(request, channel_id).programmes_changed()
    return with_sources(request, programme)


@router.get('/programmes/{programme_id}')
async def get_programme(channel_id: str, programme_id: str, request: Request):
    return with_sources(request, programme_for(request, channel_id, programme_id))


@router.put('/programmes/{programme_id}')
@router.patch('/programmes/{programme_id}')
async def update_programme(channel_id: str, programme_id: str, payload: ProgrammeInput, request: Request):
    """Replace name, duration and weekly rules atomically; preserve the current emission."""
    programme_for(request, channel_id, programme_id)
    programme = prepare(request, channel_id, payload, programme_id)
    request.app.state.db.execute('UPDATE programmes SET name=?,duration_minutes=?,rules=?,music=? WHERE id=?',
        (programme['name'], programme['duration_minutes'], json.dumps(programme['rules']), programme['music'], programme_id))
    await channel_for(request, channel_id).programmes_changed()
    return with_sources(request, programme)


@router.delete('/programmes/{programme_id}', status_code=204)
async def delete_programme(channel_id: str, programme_id: str, request: Request):
    programme_for(request, channel_id, programme_id)
    state = request.app.state
    tasks = [state.sources.tasks[s['id']] for s in state.db.sources(channel_id, programme_id) if s['id'] in state.sources.tasks]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    state.db.execute('DELETE FROM programmes WHERE id=?', (programme_id,))
    await channel_for(request, channel_id).programmes_changed(deleted=programme_id)
    return Response(status_code=204)


@router.get('/programmes/{programme_id}/sources')
async def list_sources(channel_id: str, programme_id: str, request: Request):
    programme_for(request, channel_id, programme_id)
    return request.app.state.db.sources(channel_id, programme_id)


@router.get('/schedule')
async def weekly_schedule(channel_id: str, request: Request, week: date | None = None):
    """Actual 23/24/25-hour calendar days; weekday rules repeat every week, not date exceptions."""
    channel = channel_for(request, channel_id)
    channel.scheduled()
    day = week or datetime.now(config.TIMEZONE).date()
    if not 2 <= day.year <= 9998:
        raise HTTPException(422, 'Calendar year must be between 2 and 9998')
    monday = day - timedelta(days=day.weekday())
    dates = [monday + timedelta(days=i) for i in range(8)]
    edges = []
    for value in dates:
        local = datetime.combine(value, time())
        edges.append((instants(local) or [end_instant(local, float('-inf'))])[0])
    programmes = request.app.state.db.programmes(channel_id)
    rows = blocks(programmes, edges[0], edges[-1], channel.programme_timeline.held()) if programmes else []
    days = []
    for index, value in enumerate(dates[:-1]):
        ticks = []
        cursor = edges[index]
        while cursor < edges[index + 1]:
            ticks.append({'timestamp': cursor, 'label': datetime.fromtimestamp(cursor, config.TIMEZONE).strftime('%H:%M %Z %z')})
            cursor += 1800
        days.append({'date': value.isoformat(), 'weekday': value.weekday(), 'starts_at': edges[index],
                     'ends_at': edges[index + 1], 'ticks': ticks})
    return {'week': monday.isoformat(), 'timezone': config.TZ, 'days': days,
            'occurrences': [{k: v for k, v in row.items() if k != 'programme'} for row in rows]}
