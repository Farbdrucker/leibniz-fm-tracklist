"""Thin web backend: serves the static UI and proxies the tracker's read API.

The browser never talks to the tracker directly - that keeps tracker off any
published port and means *it* needs no CORS configuration.

Two deliberate exceptions to "no CORS", both scoped to the /api/now and
/api/live endpoints that feed the widgets embedded on leibniz.fm (see
static/embed.js):

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
# /live drives a ticking "on air" widget, so keep it tighter.
LIVE_TTL = 5.0

app = FastAPI(title="leibniz.fm web")

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


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


async def _memoized(path: str, params: dict, ttl: float) -> dict:
    """Fetch from the tracker at most once per `ttl`, collapsing concurrent
    callers. This - not Cache-Control - is what keeps a burst of embedded
    widget views from becoming a burst of SQLite reads."""
    key = (path,) + tuple(sorted(params.items()))

    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]

    async with _cache_lock:
        # Re-check inside the lock: whoever held it may have just refreshed.
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        data = await _tracker_get(path, params)
        _cache[key] = (time.monotonic(), data)
        return data


def _embeddable(data: dict, ttl: float) -> JSONResponse:
    # See the module docstring for why this is a plain header and why it is `*`.
    return JSONResponse(
        data,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": f"public, max-age={int(ttl)}",
        },
    )


@app.get("/api/now")
async def api_now(today: int = 1, limit: int = 60):
    params = {"today": 1 if today else 0, "limit": max(1, min(limit, 500))}
    return _embeddable(await _memoized("/now", params, NOW_TTL), NOW_TTL)


@app.get("/api/live")
async def api_live():
    return _embeddable(await _memoized("/live", {}, LIVE_TTL), LIVE_TTL)


# Mounted last: explicit routes above are matched first, this catches the rest.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
