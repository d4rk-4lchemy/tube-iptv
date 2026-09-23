import asyncio
import base64
from contextlib import asynccontextmanager
import hmac
import secrets
import sqlite3
import time
from urllib.parse import quote, urlparse
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect
from pydantic import BaseModel, Field
from . import config
from .db import Database
from .engine import Channel, MAX_SEGMENT
from .sources import Sources, validate_url
from .versions import Versions


@asynccontextmanager
async def lifespan(app):
    app.state.db = db = Database()
    app.state.versions = versions = Versions(db)
    app.state.sources = sources = Sources(db, versions)
    app.state.channel = Channel("main", db, sources)
    from .ingest import UploadServer
    uploads = UploadServer(app.state.channel)
    await uploads.start()
    app.state.channel.clock_task = asyncio.create_task(app.state.channel.clock_loop())
    yield
    await app.state.channel.close()
    await uploads.close()
    await sources.close()
    if versions.task and not versions.task.done():
        versions.task.cancel()
        await asyncio.gather(versions.task, return_exceptions=True)
    db.conn.close()


app = FastAPI(title="Tube IPTV", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def protect(request, call_next):
    if request.url.path.startswith("/api/") or request.url.path == "/":
        if config.ADMIN_PASSWORD:
            try:
                kind, value = request.headers.get("authorization", "").split(" ", 1)
                user, password = base64.b64decode(value).decode().split(":", 1)
                valid = kind.lower() == "basic" and user == "admin" and hmac.compare_digest(password, config.ADMIN_PASSWORD)
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Tube IPTV"'})
        if request.method in ("POST", "PATCH", "DELETE", "PUT"):
            origin = request.headers.get("origin")
            allowed = {request.url.netloc, urlparse(config.PUBLIC_URL).netloc}
            if origin and urlparse(origin).netloc not in allowed:
                return JSONResponse({"detail": "Request origin is not allowed"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def status(request: Request):
    state = request.app.state
    channel = state.db.rows("SELECT * FROM channels WHERE id='main'")[0]
    return {"channel": channel, **state.channel.status(), "sources": state.db.sources(),
            "media_count": len({m["url"] for m in state.db.media()}),
            "yt_dlp": {k: v for k, v in state.versions.current().items() if k != "path"},
            "update": state.versions.job,
            "playlist_url": base_url(request) + "/playlist.m3u8" + token_suffix(),
            "stream_url": base_url(request) + "/channels/main/index.m3u8" + token_suffix()}


class SourceInput(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


@app.post("/api/sources", status_code=202)
async def add_source(payload: SourceInput, request: Request):
    try:
        url = validate_url(payload.url.strip())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    source = {"id": secrets.token_hex(12), "url": url}
    try:
        request.app.state.db.execute("INSERT INTO sources(id,channel_id,url) VALUES(?,'main',?)", (source["id"], url))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This URL has already been added")
    request.app.state.sources.refresh(source)
    return source


def find_source(request, source_id):
    rows = request.app.state.db.rows("SELECT * FROM sources WHERE id=?", (source_id,))
    if not rows:
        raise HTTPException(404, "Source not found")
    return rows[0]


@app.delete("/api/sources/{source_id}", status_code=204)
async def delete_source(source_id: str, request: Request):
    find_source(request, source_id)
    task = request.app.state.sources.tasks.get(source_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    request.app.state.db.execute("DELETE FROM sources WHERE id=?", (source_id,))
    await request.app.state.channel.reconcile_sources()
    return Response(status_code=204)


class SourcePatch(BaseModel):
    enabled: bool


@app.patch("/api/sources/{source_id}")
async def toggle_source(source_id: str, payload: SourcePatch, request: Request):
    find_source(request, source_id)
    request.app.state.db.execute("UPDATE sources SET enabled=? WHERE id=?", (payload.enabled, source_id))
    await request.app.state.channel.reconcile_sources()
    return {"ok": True}


@app.post("/api/sources/{source_id}/refresh", status_code=202)
async def refresh_source(source_id: str, request: Request):
    request.app.state.sources.refresh(find_source(request, source_id))
    return {"ok": True}


class ChannelPatch(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r'^[^\r\n"<>]+$')


@app.patch("/api/channel")
async def rename_channel(payload: ChannelPatch, request: Request):
    request.app.state.db.execute("UPDATE channels SET name=? WHERE id='main'", (payload.name,))
    return {"ok": True}


class PlaybackSettings(BaseModel):
    finish_current_on_remove: bool


@app.patch('/api/settings')
async def update_settings(payload: PlaybackSettings, request: Request):
    request.app.state.db.set_setting('finish_current_on_remove', payload.finish_current_on_remove)
    await request.app.state.channel.reconcile_sources()
    return {'finish_current_on_remove': payload.finish_current_on_remove}


@app.get("/api/versions/{channel}")
async def versions(channel: str, request: Request):
    try:
        return await request.app.state.versions.releases(channel)
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch GitHub releases: {exc}")


class VersionInput(BaseModel):
    channel: str = Field(pattern="^(stable|nightly)$")
    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")


@app.post("/api/versions", status_code=202)
async def install_version(payload: VersionInput, request: Request):
    versions = request.app.state.versions
    if versions.task and not versions.task.done():
        raise HTTPException(409, "An update is already running")
    versions.job = {"state": "installing", "error": None}
    versions.task = asyncio.create_task(versions.install(payload.channel, payload.version))
    return {"ok": True}


def base_url(request):
    return config.PUBLIC_URL or str(request.base_url).rstrip("/")


def token_suffix():
    return "?token=" + quote(config.STREAM_TOKEN) if config.STREAM_TOKEN else ""


def check_stream(request):
    if config.STREAM_TOKEN and not hmac.compare_digest(request.query_params.get("token", ""), config.STREAM_TOKEN):
        raise HTTPException(403, "Invalid stream token")


@app.get("/playlist.m3u8")
async def playlist(request: Request):
    check_stream(request)
    name = request.app.state.db.rows("SELECT name FROM channels WHERE id='main'")[0]["name"]
    body = f'#EXTM3U\n#EXTINF:-1 tvg-id="main" group-title="Tube IPTV",{name}\n{base_url(request)}/channels/main/index.m3u8{token_suffix()}\n'
    return Response(body, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


@app.get("/channels/main/index.m3u8")
async def stream(request: Request):
    check_stream(request)
    channel = request.app.state.channel
    if not channel.available():
        raise HTTPException(503, "No enabled videos available", headers={"Retry-After": "5"})
    viewer = request.query_params.get("viewer")
    if not viewer:
        # Stable session URL lets players behind the same NAT count independently.
        viewer = secrets.token_urlsafe(12)
        return Response(status_code=307, headers={"Location": f"index.m3u8?viewer={viewer}" +
                        ("&token=" + quote(config.STREAM_TOKEN) if config.STREAM_TOKEN else ""), "Cache-Control": "no-store"})
    if len(viewer) > 80:
        raise HTTPException(400)
    channel.waiters += 1
    try:
        await channel.touch(viewer)
        deadline = asyncio.get_running_loop().time() + 90
        while len(channel.segments) < 2 and channel.state != "ended":
            if not channel.available():
                raise HTTPException(503, "No enabled videos available", headers={"Retry-After": "5"})
            if await request.is_disconnected():
                channel.viewers.pop(viewer, None)
                return Response(status_code=499)
            if asyncio.get_running_loop().time() > deadline:
                raise HTTPException(503, channel.error or "The channel is buffering", headers={"Retry-After": "5"})
            async with channel.changed:
                try:
                    await asyncio.wait_for(channel.changed.wait(), 1)
                except asyncio.TimeoutError:
                    pass
        await channel.touch(viewer)
        return Response(channel.manifest(viewer, config.STREAM_TOKEN), media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})
    finally:
        channel.waiters -= 1


@app.get("/channels/main/segments/{sequence}.ts")
async def segment(sequence: int, request: Request):
    check_stream(request)
    channel = request.app.state.channel
    found = next((s for s in channel.segments if s.sequence == sequence), None)
    if not found:
        raise HTTPException(404, "This segment has expired. Reload the playlist.")
    viewer = request.query_params.get("viewer", "segment-client")[:80]
    await channel.touch(viewer)
    return Response(found.data, media_type="video/mp2t", headers={"Cache-Control": "no-store"})


@app.put("/internal/{secret}/{clip}/{filename}")
async def ingest(secret: str, clip: str, filename: str, request: Request):
    channel = request.app.state.channel
    if not hmac.compare_digest(secret, channel.secret) or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403)
    body = bytearray()
    started = time.monotonic()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_SEGMENT:
                raise HTTPException(413)
    except ClientDisconnect:
        if clip == channel.clip:
            channel.metrics['upload_aborts'] = channel.metrics.get('upload_aborts', 0) + 1
            channel.event(f'HLS upload interrupted: {filename}, {len(body)} bytes, {time.monotonic() - started:.2f}s')
            await channel.upload_aborted(clip, filename)
        return Response(status_code=499)
    try:
        accepted = await channel.ingest(clip, filename, bytes(body))
    except (ValueError, UnicodeError):
        raise HTTPException(400)
    if not accepted:
        raise HTTPException(410)
    return Response(status_code=200)
