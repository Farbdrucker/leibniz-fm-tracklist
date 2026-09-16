"""Icecast polling: fetch the current title, detect changes, persist to SQLite."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import requests

from . import db

logger = logging.getLogger("tracker.ingest")


def split_title(raw: str) -> tuple[str, str]:
    """'Caesars - Boo Boo Goo Goo' -> ('Caesars', 'Boo Boo Goo Goo').

    Without a separator (moderation, jingle, station name) artist stays empty.
    """
    if " - " in raw:
        artist, song = raw.split(" - ", 1)
        return artist.strip(), song.strip()
    return "", raw.strip()


def _parse_ts(stamp: str) -> datetime | None:
    """Parse a stored `ts` back into a naive local datetime, or None."""
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def fetch_status(url: str, timeout: float = 10.0) -> dict:
    """Return the first source with a title, plus its metadata.

    Icecast returns 'source' as a list when multiple mounts are active,
    and as a single object when there's only one - handle both.
    """
    data = requests.get(url, timeout=timeout).json()
    sources = data.get("icestats", {}).get("source", [])
    if isinstance(sources, dict):
        sources = [sources]

    for src in sources:
        title = (src.get("title") or src.get("yp_currently_playing") or "").strip()
        if title:
            return {
                "title": title,
                "listeners": src.get("listeners"),
                "bitrate": src.get("bitrate"),
                "mount": src.get("listenurl", "").rsplit("/", 1)[-1],
            }
    return {}


class Poller:
    """Background thread: polls Icecast and inserts a row on every title change.

    Also tracks its own live stats (listeners/bitrate/poll timing) behind a
    lock - originally just so the optional local `--tui` dev mode could display
    them without a second polling loop. `snapshot()` now also feeds the /live
    endpoint, which is the only place in the app that can answer "is the stream
    on air *right now*": the tracks table records title changes, so it cannot
    distinguish "still playing" from "stream died an hour ago".
    """

    def __init__(self, url: str, interval: float):
        self.url = url
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_raw = ""

        self.lock = threading.Lock()
        self.listeners: int | None = None
        self.bitrate: int | None = None
        self.mount: str = ""
        self.last_poll: datetime | None = None
        self.next_poll: float | None = None
        self.polls: int = 0
        self.errors: int = 0
        self.logged: int = 0
        self.last_error: str = ""
        # Live state: the title currently on the stream, and when it started.
        self.current_raw: str = ""
        self.current_since: datetime | None = None
        self.stream_ok: bool = False

    def start(self) -> None:
        with db.connect() as conn:
            self._last_raw = db.last_raw(conn)
            rows = db.recent_tracks(conn, 1)
        # Seed from the newest row so a restart mid-track doesn't reset the
        # "runs since" clock to now.
        if rows:
            self.current_raw = rows[0]["raw"]
            parsed = _parse_ts(rows[0]["ts"])
            self.current_since = parsed
        self._thread = threading.Thread(target=self._run, daemon=True, name="poller")
        self._thread.start()

    def snapshot(self) -> dict:
        """Thread-safe view of the live state. Includes `last_error`, so callers
        exposing this publicly must strip it (see api.live)."""
        with self.lock:
            # Treat a poll that never landed, or landed too long ago, as off
            # air - that covers a wedged thread, not just a dead stream.
            fresh = (
                self.last_poll is not None
                and (datetime.now() - self.last_poll).total_seconds() < self.interval * 3
            )
            return {
                "on_air": bool(self.stream_ok and fresh),
                "raw": self.current_raw,
                "since": self.current_since,
                "listeners": self.listeners,
                "bitrate": self.bitrate,
                "mount": self.mount,
                "last_poll": self.last_poll,
                "polls": self.polls,
                "errors": self.errors,
                "last_error": self.last_error,
            }

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            try:
                info = fetch_status(self.url)
                with self.lock:
                    self.polls += 1
                    self.last_poll = now
                    self.last_error = ""
                    # A reachable Icecast with no titled source is still "off
                    # air" as far as a listener is concerned.
                    self.stream_ok = bool(info)
                    if info:
                        self.listeners = info["listeners"]
                        self.bitrate = info["bitrate"]
                        self.mount = info["mount"]

                if info:
                    raw = info["title"]
                    if raw != self._last_raw:
                        artist, song = split_title(raw)
                        with db.connect() as conn:
                            db.insert_track(
                                conn,
                                ts=now.isoformat(timespec="seconds"),
                                day=now.date().isoformat(),
                                artist=artist,
                                song=song,
                                raw=raw,
                            )
                        self._last_raw = raw
                        with self.lock:
                            self.logged += 1
                            self.current_raw = raw
                            self.current_since = now
                        logger.info("new track: %s", raw)
            except Exception as exc:
                with self.lock:
                    self.polls += 1
                    self.errors += 1
                    self.last_poll = now
                    self.stream_ok = False
                    self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("poll failed")

            with self.lock:
                self.next_poll = datetime.now().timestamp() + self.interval
            self._stop.wait(self.interval)
