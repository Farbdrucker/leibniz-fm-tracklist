"""Thin web backend: serves the static UI and proxies the tracker's read API.

The browser never talks to the tracker directly - that keeps tracker off any
published port and means *it* needs no CORS configuration.

Two deliberate exceptions to "no CORS", both scoped to /api/now, which feeds the
widgets embedded on leibniz.fm (see static/embed.js):

  * `Access-Control-Allow-Origin: *` is set as a plain header on that one
    response rather than via CORSMiddleware - middleware would also attach CORS
    to /api/tracks and the static mount. `*` rather than an origin allowlist
    because ACAO is a browser policy, not an access control: this data is
    public, unauthenticated and read-only, so an allowlist would add no
    security while adding a www/non-www footgun, a `Vary: Origin`, and a broken
    Gutenberg editor preview (which sandboxes to `Origin: null`).
    Credentials stay off, which is the header that would actually matter.

  * A short in-process memo with single-flight. This, not Cache-Control, is what
    protects SQLite: there is no shared cache anywhere in the stack, so N
    simultaneous WordPress page views would otherwise be N tracker calls.

This service has no TZ/tzdata (only the tracker image does), so it must never
generate or reformat a timestamp - every time string originates in the tracker.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

TRACKER_API_URL = os.environ.get("TRACKER_API_URL", "http://tracker:8000")
STATIC_DIR = Path(__file__).parent / "static"

# Tracker polls Icecast every 20s, so a 10s memo never hides a full poll.
NOW_TTL = 10.0

app = FastAPI(title="leibniz.fm web")

_now_cache: dict[tuple[int, int], tuple[float, dict]] = {}
_now_lock = asyncio.Lock()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


async def _tracker_get(path: str, params: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{TRACKER_API_URL}{path}", params=params)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"tracker unreachable: {exc}") from exc
    return resp.json()


@app.get("/api/tracks")
async def api_tracks(since_id: int = 0, limit: int | None = None):
    params: dict = {"since_id": since_id}
    if limit is not None:
        params["limit"] = limit
    return JSONResponse(await _tracker_get("/tracks", params))


@app.get("/api/now")
async def api_now(today: int = 1, limit: int = 60):
    key = (1 if today else 0, max(1, min(limit, 500)))
    now = time.monotonic()

    cached = _now_cache.get(key)
    if cached and now - cached[0] < NOW_TTL:
        data = cached[1]
    else:
        async with _now_lock:
            # Re-check inside the lock: whoever held it may have just refreshed,
            # which is what collapses a burst of viewers into one tracker call.
            cached = _now_cache.get(key)
            if cached and time.monotonic() - cached[0] < NOW_TTL:
                data = cached[1]
            else:
                data = await _tracker_get("/now", {"today": key[0], "limit": key[1]})
                _now_cache[key] = (time.monotonic(), data)

    return JSONResponse(
        data,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": f"public, max-age={int(NOW_TTL)}",
        },
    )


# Mounted last: explicit routes above are matched first, this catches the rest.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
