import asyncio
import json
import re
from urllib.parse import urlparse
from .config import MAX_ITEMS
from .process import capture
from .video import RESOLUTIONS


# Prefer seekable direct HTTP media over YouTube's HLS variants. Some HLS
# variants return zero frames after an input seek in the packaged FFmpeg.
FORMAT_SELECTOR = ("bv[height<=1080][protocol=https]+ba/b[height<=1080][protocol=https]/"
                   "bv[height<=1080]+ba/b[height<=1080]/bv+ba/b/ba")


def validate_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter an HTTP or HTTPS URL without embedded credentials")
    return value


class Sources:
    def __init__(self, db, versions):
        self.db, self.versions = db, versions
        self.tasks = {}
        self.limit = asyncio.Semaphore(2)

    def base(self):
        command = self.versions.command() + ["--ignore-config", "--no-cache-dir", "--no-progress",
            "--socket-timeout", "20", "--retries", "2"]
        version = tuple(int(n) for n in re.findall(r"\d+", self.versions.current()["version"])[:3])
        if version >= (2025, 11, 12):
            command += ["--js-runtimes", "deno", "--js-runtimes", "node"]
        return command

    def refresh(self, source):
        if source["id"] in self.tasks:
            return
        self.db.execute("UPDATE sources SET state='pending',error=NULL WHERE id=?", (source["id"],))
        task = asyncio.create_task(self._refresh(source))
        self.tasks[source["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(source["id"], None))

    async def _refresh(self, source):
        try:
            async with self.limit:
                raw = await capture(self.base() + ["--flat-playlist", "--playlist-end", str(MAX_ITEMS),
                    "--skip-download", "--dump-single-json", "--", source["url"]], 180)
            info = json.loads(raw)
            entries = info.get("entries") if "entries" in info else [info]
            items, seen = [], set()
            for entry in entries or []:
                if not entry or entry.get("availability") in ("private", "premium_only", "subscriber_only"):
                    continue
                url = entry.get("webpage_url") or entry.get("url")
                if not url or not url.startswith(("https://", "http://")):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                items.append({"url": url, "title": entry.get("title") or url, "duration": entry.get("duration")})
            if not items:
                raise ValueError("The source contains no available videos")
            self.db.replace_media(source["id"], info.get("title") or source["url"], items)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.db.execute("UPDATE sources SET state='error',error=? WHERE id=?", (str(exc)[-1800:], source["id"]))

    async def resolve(self, url, resolution="1080p"):
        height = RESOLUTIONS[resolution][1]
        raw = await capture(self.base() + ["--no-playlist", "--skip-download", "-f",
            FORMAT_SELECTOR.replace("1080", str(height)), "--dump-single-json", "--", url], 90)
        info = json.loads(raw)
        formats = info.get("requested_formats") or [info]
        for fmt in formats:
            validate_url(fmt["url"])
            if fmt.get("has_drm"):
                raise ValueError("DRM content is not supported")
        return info, formats

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
