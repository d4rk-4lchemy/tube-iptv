"""Loopback-only HLS receiver that drains complete uploads even after peer EOF.

FFmpeg closes its final PUT without waiting for a response. StreamReader keeps
buffered bytes after EOF, unlike an ASGI request whose disconnect can supersede
unconsumed body events. No media is written to disk.
"""
import asyncio
import hmac
import h11

from .engine import MAX_SEGMENT


class UploadServer:
    def __init__(self, channel):
        self.channel = channel
        self.server = None
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        # FFmpeg 5's HLS muxer does not forward send_expect_100. A local-only
        # username enables its built-in 100-continue handshake (no password).
        self.channel.upload_base = f'http://upload@127.0.0.1:{port}/internal/{self.channel.secret}'

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        connection = h11.Connection(h11.SERVER)
        body = bytearray()
        clip = filename = None
        try:
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    data = await asyncio.wait_for(reader.read(65536), 30)
                    connection.receive_data(data)
                elif isinstance(event, h11.Request):
                    parts = event.target.decode('ascii').split('/')
                    if (event.method != b'PUT' or len(parts) != 5 or parts[1] != 'internal'
                            or not hmac.compare_digest(parts[2], self.channel.secret)):
                        return
                    clip, filename = parts[3:]
                    body = bytearray()
                    if (b'expect', b'100-continue') in event.headers:
                        if filename.endswith('.ts') and self.channel.media_buffer:
                            buffer = self.channel.media_buffer
                            async with buffer.changed:
                                while buffer.active and buffer.count >= buffer.max_segments:
                                    await buffer.changed.wait()
                        writer.write(connection.send(h11.InformationalResponse(status_code=100, headers=[])))
                        await writer.drain()
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    if len(body) > MAX_SEGMENT:
                        return
                elif isinstance(event, h11.EndOfMessage):
                    # Complete framing is required, including the last chunk.
                    # EOF after this point must not discard the accepted body.
                    accepted = await self.channel.ingest(clip, filename, bytes(body))
                    clip = filename = None
                    body.clear()
                    writer.write(connection.send(h11.Response(
                        status_code=200 if accepted else 410,
                        headers=[(b'Content-Length', b'0')])))
                    writer.write(connection.send(h11.EndOfMessage()))
                    await writer.drain()
                    if connection.our_state is h11.MUST_CLOSE:
                        return
                    connection.start_next_cycle()
                elif isinstance(event, h11.ConnectionClosed):
                    return
                else:
                    return
        except (h11.RemoteProtocolError, ConnectionError, asyncio.TimeoutError, ValueError):
            if clip == self.channel.clip and filename and filename.endswith('.ts'):
                self.channel.metrics['upload_aborts'] = self.channel.metrics.get('upload_aborts', 0) + 1
                self.channel.event(f'HLS upload interrupted: {filename}, {len(body)} bytes')
                await self.channel.upload_aborted(clip, filename)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.tasks.discard(task)
