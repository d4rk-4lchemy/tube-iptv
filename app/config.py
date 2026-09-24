import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TZ = os.getenv('TZ', 'Europe/Warsaw')
try:
    TIMEZONE = ZoneInfo(TZ)
except ZoneInfoNotFoundError as exc:
    raise ValueError(f'Invalid TZ: {TZ}. Use an IANA timezone such as Europe/Warsaw.') from exc

DATA = Path(os.getenv("DATA_DIR", "/data"))
PORT = int(os.getenv("PORT", "8000"))
IDLE_SECONDS = float(os.getenv("IDLE_SECONDS", "25"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
STREAM_TOKEN = os.getenv("STREAM_TOKEN", "")
ENCODER = os.getenv("ENCODER", "software")
VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "/dev/dri/renderD128")
MAX_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "500"))
