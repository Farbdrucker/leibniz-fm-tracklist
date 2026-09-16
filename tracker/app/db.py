"""SQLite schema, connection handling, and query helpers.

Single file owned exclusively by the tracker service. WAL mode is enabled
because there are three concurrent access points to this file: the Icecast
poller thread (writer), the provider-sync thread (reader+writer), and the
FastAPI request handlers (readers).

Callers open one connection per logical operation via `connect()` and pass
it into the helper functions below, so a multi-step operation (e.g. syncing
one calendar day) commits atomically as a single transaction.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_db_path: Path | None = None
_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    day        TEXT NOT NULL,
    artist     TEXT NOT NULL DEFAULT '',
    song       TEXT NOT NULL DEFAULT '',
    raw        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_day ON tracks(day);

CREATE TABLE IF NOT EXISTS provider_playlists (
    provider    TEXT NOT NULL,
    day         TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (provider, day)
);

CREATE TABLE IF NOT EXISTS provider_tracks (
    provider   TEXT NOT NULL,
    day        TEXT NOT NULL,
    track_key  TEXT NOT NULL,
    uri        TEXT,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, day, track_key)
);
CREATE INDEX IF NOT EXISTS idx_provider_tracks_day_uri ON provider_tracks(provider, day, uri);

CREATE TABLE IF NOT EXISTS provider_search_cache (
    provider   TEXT NOT NULL,
    track_key  TEXT NOT NULL,
    uri        TEXT,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, track_key)
);

CREATE TABLE IF NOT EXISTS sync_state (
    provider              TEXT PRIMARY KEY,
    last_synced_track_id  INTEGER NOT NULL DEFAULT 0
);
"""


def configure(db_path: str | Path) -> None:
    """Set the DB file path. Must be called once before connect()."""
    global _db_path
    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)


def init_schema() -> None:
    with _init_lock, connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    if _db_path is None:
        raise RuntimeError("db.configure() must be called before db.connect()")
    conn = sqlite3.connect(_db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -- tracks -----------------------------------------------------------------

def insert_track(conn, ts: str, day: str, artist: str, song: str, raw: str) -> int:
    cur = conn.execute(
        "INSERT INTO tracks (ts, day, artist, song, raw) VALUES (?, ?, ?, ?, ?)",
        (ts, day, artist, song, raw),
    )
    return cur.lastrowid


def last_raw(conn) -> str:
    row = conn.execute("SELECT raw FROM tracks ORDER BY id DESC LIMIT 1").fetchone()
    return row["raw"] if row else ""


def track_count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM tracks").fetchone()["n"]


def tracks_since(conn, since_id: int = 0, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT id, ts, artist, song, raw FROM tracks WHERE id > ? ORDER BY id"
    params: list = [since_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def recent_tracks(conn, limit: int) -> list[sqlite3.Row]:
    """Most recent tracks, newest first - used by the local --tui dev mode."""
    return conn.execute(
        "SELECT id, ts, artist, song, raw FROM tracks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def tracks_for_sync(conn, since_id: int) -> list[sqlite3.Row]:
    """Rows eligible for provider sync: has an artist (skips station IDs/jingles)."""
    return conn.execute(
        "SELECT id, day, artist, song FROM tracks "
        "WHERE id > ? AND artist != '' ORDER BY id",
        (since_id,),
    ).fetchall()


# -- sync_state ---------------------------------------------------------------

def get_sync_cursor(conn, provider: str) -> int:
    row = conn.execute(
        "SELECT last_synced_track_id FROM sync_state WHERE provider = ?", (provider,)
    ).fetchone()
    return row["last_synced_track_id"] if row else 0


def set_sync_cursor(conn, provider: str, last_synced_track_id: int) -> None:
    conn.execute(
        "INSERT INTO sync_state (provider, last_synced_track_id) VALUES (?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET last_synced_track_id = excluded.last_synced_track_id",
        (provider, last_synced_track_id),
    )


# -- provider_playlists ---------------------------------------------------------

def get_provider_playlist(conn, provider: str, day: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT playlist_id, name FROM provider_playlists WHERE provider = ? AND day = ?",
        (provider, day),
    ).fetchone()


def set_provider_playlist(conn, provider: str, day: str, playlist_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO provider_playlists (provider, day, playlist_id, name) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider, day) DO UPDATE SET playlist_id = excluded.playlist_id, "
        "name = excluded.name",
        (provider, day, playlist_id, name),
    )


# -- provider_tracks (per-day dedup) --------------------------------------------

def get_provider_track(conn, provider: str, day: str, track_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT uri, status FROM provider_tracks WHERE provider = ? AND day = ? AND track_key = ?",
        (provider, day, track_key),
    ).fetchone()


def upsert_provider_track(conn, provider: str, day: str, track_key: str,
                           uri: str | None, status: str) -> None:
    conn.execute(
        "INSERT INTO provider_tracks (provider, day, track_key, uri, status) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(provider, day, track_key) DO UPDATE SET "
        "uri = excluded.uri, status = excluded.status, updated_at = datetime('now')",
        (provider, day, track_key, uri, status),
    )


def day_has_uri(conn, provider: str, day: str, uri: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM provider_tracks WHERE provider = ? AND day = ? AND uri = ? LIMIT 1",
        (provider, day, uri),
    ).fetchone()
    return row is not None


# -- provider_search_cache (global, cross-day) ----------------------------------

def get_search_cache(conn, provider: str, track_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT uri, status FROM provider_search_cache WHERE provider = ? AND track_key = ?",
        (provider, track_key),
    ).fetchone()


def upsert_search_cache(conn, provider: str, track_key: str, uri: str | None, status: str) -> None:
    conn.execute(
        "INSERT INTO provider_search_cache (provider, track_key, uri, status) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider, track_key) DO UPDATE SET "
        "uri = excluded.uri, status = excluded.status, updated_at = datetime('now')",
        (provider, track_key, uri, status),
    )
