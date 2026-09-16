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
    lock, purely so the optional local `--tui` dev mode can display them
    without running a second, redundant polling loop.
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

    def start(self) -> None:
        with db.connect() as conn:
            self._last_raw = db.last_raw(conn)
        self._thread = threading.Thread(target=self._run, daemon=True, name="poller")
        self._thread.start()

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
                        logger.info("new track: %s", raw)
            except Exception as exc:
                with self.lock:
                    self.polls += 1
                    self.errors += 1
                    self.last_poll = now
                    self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("poll failed")

            with self.lock:
                self.next_poll = datetime.now().timestamp() + self.interval
            self._stop.wait(self.interval)
