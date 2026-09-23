import asyncio
from urllib.parse import urlsplit

from app.engine import Channel
from app.ingest import UploadServer


async def test_complete_large_upload_survives_peer_close_and_backpressure():
    channel = Channel('test', None, None)
    channel.clip = 'clip'
    gate = asyncio.Event()
    received = []
    async def sink(segment):
        await gate.wait()
        received.append(segment.data)
    channel.segment_sink = sink
    server = UploadServer(channel)
    await server.start()
    port = urlsplit(channel.upload_base).port
    data = b'G' * (3 * 1024 * 1024)
    async def send(name, body):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        path = urlsplit(channel.upload_base).path + '/clip/' + name
        writer.write(f'PUT {path} HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n'.encode())
        writer.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    try:
        await send('index.m3u8', b'#EXTM3U\n#EXTINF:4,\n000000.ts\n#EXT-X-ENDLIST\n')
        await send('000000.ts', data)
        await asyncio.sleep(.05)
        assert not received and not channel.ingest_finished.is_set()
        gate.set()
        await asyncio.wait_for(channel.ingest_finished.wait(), 2)
        assert received == [data]
        assert channel.metrics.get('upload_aborts', 0) == 0
    finally:
        gate.set()
        await server.close()


async def test_truncated_upload_is_not_accepted_as_a_complete_segment():
    channel = Channel('test', None, None)
    channel.clip = 'clip'
    server = UploadServer(channel)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', urlsplit(channel.upload_base).port)
        path = urlsplit(channel.upload_base).path + '/clip/000000.ts'
        writer.write(f'PUT {path} HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n1000\r\npartial'.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if channel.metrics.get('upload_aborts'):
                break
            await asyncio.sleep(.01)
        assert channel.metrics['upload_aborts'] == 1
        assert not channel.pending and not channel.segments
    finally:
        await server.close()


async def test_keepalive_acknowledges_multiple_uploads_and_rejects_wrong_secret():
    channel = Channel('test', None, None)
    channel.clip = 'clip'
    server = UploadServer(channel)
    await server.start()
    port = urlsplit(channel.upload_base).port
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        for index in range(2):
            path = urlsplit(channel.upload_base).path + f'/clip/{index:06d}.ts'
            writer.write(f'PUT {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 3\r\n\r\nabc'.encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 1)
            assert response.startswith(b'HTTP/1.1 200 ')
        writer.close()
        await writer.wait_closed()
        assert len(channel.pending) == 2
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(b'PUT /internal/wrong/clip/000002.ts HTTP/1.1\r\nHost: localhost\r\nContent-Length: 3\r\n\r\nabc')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b''
        writer.close()
        await writer.wait_closed()
        assert len(channel.pending) == 2
    finally:
        await server.close()


async def test_continue_handshake_waits_for_reserve_capacity():
    from types import SimpleNamespace
    channel = Channel('test', None, None)
    channel.clip = 'clip'
    buffer = SimpleNamespace(active=True, count=3, max_segments=3, changed=asyncio.Condition())
    channel.media_buffer = buffer
    server = UploadServer(channel)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', urlsplit(channel.upload_base).port)
        path = urlsplit(channel.upload_base).path + '/clip/000000.ts'
        writer.write(f'PUT {path} HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 3\r\n\r\n'.encode())
        await writer.drain()
        response = asyncio.create_task(reader.readuntil(b'\r\n\r\n'))
        await asyncio.sleep(.05)
        assert not response.done(), 'Producer allowed to upload into a full reserve'
        async with buffer.changed:
            buffer.count = 2
            buffer.changed.notify_all()
        assert (await asyncio.wait_for(response, 1)).startswith(b'HTTP/1.1 100 ')
        writer.write(b'abc')
        await writer.drain()
        assert (await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 1)).startswith(b'HTTP/1.1 200 ')
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
