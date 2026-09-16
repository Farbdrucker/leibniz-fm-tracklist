"""Thin web backend: serves the static UI and proxies /api/tracks to the
tracker service. The browser never talks to the tracker directly - this
keeps tracker off any published port and needs no CORS configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

TRACKER_API_URL = os.environ.get("TRACKER_API_URL", "http://tracker:8000")
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="leibniz.fm web")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/tracks")
async def api_tracks(since_id: int = 0, limit: int | None = None):
    params: dict = {"since_id": since_id}
    if limit is not None:
        params["limit"] = limit
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{TRACKER_API_URL}/tracks", params=params)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"tracker unreachable: {exc}") from exc
    return JSONResponse(resp.json())


# Mounted last: explicit routes above are matched first, this catches the rest.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
