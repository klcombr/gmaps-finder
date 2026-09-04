"""SQLite deduplication — stores only identifiers, not full business data."""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone


class DedupDB:
    def __init__(self, path: str = "data/dedup.db"):
        self.path = path
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen (
                stable_id   TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                has_website TEXT DEFAULT 'unknown'
            );
            CREATE INDEX IF NOT EXISTS idx_fp ON seen(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_ls ON seen(last_seen);
        """)
        conn.commit()

    @staticmethod
    def _sid(place_id: str, fp: str) -> str:
        return place_id if place_id else f"fp:{fp}"

    def is_seen(self, place_id: str, fingerprint: str) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT 1 FROM seen WHERE stable_id=?",
            (self._sid(place_id, fingerprint),),
        )
        return cur.fetchone() is not None

    def record(self, place_id: str, fingerprint: str,
               has_website: str = "unknown") -> None:
        now = datetime.now(timezone.utc).isoformat()
        sid = self._sid(place_id, fingerprint)
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO seen (stable_id, fingerprint, first_seen, last_seen, has_website)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(stable_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                has_website=excluded.has_website
        """, (sid, fingerprint, now, now, has_website))
        conn.commit()

    def needs_recheck(self, place_id: str, fingerprint: str, days: int) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT last_seen FROM seen WHERE stable_id=?",
            (self._sid(place_id, fingerprint),),
        )
        row = cur.fetchone()
        if not row:
            return True
        try:
            last = datetime.fromisoformat(row[0])
        except (ValueError, TypeError):
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last > timedelta(days=days)

    def stats(self) -> dict:
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        no_site = conn.execute(
            "SELECT COUNT(*) FROM seen WHERE has_website='false'"
        ).fetchone()[0]
        return {"total_tracked": total, "without_website": no_site}

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
