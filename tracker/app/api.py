"""Tracker's internal read API - deliberately minimal: a health check, a bulk
track dump, and two small embed payloads.

`/tracks` stays an unfiltered dump: filtering/search/calendar aggregation live
client-side in the web UI, so that proven logic isn't reimplemented in both
Python and JS.

`/now` is the one carve-out, for the widgets embedded on third-party pages
(leibniz.fm). It reimplements none of that client-side logic - no search, no
station detection, no aggregation - it only answers "the newest few rows" and
"one indexed day", which the bulk dump cannot do cheaply: a foreign page must
not re-download the whole archive (~300 rows/day, growing) every 30s for every
visitor. It also decides what *today* means server-side, because this container
is the only one running on Europe/Berlin (see the TZ note in the Dockerfile);
a visitor's own `new Date()` would pick the wrong day boundary abroad.

`/live` answers a question the database structurally cannot: rows are written
only when a title *changes*, so the newest row looks identical whether the song
is playing right now or the stream went down an hour ago. That state exists only
in the running Poller's memory - which makes this the one endpoint that reads
from something other than SQLite.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from . import db

router = APIRouter()

# Rows returned newest-first in /now. Enough that the client can skip past a run
# of jingles/station IDs to find the last real track, small enough to stay cheap.
RECENT_LIMIT = 10


def _item(row) -> dict:
    """Row -> wire object. Single-letter keys keep the bulk dump small; shared by
    both endpoints so the two payloads can never drift apart."""
    return {"id": row["id"], "t": row["ts"], "a": row["artist"], "s": row["song"], "r": row["raw"]}


@router.get("/health")
def health() -> dict:
    with db.connect() as conn:
        count = db.track_count(conn)
    return {"status": "ok", "tracks": count}


@router.get("/tracks")
def tracks(since_id: int = 0, limit: int | None = None) -> dict:
    with db.connect() as conn:
        rows = db.tracks_since(conn, since_id, limit)
    items = [_item(r) for r in rows]
    next_since_id = items[-1]["id"] if items else since_id
    return {"tracks": items, "next_since_id": next_since_id}


@router.get("/now")
def now(today: int = 1, limit: int = 60) -> dict:
    """Embed payload: the newest few tracks plus (optionally) today's list.

    `today=0` lets the compact "Zuletzt gespielt" widget skip the day list.
    `recent` is deliberately *not* day-scoped, so the widget doesn't blank out
    every night at midnight.
    """
    limit = max(1, min(limit, 500))
    # Naive local time, exactly as ingest.py writes `ts` - this container is
    # pinned to Europe/Berlin, so this is German wall-clock, not UTC.
    stamp = datetime.now()
    day = stamp.date().isoformat()
    with db.connect() as conn:
        recent = db.recent_tracks(conn, RECENT_LIMIT)
        today_rows = db.tracks_for_day(conn, day, limit) if today else []
        today_total = db.day_track_count(conn, day)
    return {
        "day": day,
        "server_time": stamp.isoformat(timespec="seconds"),
        "recent": [_item(r) for r in recent],
        "today": [_item(r) for r in today_rows],
        "today_total": today_total,
    }


@router.get("/live")
def live(request: Request) -> dict:
    """What is on the stream *right now* - the one question the tracks table
    can't answer.

    Rows are only written when a title *changes*, so the newest row looks
    identical whether the song is playing or the stream died an hour ago.
    Only the poller knows, and only in memory: whether the last poll actually
    reached a titled Icecast source, and how many people are listening.

    The response is deliberately a curated subset of Poller.snapshot() - that
    dict carries `last_error` (exception text, internal URLs) and poll counters,
    none of which belong on a public, cross-origin endpoint.
    """
    stamp = datetime.now()
    poller = getattr(request.app.state, "poller", None)
    snap = poller.snapshot() if poller else {}

    with db.connect() as conn:
        rows = db.recent_tracks(conn, 1)

    since = snap.get("since")
    listeners = snap.get("listeners")
    try:
        listeners = int(listeners)
    except (TypeError, ValueError):
        listeners = None

    return {
        "server_time": stamp.isoformat(timespec="seconds"),
        "on_air": bool(snap.get("on_air")),
        "listeners": listeners,
        "since": since.isoformat(timespec="seconds") if since else None,
        "last": _item(rows[0]) if rows else None,
    }
