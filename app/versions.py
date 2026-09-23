"""Immutable official yt-dlp zipapps; selection changes atomically after verification."""
import asyncio
import hashlib
import re
import sys
import time
from pathlib import Path
import httpx
import yt_dlp.version
from .config import DATA
from .process import capture

REPOS = {"stable": "yt-dlp/yt-dlp", "nightly": "yt-dlp/yt-dlp-nightly-builds"}


class Versions:
    def __init__(self, db):
        self.db = db
        self.directory = DATA / "versions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.job = {"state": "idle", "error": None}
        self.cache = {}
        self.task = None

    def current(self):
        selected = self.db.setting("yt_dlp")
        return selected or {"channel": "stable", "version": yt_dlp.version.__version__, "path": None}

    def command(self):
        path = self.current().get("path")
        return [sys.executable, path] if path else [sys.executable, "-m", "yt_dlp"]

    async def releases(self, channel):
        if channel not in REPOS:
            raise ValueError("Unknown release channel")
        cached = self.cache.get(channel)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            result = await client.get(f"https://api.github.com/repos/{REPOS[channel]}/releases?per_page=25")
            result.raise_for_status()
        rows = [{"version": r["tag_name"], "published": r["published_at"]}
                for r in result.json() if not r["draft"] and (channel == "nightly" or not r["prerelease"])]
        self.cache[channel] = (time.monotonic(), rows)
        return rows

    async def install(self, channel, version):
        async with self.lock:
            self.job = {"state": "installing", "error": None}
            try:
                if channel not in REPOS or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
                    raise ValueError("Invalid version")
                if version == "latest":
                    releases = await self.releases(channel)
                    if not releases:
                        raise ValueError("No releases available")
                    version = releases[0]["version"]
                base = f"https://github.com/{REPOS[channel]}/releases/download/{version}"
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    binary, checksums = await asyncio.gather(client.get(base + "/yt-dlp"), client.get(base + "/SHA2-256SUMS"))
                    binary.raise_for_status()
                    checksums.raise_for_status()
                expected = next(line.split()[0] for line in checksums.text.splitlines()
                                if line.split()[-1].lstrip("*") == "yt-dlp")
                if hashlib.sha256(binary.content).hexdigest() != expected:
                    raise ValueError("SHA-256 checksum mismatch")
                target = self.directory / f"{channel}-{version}"
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(binary.content)
                try:
                    actual = (await capture([sys.executable, str(temporary), "--version"], 15)).strip()
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                self.db.set_setting("yt_dlp", {"channel": channel, "version": actual, "path": str(target)})
                self.job = {"state": "done", "error": None}
            except Exception as exc:
                self.job = {"state": "error", "error": str(exc)}
