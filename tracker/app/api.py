"""Tracker's internal read API - deliberately minimal: a health check and a
bulk track dump. Filtering/search/calendar aggregation stay client-side in
the web UI (fed by this endpoint), so that proven logic isn't reimplemented
in both Python and JS.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import db

router = APIRouter()


@router.get("/health")
def health() -> dict:
    with db.connect() as conn:
        count = db.track_count(conn)
    return {"status": "ok", "tracks": count}


@router.get("/tracks")
def tracks(since_id: int = 0, limit: int | None = None) -> dict:
    with db.connect() as conn:
        rows = db.tracks_since(conn, since_id, limit)
    items = [
        {"id": r["id"], "t": r["ts"], "a": r["artist"], "s": r["song"], "r": r["raw"]}
        for r in rows
    ]
    next_since_id = items[-1]["id"] if items else since_id
    return {"tracks": items, "next_since_id": next_since_id}
