"""Streaming XMLTV export of detached wall-clock schedules."""
from datetime import datetime, timezone
import math
import re
from xml.etree.ElementTree import Element, SubElement, tostring

DEFAULT_DAYS = 2
_INVALID_XML = re.compile('[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


def settings(db):
    return {'days': db.setting('epg_days', DEFAULT_DAYS)}


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y%m%d%H%M%S +0000')


def xmltv(channels, starts_at, ends_at):
    """Yield bounded batches; only detached metadata is accessed by this worker."""
    yield b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="Tube IPTV">\n'
    for channel, _ in channels:
        element = Element('channel', id=channel['id'])
        SubElement(element, 'display-name').text = _INVALID_XML.sub('', channel['name'])
        yield tostring(element, encoding='utf-8') + b'\n'
    for channel, timeline in channels:
        batch = bytearray()
        for index, slot in enumerate(timeline.slots(starts_at, ends_at)):
            start, stop = math.floor(slot.starts_at), math.floor(slot.ends_at)
            if stop > start:
                element = Element('programme', start=timestamp(start), stop=timestamp(stop), channel=channel['id'])
                SubElement(element, 'title').text = _INVALID_XML.sub('', slot.item['title'])
                batch.extend(tostring(element, encoding='utf-8') + b'\n')
            # Also yield for sub-second slots so cancellation can be observed.
            if len(batch) >= 65536 or index % 256 == 255:
                yield bytes(batch)
                batch.clear()
        if batch:
            yield bytes(batch)
    yield b'</tv>\n'
