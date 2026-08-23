"""SQLite persistence: metric history, snapshots, source health, news dedup, alerts."""
import json
import os
import sqlite3
import threading

from .util import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
  ts REAL NOT NULL, key TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_key_ts ON metrics(key, ts);
CREATE TABLE IF NOT EXISTS snapshots (
  ts REAL PRIMARY KEY, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, last_ok REAL, last_status TEXT, items INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts (
  ts REAL NOT NULL, level TEXT NOT NULL, metric TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_seen (
  link_hash TEXT PRIMARY KEY, first_seen REAL
);
CREATE TABLE IF NOT EXISTS news (
  link TEXT PRIMARY KEY, first_seen REAL, source TEXT, title TEXT,
  published REAL, summary TEXT
);
"""


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # -- metrics -------------------------------------------------------
    def record_metrics(self, values: dict[str, float]):
        now = utcnow()
        with self._lock:
            self._db.executemany(
                "INSERT INTO metrics(ts, key, value) VALUES (?,?,?)",
                [(now, k, float(v)) for k, v in values.items() if isinstance(v, (int, float))],
            )
            self._db.commit()

    def history(self, key: str, limit: int = 500) -> list[tuple[float, float]]:
        # rowid tiebreaker: time.time() can return identical values for rapid
        # inserts (coarse Windows clock) -> keep stable insertion order.
        cur = self._db.execute(
            "SELECT ts, value FROM metrics WHERE key=? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (key, int(limit)),
        )
        rows = cur.fetchall()
        return list(reversed(rows))

    def window_values(self, key: str, since: float) -> list[float]:
        cur = self._db.execute(
            "SELECT value FROM metrics WHERE key=? AND ts>=? ORDER BY ts ASC, rowid ASC",
            (key, since),
        )
        return [r[0] for r in cur.fetchall()]

    # -- snapshots -----------------------------------------------------
    def save_snapshot(self, snapshot: dict):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO snapshots(ts, json) VALUES (?,?)",
                (snapshot.get("ts", utcnow()), json.dumps(snapshot)),
            )
            # retention: keep last 7 days of full snapshots
            self._db.execute("DELETE FROM snapshots WHERE ts < ?", (utcnow() - 7 * 86400,))
            self._db.commit()

    def latest_snapshot(self) -> dict | None:
        cur = self._db.execute(
            "SELECT json FROM snapshots ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    # -- sources -------------------------------------------------------
    def source_ok(self, name: str, items: int = 0):
        with self._lock:
            self._db.execute(
                "INSERT INTO sources(name,last_ok,last_status,items) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_ok=excluded.last_ok, "
                "last_status=excluded.last_status, items=excluded.items",
                (name, utcnow(), "ok", items),
            )
            self._db.commit()

    def source_fail(self, name: str, error: str):
        with self._lock:
            self._db.execute(
                "INSERT INTO sources(name,last_ok,last_status,items) VALUES (?,?,?,0) "
                "ON CONFLICT(name) DO UPDATE SET last_status=excluded.last_status",
                (name, utcnow(), f"error: {error}"[:300]),
            )
            self._db.commit()

    def all_sources(self) -> list[dict]:
        cur = self._db.execute("SELECT name, last_ok, last_status, items FROM sources")
        return [
            {"name": n, "last_ok": t, "status": s, "items": i}
            for n, t, s, i in cur.fetchall()
        ]

    # -- alerts --------------------------------------------------------
    def save_alerts(self, alerts: list[dict]):
        if not alerts:
            return
        now = utcnow()
        with self._lock:
            self._db.executemany(
                "INSERT INTO alerts(ts, level, metric, message) VALUES (?,?,?,?)",
                [(a.get("ts", now), a["level"], a["metric"], a["message"]) for a in alerts],
            )
            self._db.execute("DELETE FROM alerts WHERE ts < ?", (now - 3 * 86400,))
            self._db.commit()

    def recent_alerts(self, limit: int = 50) -> list[dict]:
        cur = self._db.execute(
            "SELECT ts, level, metric, message FROM alerts ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        )
        return [{"ts": t, "level": l, "metric": m, "message": msg} for t, l, m, msg in cur.fetchall()]

    # -- news ----------------------------------------------------------
    def filter_new_links(self, items: list[dict]) -> list[dict]:
        import hashlib
        out = []
        now = utcnow()
        with self._lock:
            for it in items:
                h = hashlib.sha256(it["link"].encode()).hexdigest()
                try:
                    self._db.execute("INSERT INTO news_seen(link_hash, first_seen) VALUES (?,?)", (h, now))
                    out.append(it)
                except sqlite3.IntegrityError:
                    pass
            self._db.commit()
        return out

    def save_news(self, items: list[dict]):
        now = utcnow()
        with self._lock:
            for it in items:
                self._db.execute(
                    "INSERT OR REPLACE INTO news(link, first_seen, source, title, published, summary) "
                    "VALUES (?,?,?,?,?,?)",
                    (it["link"], now, it.get("source", ""), it.get("title", ""),
                     it.get("published", 0), it.get("summary", "")[:500]),
                )
            # retention: drop news older than 14 days by published date
            self._db.execute("DELETE FROM news WHERE published < ? AND published > 0",
                             (now - 14 * 86400,))
            self._db.commit()

    def recent_news(self, limit: int = 30) -> list[dict]:
        cur = self._db.execute(
            "SELECT first_seen, source, title, link, summary FROM news "
            "ORDER BY COALESCE(NULLIF(published,0), first_seen) DESC LIMIT ?",
            (int(limit),),
        )
        return [
            {"ts": fs, "source": s, "title": t, "link": l, "summary": sm}
            for fs, s, t, l, sm in cur.fetchall()
        ]

    def close(self):
        self._db.close()
