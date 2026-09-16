#!/usr/bin/env python3
"""
Minimaler Spotify-Client fuer den Radio-Tracker.

Zwei Aufgaben:
  1. OAuth-Login (einmalig), um ein Refresh-Token zu bekommen:
         python spotify_client.py login
  2. Titel suchen und in eine Tagesplaylist einsortieren (PlaylistSync).

Zugangsdaten kommen aus Umgebungsvariablen:
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
Das Refresh-Token landet nach dem Login in spotify_token.json.
"""

from __future__ import annotations

import base64
import difflib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# Basis-URLs sind ueberschreibbar, damit man gegen einen Fake-Server testen kann.
API_BASE = os.environ.get("SPOTIFY_API_BASE", "https://api.spotify.com")
ACCOUNTS = os.environ.get("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com")

REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"

TOKEN_FILE = Path("spotify_token.json")
CACHE_FILE = Path("spotify_cache.json")


# --------------------------------------------------------------------------
# Textnormalisierung fuer Suche und Dublettenerkennung
# --------------------------------------------------------------------------

_BRACKETS = re.compile(r"[\(\[].*?[\)\]]")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*", re.I)
_NOISE = re.compile(r"[^a-z0-9äöüß ]+")


def normalize(text: str) -> str:
    """'Caesars (Remastered) feat. X' -> 'caesars'"""
    text = text.lower()
    text = _BRACKETS.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = text.replace("&", " and ")
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def track_key(artist: str, song: str) -> str:
    """Schluessel fuer die Dublettenpruefung, unabhaengig von Schreibweise."""
    return f"{normalize(artist)}|{normalize(song)}"


# --------------------------------------------------------------------------
# API-Client
# --------------------------------------------------------------------------

class SpotifyError(RuntimeError):
    pass


class Spotify:
    """Duenne Huelle um die Web API. Kuemmert sich um Token-Refresh,
    Rate-Limits (429) und die Feld-Umbenennungen der Februar-2026-Migration."""

    def __init__(self, client_id: str, client_secret: str,
                 refresh_token: str | None = None,
                 token_file: Path = TOKEN_FILE):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        self.refresh_token = refresh_token or self._load_refresh_token()
        if not self.refresh_token:
            raise SpotifyError(
                "Kein Refresh-Token. Einmalig 'python spotify_client.py login' ausfuehren."
            )
        self.access_token = ""
        self.expires_at = 0.0
        self.session = requests.Session()
        self._lock = threading.Lock()

    # -- Token ------------------------------------------------------------

    def _load_refresh_token(self) -> str:
        if os.environ.get("SPOTIFY_REFRESH_TOKEN"):
            return os.environ["SPOTIFY_REFRESH_TOKEN"]
        if self.token_file.exists():
            return json.loads(self.token_file.read_text()).get("refresh_token", "")
        return ""

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
                raise SpotifyError(f"Token-Refresh fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            self.access_token = data["access_token"]
            self.expires_at = time.time() + data.get("expires_in", 3600)
            # Spotify kann ein rotiertes Refresh-Token zurueckgeben.
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
                self._save_tokens()
            return self.access_token

    def _save_tokens(self) -> None:
        self.token_file.write_text(json.dumps({"refresh_token": self.refresh_token}, indent=2))

    # -- Requests ---------------------------------------------------------

    def request(self, method: str, path: str, *, params=None, json_body=None,
                tries: int = 4) -> dict:
        for attempt in range(tries):
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
                self.access_token = ""      # erzwingt Refresh
                continue
            if resp.status_code >= 400:
                raise SpotifyError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
            if not resp.content:
                return {}
            return resp.json()
        raise SpotifyError(f"{method} {path}: nach {tries} Versuchen aufgegeben")

    # -- Endpunkte --------------------------------------------------------

    def search_track(self, artist: str, song: str,
                     min_artist: float = 0.55, min_title: float = 0.60) -> dict | None:
        """Sucht einen Titel und gibt ihn nur zurueck, wenn er plausibel passt.

        Die Volltextsuche liefert fast immer irgendein Ergebnis - ohne Pruefung
        landen sonst wildfremde Songs in der Playlist.
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
        # Seit der Februar-2026-Migration: /v1/me/playlists statt /v1/users/{id}/playlists
        data = self.request("POST", "/v1/me/playlists",
                            json_body={"name": name, "public": public,
                                       "description": description})
        return data["id"]

    def playlist_uris(self, playlist_id: str) -> set[str]:
        """Alle bereits enthaltenen Track-URIs - Grundlage der Dublettenpruefung."""
        uris: set[str] = set()
        offset = 0
        while True:
            data = self._playlist_items(playlist_id, offset)
            items = data.get("items", [])
            for entry in items:
                # Neu heisst der Wrapper 'item', 'track' gilt noch uebergangsweise.
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
        for i in range(0, len(uris), 100):          # API-Limit: 100 pro Request
            batch = uris[i:i + 100]
            try:
                self.request("POST", f"/v1/playlists/{playlist_id}/items",
                             json_body={"uris": batch})
            except SpotifyError as exc:
                if "404" not in str(exc):
                    raise
                self.request("POST", f"/v1/playlists/{playlist_id}/tracks",
                             json_body={"uris": batch})


# --------------------------------------------------------------------------
# Tagesplaylists
# --------------------------------------------------------------------------

class PlaylistSync:
    """Sortiert Titel in eine Playlist pro Kalendertag, ohne Dubletten.

    Der Cache haelt fest, welche Playlist zu welchem Tag gehoert, welche Titel
    dort schon liegen und welche Suchanfragen bereits erfolglos waren - damit
    ueberlebt das alles einen Neustart und kostet keine unnoetigen API-Calls.
    """

    def __init__(self, spotify: Spotify,
                 name_template: str = "Leibniz.fm {date}",
                 description: str = "Automatisch mitgeschnitten von leibniz.fm",
                 cache_file: Path = CACHE_FILE):
        self.sp = spotify
        self.name_template = name_template
        self.description = description
        self.cache_file = Path(cache_file)
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        if self.cache_file.exists():
            try:
                return json.loads(self.cache_file.read_text())
            except json.JSONDecodeError:
                pass
        return {"days": {}, "found": {}, "missing": []}

    def _save_cache(self) -> None:
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=2, ensure_ascii=False))
        tmp.replace(self.cache_file)          # atomar, kein halbes JSON bei Strg+C

    def playlist_name(self, day: date) -> str:
        return self.name_template.format(date=day.isoformat(),
                                         datum=day.strftime("%d.%m.%Y"))

    def _day_entry(self, day: date) -> dict:
        key = day.isoformat()
        entry = self.cache["days"].get(key)
        if entry:
            return entry

        name = self.playlist_name(day)
        playlist_id = ""
        for playlist in self.sp.my_playlists():        # evtl. schon vorhanden
            if playlist.get("name") == name:
                playlist_id = playlist["id"]
                break
        if not playlist_id:
            playlist_id = self.sp.create_playlist(name, self.description)
            uris: set[str] = set()
        else:
            uris = self.sp.playlist_uris(playlist_id)

        entry = {"playlist_id": playlist_id, "name": name,
                 "uris": sorted(uris), "keys": []}
        self.cache["days"][key] = entry
        self._save_cache()
        return entry

    def sync(self, tracks) -> dict:
        """tracks: iterable von (date, interpret, song). Gibt eine Statistik zurueck."""
        stats = {"added": 0, "duplicate": 0, "missing": 0, "playlist": ""}

        by_day: dict[date, list[tuple[str, str]]] = {}
        for day, artist, song in tracks:
            if not artist:                     # Moderation/Jingle ueberspringen
                continue
            by_day.setdefault(day, []).append((artist, song))

        for day, items in sorted(by_day.items()):
            entry = self._day_entry(day)
            uris = set(entry["uris"])
            keys = set(entry["keys"])
            to_add: list[str] = []

            for artist, song in items:
                key = track_key(artist, song)
                if key in keys:                        # heute schon gelaufen
                    stats["duplicate"] += 1
                    continue
                if key in self.cache["missing"]:
                    stats["missing"] += 1
                    keys.add(key)
                    continue

                uri = self.cache["found"].get(key)
                if not uri:
                    found = self.sp.search_track(artist, song)
                    if not found:
                        self.cache["missing"].append(key)
                        stats["missing"] += 1
                        keys.add(key)
                        continue
                    uri = found["uri"]
                    self.cache["found"][key] = uri

                keys.add(key)
                if uri in uris:                        # anderer Titelstring, gleicher Song
                    stats["duplicate"] += 1
                    continue
                uris.add(uri)
                to_add.append(uri)

            if to_add:
                self.sp.add_uris(entry["playlist_id"], to_add)
                stats["added"] += len(to_add)

            entry["uris"] = sorted(uris)
            entry["keys"] = sorted(keys)
            stats["playlist"] = entry["name"]

        self._save_cache()
        return stats


# --------------------------------------------------------------------------
# Einmaliger Login
# --------------------------------------------------------------------------

def login() -> None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        sys.exit("Bitte SPOTIFY_CLIENT_ID und SPOTIFY_CLIENT_SECRET setzen.")

    state = secrets.token_urlsafe(16)
    params = {"client_id": client_id, "response_type": "code",
              "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state}
    auth_url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode(params)

    result: dict[str, str] = {}
    parsed = urllib.parse.urlparse(REDIRECT_URI)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in result and result.get("state") == state
            self.wfile.write(
                ("<h2>Geschafft - du kannst das Fenster schliessen.</h2>" if ok
                 else "<h2>Fehlgeschlagen. Zurueck ins Terminal.</h2>").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    print("Browser oeffnet sich. Falls nicht, diese URL aufrufen:\n", auth_url)
    webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()

    if "code" not in result:
        sys.exit(f"Kein Code erhalten: {result}")
    if result.get("state") != state:
        sys.exit("State stimmt nicht - Abbruch.")

    raw = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        f"{ACCOUNTS}/api/token",
        data={"grant_type": "authorization_code", "code": result["code"],
              "redirect_uri": REDIRECT_URI},
        headers={"Authorization": "Basic " + raw}, timeout=15,
    )
    if resp.status_code != 200:
        sys.exit(f"Token-Tausch fehlgeschlagen: {resp.status_code} {resp.text[:300]}")

    TOKEN_FILE.write_text(json.dumps({"refresh_token": resp.json()["refresh_token"]}, indent=2))
    print(f"Refresh-Token gespeichert in {TOKEN_FILE.resolve()}")


def from_env(**kwargs) -> Spotify:
    return Spotify(os.environ.get("SPOTIFY_CLIENT_ID", ""),
                   os.environ.get("SPOTIFY_CLIENT_SECRET", ""), **kwargs)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(__doc__)
