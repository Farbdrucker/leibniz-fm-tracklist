"""One-off importer: legacy playlist.csv (+ optional spotify_cache.json) -> SQLite.

Run inside the built tracker image so the migrated DB always matches the
live schema:

    docker compose run --rm -v "$(pwd)":/legacy:ro tracker \\
        python -m app.migrate --csv /legacy/playlist.csv \\
                               --cache /legacy/spotify_cache.json \\
                               --db /data/tracklist.db

The sync cursor is deliberately reset to 0 (full resync) rather than set to
a cutoff: the migrated provider_tracks/provider_search_cache rows turn that
replay into pure dedup lookups (no repeat Spotify searches for anything
already classified), and it self-heals any gap between the legacy cache and
the moment of migration that a cutoff would silently drop.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import db


def import_csv(conn, csv_path: Path) -> int:
    count = 0
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 4 or row[0] == "zeit" or not row[0]:
                continue
            ts, artist, song, raw = row[0], row[1], row[2], row[3]
            day = ts[:10]
            db.insert_track(conn, ts=ts, day=day, artist=artist, song=song, raw=raw)
            count += 1
    return count


def import_cache(conn, cache_path: Path, provider: str = "spotify") -> tuple[int, int]:
    cache = json.loads(cache_path.read_text())
    found = cache.get("found", {})
    missing = set(cache.get("missing", []))

    days_imported = 0
    tracks_imported = 0

    for day_key, entry in cache.get("days", {}).items():
        db.set_provider_playlist(conn, provider, day_key, entry["playlist_id"], entry["name"])
        days_imported += 1

        resolved_uris: set[str] = set()
        for key in entry.get("keys", []):
            if key in missing:
                db.upsert_provider_track(conn, provider, day_key, key, None, "missing")
            else:
                uri = found.get(key)
                status = "found" if uri else "missing"
                db.upsert_provider_track(conn, provider, day_key, key, uri, status)
                if uri:
                    resolved_uris.add(uri)
            tracks_imported += 1

        # URIs present in the playlist that weren't resolved via a text key
        # (adopted from a pre-existing playlist) - preserves the old
        # `uri in uris` duplicate guard with a synthetic key.
        for uri in entry.get("uris", []):
            if uri not in resolved_uris:
                db.upsert_provider_track(conn, provider, day_key, f"uri:{uri}", uri, "found")
                tracks_imported += 1

    for key, uri in found.items():
        db.upsert_search_cache(conn, provider, key, uri, "found")
    for key in missing:
        db.upsert_search_cache(conn, provider, key, None, "missing")

    db.set_sync_cursor(conn, provider, 0)
    return days_imported, tracks_imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy CSV/JSON into SQLite")
    parser.add_argument("--csv", required=True, help="path to playlist.csv")
    parser.add_argument("--cache", help="path to spotify_cache.json (optional)")
    parser.add_argument("--db", required=True, help="path to the new SQLite DB file")
    parser.add_argument("--force", action="store_true",
                        help="wipe an existing non-empty DB before importing")
    args = parser.parse_args()

    db.configure(args.db)
    db.init_schema()

    with db.connect() as conn:
        existing = db.track_count(conn)
        if existing and not args.force:
            sys.exit(f"{args.db} already has {existing} tracks - pass --force to overwrite.")
        if existing and args.force:
            conn.executescript(
                "DELETE FROM tracks; DELETE FROM provider_playlists; "
                "DELETE FROM provider_tracks; DELETE FROM provider_search_cache; "
                "DELETE FROM sync_state;"
            )

        track_count = import_csv(conn, Path(args.csv))
        print(f"imported {track_count} tracks from {args.csv}")

        if args.cache:
            days, cache_tracks = import_cache(conn, Path(args.cache))
            print(f"imported {days} day-playlists and {cache_tracks} cached track entries "
                  f"from {args.cache}")
            print("sync cursor reset to 0 - next sync run will replay all history "
                  "(cheap: fully backed by the imported cache)")


if __name__ == "__main__":
    main()
