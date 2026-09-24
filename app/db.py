import json
import sqlite3
import uuid
from .config import DATA
from .timeline import known_duration


class Database:
    def __init__(self, path=None):
        path = path or DATA / "tube.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO channels SELECT 'main', 'Tube / 01' WHERE NOT EXISTS (SELECT 1 FROM channels);
        CREATE TABLE IF NOT EXISTS sources(
            id TEXT PRIMARY KEY, channel_id TEXT REFERENCES channels(id), url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending', error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(channel_id,url));
        CREATE TABLE IF NOT EXISTS media(
            id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
            url TEXT NOT NULL, title TEXT NOT NULL, duration REAL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS programmes(
            id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
            name TEXT NOT NULL, duration_minutes INTEGER NOT NULL, rules TEXT NOT NULL);
        """)
        self._migrate_sources()
        if 'music' not in {r['name'] for r in self.rows('PRAGMA table_info(programmes)')}:
            self.conn.execute('ALTER TABLE programmes ADD COLUMN music INTEGER NOT NULL DEFAULT 0')
        self.conn.execute("UPDATE sources SET state='error', error='Interrupted by a restart. Refresh the source.' WHERE state='pending'")
        self.conn.commit()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def _migrate_sources(self):
        if 'programme_id' in {r['name'] for r in self.rows('PRAGMA table_info(sources)')}:
            return
        # Rebuild the old channel-wide UNIQUE constraint without losing media FKs.
        self.conn.commit()
        self.conn.execute('PRAGMA foreign_keys=OFF')
        try:
            self.conn.executescript("""
            BEGIN;
            CREATE TABLE sources_new(
                id TEXT PRIMARY KEY, channel_id TEXT REFERENCES channels(id), url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                programme_id TEXT REFERENCES programmes(id) ON DELETE CASCADE);
            INSERT INTO sources_new SELECT *, NULL FROM sources;
            DROP TABLE sources;
            ALTER TABLE sources_new RENAME TO sources;
            CREATE UNIQUE INDEX sources_channel_url ON sources(channel_id,url) WHERE programme_id IS NULL;
            CREATE UNIQUE INDEX sources_programme_url ON sources(programme_id,url) WHERE programme_id IS NOT NULL;
            COMMIT;
            """)
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.execute('PRAGMA foreign_keys=ON')

    def programmes(self, channel="main"):
        rows = self.rows('SELECT * FROM programmes WHERE channel_id=? ORDER BY rowid', (channel,))
        return [{**row, 'music': bool(row['music']), 'rules': json.loads(row['rules'])} for row in rows]

    def sources(self, channel="main", programme_id=None, all_sources=False):
        return self.rows("""SELECT s.*, COUNT(m.id) AS count FROM sources s LEFT JOIN media m
            ON m.source_id=s.id WHERE s.channel_id=? AND (? OR s.programme_id IS ?)
            GROUP BY s.id ORDER BY s.created_at,s.id""", (channel, all_sources, programme_id))

    def media(self, channel="main", programme_id=None, all_sources=False):
        return self.rows("""SELECT m.* FROM media m JOIN sources s ON m.source_id=s.id
            WHERE s.channel_id=? AND s.enabled=1 AND (? OR s.programme_id IS ?)""",
            (channel, all_sources, programme_id))

    def replace_media(self, source, title, items):
        with self.conn:
            if not self.rows("SELECT id FROM sources WHERE id=?", (source,)):
                return
            previous = {row["url"]: row["duration"] for row in self.rows(
                "SELECT url,duration FROM media WHERE source_id=?", (source,))}
            self.conn.execute("DELETE FROM media WHERE source_id=?", (source,))
            for item in items:
                duration = item.get("duration")
                if not known_duration(duration):
                    duration = previous.get(item["url"])
                self.conn.execute("INSERT INTO media VALUES(?,?,?,?,?)", (
                    uuid.uuid4().hex, source, item["url"], item["title"], duration))
            self.conn.execute("UPDATE sources SET title=?,state='ready',error=NULL WHERE id=?", (title, source))

    def setting(self, key, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def update_duration(self, channel, url, duration):
        self.execute('''UPDATE media SET duration=? WHERE url=? AND source_id IN
            (SELECT id FROM sources WHERE channel_id=?)''', (duration, url, channel))

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))
