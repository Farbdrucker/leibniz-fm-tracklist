"""Discogs: label, format, genres/styles, the album's tracklist, YouTube videos,
and a fallback cover and artist profile.

Uses key/secret auth (DISCOGS_CONSUMER_KEY/_SECRET), which is enough for
database search and image URLs without an OAuth user flow, and raises the rate
limit to 60 requests/minute. Discogs' terms require the attribution "Data
provided by Discogs", which song.html shows.
"""

from __future__ import annotations

import re

from ..textmatch import clean_title, similarity
from .http import HttpClient, RateLimiter, safe_url

API = "https://api.discogs.com"
MIN_ARTIST = 0.55
MIN_TITLE = 0.60
# Search hits whose tracklist is checked before giving up - each costs a request.
MAX_CANDIDATES = 3

_limiter = RateLimiter(1.05)

# Discogs appends "(2)" to disambiguate artists of the same name, and "*" to
# mark a name variation.
_ARTIST_SUFFIX = re.compile(r"\s*(\(\d+\)|\*)$")
_MARKUP_NAMED = re.compile(r"\[(?:a|l|m|r)=([^\]]+)\]")
_MARKUP_URL = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.S)
_MARKUP_REST = re.compile(r"\[/?[a-z]+\d*\]|\[[almr]\d+\]", re.I)


def artist_name(name: str) -> str:
    return _ARTIST_SUFFIX.sub("", name or "").strip()


def clean_profile(text: str) -> str:
    """Discogs' BBCode-ish profile markup -> plain text."""
    text = _MARKUP_URL.sub(r"\1", text or "")
    text = _MARKUP_NAMED.sub(r"\1", text)
    text = _MARKUP_REST.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Discogs:
    def __init__(self, user_agent: str, key: str, secret: str):
        self.http = HttpClient(user_agent, _limiter,
                               headers={"Authorization": f"Discogs key={key}, secret={secret}"})

    def lookup(self, artist: str, song: str, album_hint: str = "") -> dict | None:
        """`album_hint` is the album MusicBrainz found, if any - the strongest
        signal for picking the original release over a compilation."""
        found = self._find(artist, song, album_hint)
        if found is None:
            return None
        kind, detail = found

        release = detail
        if kind == "master" and detail.get("main_release"):
            release = self.http.get_json(f"{API}/releases/{detail['main_release']}") or {}

        result: dict = {"release": _release_info(kind, detail, release)}
        videos = [v for v in (detail.get("videos") or release.get("videos") or []) if safe_url(v.get("uri"))]
        result["videos"] = [
            {"title": v.get("title") or "", "url": v["uri"], "duration": v.get("duration")}
            for v in videos[:8]
        ]
        image = _primary_image(detail.get("images") or release.get("images"))
        if image:
            result["cover"] = {"url": image, "large": image, "source": "Discogs",
                               "source_url": result["release"]["url"]}

        artist_ref = next((a for a in detail.get("artists") or [] if a.get("id")), None)
        if artist_ref:
            result["artist"] = self._artist(artist_ref["id"])
        return result

    # -- search ---------------------------------------------------------------

    def _find(self, artist: str, song: str, album_hint: str) -> tuple[str, dict] | None:
        """Search masters first (one canonical entry per album), then plain
        releases (many singles have no master). A hit only counts if the
        artist matches and the song is actually on its tracklist.

        Discogs' `track` filter is unreliable (no hits for "I Can't Win" on
        Room On Fire), so the album MusicBrainz found is tried first."""
        searches = []
        if album_hint:
            searches += [("master", {"release_title": album_hint}), ("release", {"release_title": album_hint})]
        searches += [("master", {"track": clean_title(song) or song}), ("release", {"track": clean_title(song) or song})]
        tried: set[tuple[str, int]] = set()
        for kind, query in searches:
            params = {"type": kind, "artist": artist, "per_page": 15, **query}
            data = self.http.get_json(f"{API}/database/search", params) or {}
            results = [r for r in data.get("results") or [] if _artist_matches(r, artist)]
            for hit in _rank(results, album_hint)[:MAX_CANDIDATES]:
                if (kind, hit["id"]) in tried:
                    continue
                tried.add((kind, hit["id"]))
                detail = self.http.get_json(f"{API}/{kind}s/{hit['id']}")
                if detail and _has_track(detail, song):
                    return kind, detail
        return None

    # -- artist ---------------------------------------------------------------

    def _artist(self, artist_id: int) -> dict:
        data = self.http.get_json(f"{API}/artists/{artist_id}") or {}
        return {
            "id": artist_id,
            "name": artist_name(data.get("name") or ""),
            "profile": clean_profile(data.get("profile") or "")[:2000],
            "image": _primary_image(data.get("images")),
            "members": [artist_name(m.get("name") or "") for m in data.get("members") or [] if m.get("active")][:12],
            "urls": [u for u in (safe_url(u) for u in data.get("urls") or []) if u][:8],
            "url": safe_url(data.get("uri")),
        }


def _artist_matches(hit: dict, artist: str) -> bool:
    # Search titles read "Artist - Title"; the artist part may join several.
    credited = (hit.get("title") or "").split(" - ", 1)[0]
    names = [artist_name(n) for n in re.split(r"\s*(?:,|&|/| And | Feat\.? )\s*", credited)]
    return max([similarity(artist, artist_name(credited))] + [similarity(artist, n) for n in names]) >= MIN_ARTIST


def _rank(results: list, album_hint: str) -> list:
    def key(hit):
        title = (hit.get("title") or "").split(" - ", 1)[-1]
        formats = " ".join(hit.get("format") or []).lower()
        return (
            0 if album_hint and similarity(album_hint, title) >= 0.8 else 1,
            1 if any(w in formats for w in ("compilation", "unofficial", "promo", "sampler")) else 0,
            str(hit.get("year") or "9999"),
        )
    return sorted(results, key=key)


def _has_track(detail: dict, song: str) -> bool:
    targets = {song, clean_title(song)}
    for track in detail.get("tracklist") or []:
        title = track.get("title") or ""
        if any(similarity(t, title) >= MIN_TITLE for t in targets if t):
            return True
    return False


def _primary_image(images) -> str:
    images = images or []
    primary = next((i for i in images if i.get("type") == "primary"), images[0] if images else None)
    url = safe_url((primary or {}).get("uri"))
    return "" if url.endswith("spacer.gif") else url


def _release_info(kind: str, detail: dict, release: dict) -> dict:
    labels = []
    for label in release.get("labels") or []:
        name = artist_name(label.get("name") or "")
        if name and name not in labels:
            labels.append(name)
    formats = []
    for fmt in release.get("formats") or []:
        parts = [fmt.get("name") or ""] + (fmt.get("descriptions") or [])
        formats.append(", ".join(p for p in parts if p))
    community = release.get("community") or {}
    return {
        "kind": kind,
        "id": detail.get("id"),
        "title": detail.get("title") or release.get("title") or "",
        "year": detail.get("year") or release.get("year") or None,
        # Discogs pads unknown parts: "1983-00-00" means just "1983".
        "released": re.sub(r"(-00)+$", "", release.get("released") or ""),
        "country": release.get("country") or "",
        "labels": labels[:3],
        "formats": formats[:3],
        "genres": (detail.get("genres") or release.get("genres") or [])[:5],
        "styles": (detail.get("styles") or release.get("styles") or [])[:8],
        "tracklist": [
            {"position": t.get("position") or "", "title": t.get("title") or "", "duration": t.get("duration") or ""}
            for t in (detail.get("tracklist") or [])
            if t.get("type_", "track") == "track"
        ][:40],
        "have": community.get("have"),
        "want": community.get("want"),
        "url": safe_url(detail.get("uri")),
    }
