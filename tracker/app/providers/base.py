"""Streaming-provider plugin interface.

A provider only needs to answer three narrow questions: can you find this
track, does today's destination playlist exist (creating it if not), and can
you add tracks to it. All day-grouping, dedup, and caching logic lives once
in sync_engine.SyncEngine, generic over any provider - so a new platform
(Tidal, Deezer, ...) is a single new module implementing this Protocol plus
a [[providers]] entry in config.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass
class TrackMatch:
    uri: str
    title: str
    artists: list[str]


@dataclass
class PlaylistRef:
    playlist_id: str
    name: str
    existing_uris: set[str]


@dataclass
class SyncStats:
    added: int = 0
    duplicate: int = 0
    missing: int = 0
    playlist: str = ""


class StreamingProvider(Protocol):
    name: str  # e.g. "spotify" - matches TOML `type` and the DB `provider` column

    def search_track(self, artist: str, song: str) -> TrackMatch | None:
        """Find a track, verifying the match is plausible. None if not found."""
        ...

    def ensure_day_playlist(self, day: date, name: str, description: str) -> PlaylistRef:
        """Find-or-create the destination playlist for a calendar day."""
        ...

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        """Add tracks to a playlist. Implementations must chunk to API limits."""
        ...
