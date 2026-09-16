"""Generic day-grouping/dedup orchestration, agnostic of the streaming provider.

Backed by SQLite: provider_playlists (playlist identity per day),
provider_tracks (per-day dedup), provider_search_cache (global cross-day
search cache), sync_state (resume cursor). A provider only has to answer the
three StreamingProvider questions - everything else lives here once, so a new
platform doesn't need to reimplement dedup/caching.

Each sync() call runs as a single SQLite transaction, matching the original
script's cache-saved-once-at-the-end-of-a-batch semantics: if the process
crashes mid-batch, nothing from that batch is persisted and the next run
simply redoes it (a track already added to a remote playlist but not yet
recorded locally could be re-added on retry - the same edge case the
original JSON-cache design had, and rare enough for a personal radio feed to
accept rather than add complexity for).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from . import db
from .providers.base import StreamingProvider, SyncStats
from .textmatch import track_key

logger = logging.getLogger("tracker.sync_engine")


class SyncEngine:
    def __init__(self, provider: StreamingProvider, name_template: str, description: str):
        self.provider = provider
        self.name_template = name_template
        self.description = description

    def playlist_name(self, day: date) -> str:
        return self.name_template.format(date=day.isoformat(), datum=day.strftime("%d.%m.%Y"))

    def sync(self) -> SyncStats:
        stats = SyncStats()
        provider_name = self.provider.name

        with db.connect() as conn:
            cursor = db.get_sync_cursor(conn, provider_name)
            rows = db.tracks_for_sync(conn, cursor)
            if not rows:
                return stats

            by_day: dict[str, list] = defaultdict(list)
            for row in rows:
                by_day[row["day"]].append(row)

            max_id = cursor
            for day_key in sorted(by_day):
                day = date.fromisoformat(day_key)
                day_rows = by_day[day_key]
                day_stats = self._sync_day(conn, provider_name, day, day_rows)
                stats.added += day_stats.added
                stats.duplicate += day_stats.duplicate
                stats.missing += day_stats.missing
                stats.playlist = day_stats.playlist or stats.playlist
                max_id = max(max_id, max(r["id"] for r in day_rows))

            db.set_sync_cursor(conn, provider_name, max_id)

        return stats

    def _sync_day(self, conn, provider_name: str, day: date, rows: list) -> SyncStats:
        stats = SyncStats()
        day_key = day.isoformat()

        existing = db.get_provider_playlist(conn, provider_name, day_key)
        if existing:
            playlist_id, playlist_name = existing["playlist_id"], existing["name"]
        else:
            name = self.playlist_name(day)
            ref = self.provider.ensure_day_playlist(day, name, self.description)
            playlist_id, playlist_name = ref.playlist_id, ref.name
            db.set_provider_playlist(conn, provider_name, day_key, playlist_id, playlist_name)
            for uri in ref.existing_uris:
                db.upsert_provider_track(conn, provider_name, day_key, f"uri:{uri}", uri, "found")

        stats.playlist = playlist_name
        to_add: list[str] = []

        for row in rows:
            key = track_key(row["artist"], row["song"])

            if db.get_provider_track(conn, provider_name, day_key, key):
                stats.duplicate += 1
                continue

            cached = db.get_search_cache(conn, provider_name, key)
            if cached and cached["status"] == "missing":
                db.upsert_provider_track(conn, provider_name, day_key, key, None, "missing")
                stats.missing += 1
                continue

            uri = cached["uri"] if cached and cached["status"] == "found" else None
            if uri is None:
                match = self.provider.search_track(row["artist"], row["song"])
                if match is None:
                    db.upsert_search_cache(conn, provider_name, key, None, "missing")
                    db.upsert_provider_track(conn, provider_name, day_key, key, None, "missing")
                    stats.missing += 1
                    continue
                db.upsert_search_cache(conn, provider_name, key, match.uri, "found")
                uri = match.uri

            duplicate = db.day_has_uri(conn, provider_name, day_key, uri)
            db.upsert_provider_track(conn, provider_name, day_key, key, uri, "found")
            if duplicate:
                stats.duplicate += 1
                continue
            to_add.append(uri)

        if to_add:
            self.provider.add_tracks(playlist_id, to_add)
            stats.added += len(to_add)

        return stats
