"""Song info orchestration: ask each source, merge into one display-ready
document, cache it in song_metadata, and keep newly played songs warm.

Two entry points share one cache and one set of rate limiters:

  * MetadataWorker (background thread) - gives new tracks their slug, then
    fetches info for songs as they are played, so the newest song's page and
    the "Zuletzt gespielt" hero are usually ready before anyone asks.
  * Enricher.ensure() from the /songs endpoint - the on-demand path when
    someone opens a song that was never fetched (the archive predates this
    feature, and the worker deliberately doesn't crawl it).

Every source is optional and isolated: a failing Discogs never costs the
MusicBrainz data, and none of this can affect ingestion.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from .. import db
from ..config import MetadataSettings
from ..textmatch import similarity, song_slug
from .discogs import Discogs
from .http import MetadataError
from .musicbrainz import MusicBrainz
from .wikimedia import Wikimedia

logger = logging.getLogger("tracker.metadata")

# How long a cached result is trusted, by status. `partial` = something was
# found but a source errored, so the gap is worth retrying soon; `missing` is
# retried rarely, since obscure local acts seldom appear overnight.
REFRESH_AFTER = {
    "found": timedelta(days=90),
    "partial": timedelta(hours=6),
    "missing": timedelta(days=14),
    "error": timedelta(minutes=30),
}

# sync_state row holding the worker's position in the tracks table.
CURSOR = "metadata"
# On the very first start, only the newest few rows are enriched - not the
# whole archive, which would mean hours of API calls nobody asked for.
BOOTSTRAP_ROWS = 10


def needs_fetch(row) -> bool:
    if row is None:
        return True
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
    except (TypeError, ValueError):
        return True
    return datetime.now() - fetched > REFRESH_AFTER.get(row["status"], timedelta(0))


def as_payload(row) -> dict:
    """song_metadata row -> the `metadata` object of the /songs response."""
    if row is None:
        return {"status": "pending", "fetched_at": None, "needs_fetch": True, "data": {}}
    return {
        "status": row["status"],
        "fetched_at": row["fetched_at"],
        "needs_fetch": needs_fetch(row),
        "data": json.loads(row["data"] or "{}"),
    }


class Enricher:
    def __init__(self, settings: MetadataSettings):
        user_agent = f"leibniz-fm-tracklist/1.0 ( {settings.contact} )"
        self.musicbrainz = MusicBrainz(user_agent)
        self.wikimedia = Wikimedia(user_agent)
        self.discogs = (Discogs(user_agent, settings.discogs_key, settings.discogs_secret)
                        if settings.discogs_key and settings.discogs_secret else None)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def ensure(self, slug: str, artist: str, song: str) -> dict:
        """Cached info for a song, fetching it first if missing or stale.

        Single-flight per slug: the worker and a page view (or two page views)
        asking for the same new song wait for one fetch instead of doubling it.
        """
        with self._locks_guard:
            lock = self._locks.setdefault(slug, threading.Lock())
        with lock:
            with db.connect() as conn:
                row = db.get_song_metadata(conn, slug)
            if not needs_fetch(row):
                return as_payload(row)

            status, data = self.lookup(artist, song)
            if status == "error" and row is not None and row["status"] in ("found", "partial"):
                # A transient outage must not wipe what we already had.
                status, data = row["status"], json.loads(row["data"] or "{}")
            fetched_at = datetime.now().isoformat(timespec="seconds")
            with db.connect() as conn:
                db.upsert_song_metadata(conn, slug, status, json.dumps(data, ensure_ascii=False), fetched_at)
                row = db.get_song_metadata(conn, slug)
            logger.info("metadata %s: %s (%s)", status, slug,
                        ", ".join(f"{k}={v}" for k, v in data.get("sources", {}).items()))
            return as_payload(row)

    def lookup(self, artist: str, song: str) -> tuple[str, dict]:
        sources: dict[str, str] = {}

        mb = self._attempt(sources, "musicbrainz", lambda: self.musicbrainz.lookup(artist, song))
        mb = mb or {}
        mb_release = mb.get("release") or {}
        mb_artist = mb.get("artist") or {}

        dg: dict = {}
        if self.discogs is None:
            sources["discogs"] = "off"
        else:
            dg = self._attempt(sources, "discogs",
                               lambda: self.discogs.lookup(artist, song, mb_release.get("title", ""))) or {}

        wd: dict = {}
        wikidata_url = (mb_artist.get("links") or {}).get("wikidata")
        if wikidata_url:
            wd = self._attempt(sources, "wikidata", lambda: self.wikimedia.artist(wikidata_url)) or {}
        else:
            sources["wikidata"] = "missing"

        data = merge(mb, dg, wd)
        data["sources"] = sources
        values = set(sources.values())
        if "found" in values:
            status = "partial" if "error" in values else "found"
        else:
            status = "error" if "error" in values else "missing"
        return status, data

    @staticmethod
    def _attempt(sources: dict, name: str, fn):
        try:
            result = fn()
        except MetadataError as exc:
            logger.warning("%s lookup failed: %s", name, exc)
            sources[name] = "error"
            return None
        except Exception:
            logger.exception("%s lookup crashed", name)
            sources[name] = "error"
            return None
        sources[name] = "found" if result else "missing"
        return result


def merge(mb: dict, dg: dict, wd: dict) -> dict:
    """Three source documents -> the one shape song.html renders.

    MusicBrainz leads on facts (it matched the recording, not just an album),
    Discogs fills in what MusicBrainz doesn't carry (label, format, tracklist,
    styles, videos). Discogs' album-specific fields are only merged when both
    sources agree on the album - otherwise the tracklist could belong to a
    compilation while the title names the studio album.
    """
    recording = mb.get("recording") or {}
    mb_rel = mb.get("release") or {}
    dg_rel = dg.get("release") or {}
    same_album = bool(mb_rel and dg_rel and similarity(mb_rel.get("title", ""), dg_rel.get("title", "")) >= 0.8)
    use_dg_album = bool(dg_rel) and (same_album or not mb_rel)

    album: dict = {}
    if mb_rel or use_dg_album:
        album = {
            "title": mb_rel.get("title") or dg_rel.get("title", ""),
            "type": mb_rel.get("type", ""),
            "date": mb_rel.get("date") or (dg_rel.get("released") if use_dg_album else "")
                    or (str(dg_rel["year"]) if use_dg_album and dg_rel.get("year") else ""),
            "country": mb_rel.get("country") or (dg_rel.get("country", "") if use_dg_album else ""),
            "musicbrainz_url": mb_rel.get("url", ""),
        }
        if use_dg_album:
            for key in ("labels", "formats", "tracklist", "have", "want"):
                album[key] = dg_rel.get(key)
            album["discogs_url"] = dg_rel.get("url", "")

    genres: list[str] = []
    for name in (dg_rel.get("styles") or []) + (dg_rel.get("genres") or []) + (recording.get("tags") or []):
        if name and name.lower() not in (g.lower() for g in genres):
            genres.append(name)

    # Discogs' image may show a different release than `album` - still a
    # release with this song on it, which beats an empty square.
    cover = mb.get("cover") or dg.get("cover")

    mb_artist = mb.get("artist") or {}
    dg_artist = dg.get("artist") or {}
    links = mb_artist.get("links") or {}
    artist: dict = {}
    if mb_artist or dg_artist or wd:
        artist = {
            "name": mb_artist.get("name") or dg_artist.get("name", ""),
            "type": mb_artist.get("type", ""),
            "disambiguation": mb_artist.get("disambiguation", ""),
            "country": mb_artist.get("country", ""),
            "area": mb_artist.get("area", ""),
            "begin_area": mb_artist.get("begin_area", ""),
            "begin": mb_artist.get("begin", ""),
            "end": mb_artist.get("end", ""),
            "ended": mb_artist.get("ended", False),
            "genres": mb_artist.get("genres", []),
            "description": wd.get("description", ""),
            "wikipedia": wd.get("wikipedia"),
            # Discogs' profile is English and terser; only worth showing when
            # Wikipedia has nothing.
            "profile": "" if wd.get("wikipedia") else dg_artist.get("profile", ""),
            "members": dg_artist.get("members", []),
            "image": wd.get("image") or ({"url": dg_artist["image"], "source_url": dg_artist.get("url", "")}
                                         if dg_artist.get("image") else None),
            "website": wd.get("website") or links.get("official homepage", ""),
            "bandcamp_url": links.get("bandcamp", ""),
            "musicbrainz_url": mb_artist.get("url", ""),
            "discogs_url": dg_artist.get("url") or links.get("discogs", ""),
            "wikidata_url": wd.get("url", ""),
        }

    return {
        "recording": {
            "title": recording.get("title", ""),
            "artist": recording.get("artist", ""),
            "length_ms": recording.get("length_ms"),
            "first_release": recording.get("first_release", ""),
            "isrcs": recording.get("isrcs", []),
            "musicbrainz_url": recording.get("url", ""),
        } if recording else {},
        "album": album,
        "cover": cover,
        "genres": genres[:10],
        "artist": artist,
        "videos": dg.get("videos") or [],
    }


class MetadataWorker:
    """Background thread: assigns slugs to new tracks and enriches songs as
    they're played. Runs even when enrichment is disabled (`enricher=None`),
    because song pages - and their play history - need the slugs regardless."""

    def __init__(self, enricher: Enricher | None, interval: float = 10.0):
        self.enricher = enricher
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="metadata")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            try:
                self.step()
            except Exception:
                logger.exception("metadata worker step failed")
            if self._stop.wait(self.interval):
                return

    def step(self) -> None:
        with db.connect() as conn:
            assigned = db.assign_slugs(conn, song_slug)
            if assigned > 1:
                logger.info("assigned slugs to %d tracks", assigned)
            if self.enricher is None:
                return
            cursor = db.get_sync_cursor(conn, CURSOR, default=None)
            if cursor is None:
                cursor = max(db.max_track_id(conn) - BOOTSTRAP_ROWS, 0)
                db.set_sync_cursor(conn, CURSOR, cursor)
            rows = db.song_tracks_since(conn, cursor)

        for row in rows:
            if self._stop.is_set():
                return
            try:
                self.enricher.ensure(row["slug"], row["artist"], row["song"])
            except Exception:
                logger.exception("enriching %s failed", row["slug"])
            with db.connect() as conn:
                db.set_sync_cursor(conn, CURSOR, row["id"])
