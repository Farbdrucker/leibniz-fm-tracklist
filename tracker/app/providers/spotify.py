"""Spotify Web API client + StreamingProvider adapter.

The HTTP wrapper (token refresh, rate-limit/401 retry, endpoint fallbacks for
Spotify's Feb-2026 API migration) is ported from the original spotify_client.py
almost verbatim. Unlike the original, the refresh token comes only from the
environment (no local spotify_token.json fallback) - this process is meant to
run unattended in a container.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from datetime import date
from pathlib import Path

import requests

from ..textmatch import similarity
from .base import PlaylistRef, TrackMatch

API_BASE = os.environ.get("SPOTIFY_API_BASE", "https://api.spotify.com")
ACCOUNTS = os.environ.get("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com")


class SpotifyError(RuntimeError):
    pass


class Spotify:
    """Thin wrapper around the Web API: token refresh, rate limits (429), and
    the field renames from the February 2026 migration."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 token_file: Path | None = None):
        if not refresh_token:
            raise SpotifyError(
                "No refresh token configured (SPOTIFY_REFRESH_TOKEN[_FILE]). "
                "Run scripts/spotify_login.py once to obtain one."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.token_file = token_file
        self.access_token = ""
        self.expires_at = 0.0
        self.session = requests.Session()
        self._lock = threading.Lock()

    # -- token --------------------------------------------------------------

    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _ensure_token(self) -> str:
        with self._lock:
            if self.access_token and time.time() < self.expires_at - 60:
                return self.access_token
            resp = self.session.post(
                f"{ACCOUNTS}/api/token",
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                headers={"Authorization": self._basic_auth()},
                timeout=15,
            )
            if resp.status_code != 200:
                raise SpotifyError(f"token refresh failed: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            self.access_token = data["access_token"]
            self.expires_at = time.time() + data.get("expires_in", 3600)
            # Spotify may rotate the refresh token.
            if data.get("refresh_token") and data["refresh_token"] != self.refresh_token:
                self.refresh_token = data["refresh_token"]
                if self.token_file is not None:
                    self.token_file.write_text(self.refresh_token)
            return self.access_token

    # -- requests -------------------------------------------------------------

    def request(self, method: str, path: str, *, params=None, json_body=None,
                tries: int = 4) -> dict:
        for _ in range(tries):
            token = self._ensure_token()
            resp = self.session.request(
                method, f"{API_BASE}{path}",
                params=params, json=json_body,
                headers={"Authorization": f"Bearer {token}"}, timeout=20,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "3")) + 1
                time.sleep(min(wait, 60))
                continue
            if resp.status_code == 401:
                self.access_token = ""  # force refresh
                continue
            if resp.status_code >= 400:
                raise SpotifyError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
            if not resp.content:
                return {}
            return resp.json()
        raise SpotifyError(f"{method} {path}: gave up after {tries} attempts")

    # -- endpoints --------------------------------------------------------------

    def search_track(self, artist: str, song: str,
                      min_artist: float = 0.55, min_title: float = 0.60) -> dict | None:
        """Search for a track, returning it only if the match is plausible.

        Full-text search almost always returns something - without the
        similarity check, unrelated songs end up in the playlist.
        """
        queries = [f'artist:"{artist}" track:"{song}"', f"{artist} {song}"]
        best, best_score = None, 0.0

        for query in queries:
            data = self.request("GET", "/v1/search",
                                 params={"q": query, "type": "track", "limit": 5})
            for item in data.get("tracks", {}).get("items", []):
                names = [a["name"] for a in item.get("artists", [])]
                a_score = max((similarity(artist, n) for n in names), default=0.0)
                t_score = similarity(song, item.get("name", ""))
                if a_score >= min_artist and t_score >= min_title:
                    score = a_score + t_score
                    if score > best_score:
                        best, best_score = item, score
            if best:
                break
        return best

    def my_playlists(self):
        offset = 0
        while True:
            data = self.request("GET", "/v1/me/playlists",
                                 params={"limit": 50, "offset": offset})
            items = data.get("items", [])
            yield from (p for p in items if p)
            if len(items) < 50:
                return
            offset += 50

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> str:
        # Since the Feb-2026 migration: /v1/me/playlists instead of /v1/users/{id}/playlists
        data = self.request("POST", "/v1/me/playlists",
                             json_body={"name": name, "public": public,
                                        "description": description})
        return data["id"]

    def playlist_uris(self, playlist_id: str) -> set[str]:
        """All URIs already in the playlist - basis of the dedup check."""
        uris: set[str] = set()
        offset = 0
        while True:
            data = self._playlist_items(playlist_id, offset)
            items = data.get("items", [])
            for entry in items:
                # renamed wrapper is 'item'; 'track' still works transitionally
                track = entry.get("item") or entry.get("track") or {}
                if track.get("uri"):
                    uris.add(track["uri"])
            if len(items) < 100:
                return uris
            offset += 100

    def _playlist_items(self, playlist_id: str, offset: int) -> dict:
        try:
            return self.request("GET", f"/v1/playlists/{playlist_id}/items",
                                 params={"limit": 100, "offset": offset})
        except SpotifyError as exc:
            if "404" not in str(exc):
                raise
            return self.request("GET", f"/v1/playlists/{playlist_id}/tracks",
                                 params={"limit": 100, "offset": offset})

    def add_uris(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):  # API limit: 100 per request
            batch = uris[i:i + 100]
            try:
                self.request("POST", f"/v1/playlists/{playlist_id}/items",
                              json_body={"uris": batch})
            except SpotifyError as exc:
                if "404" not in str(exc):
                    raise
                self.request("POST", f"/v1/playlists/{playlist_id}/tracks",
                              json_body={"uris": batch})


class SpotifyProvider:
    """Adapts Spotify to the StreamingProvider protocol."""

    name = "spotify"

    def __init__(self, spotify: Spotify,
                 min_artist_similarity: float = 0.55,
                 min_title_similarity: float = 0.60):
        self.sp = spotify
        self.min_artist_similarity = min_artist_similarity
        self.min_title_similarity = min_title_similarity

    def search_track(self, artist: str, song: str) -> TrackMatch | None:
        found = self.sp.search_track(artist, song, self.min_artist_similarity,
                                      self.min_title_similarity)
        if not found:
            return None
        return TrackMatch(
            uri=found["uri"],
            title=found.get("name", ""),
            artists=[a["name"] for a in found.get("artists", [])],
        )

    def ensure_day_playlist(self, day: date, name: str, description: str) -> PlaylistRef:
        for playlist in self.sp.my_playlists():
            if playlist.get("name") == name:
                return PlaylistRef(
                    playlist_id=playlist["id"], name=name,
                    existing_uris=self.sp.playlist_uris(playlist["id"]),
                )
        playlist_id = self.sp.create_playlist(name, description)
        return PlaylistRef(playlist_id=playlist_id, name=name, existing_uris=set())

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        self.sp.add_uris(playlist_id, uris)
