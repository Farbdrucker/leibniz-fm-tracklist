"""MusicBrainz (recording, release, artist) + Cover Art Archive.

MusicBrainz is the backbone: its IDs are what link a song to Wikidata, and the
Cover Art Archive is keyed by them. It allows one request per second per
client and requires a descriptive User-Agent - both enforced in http.py.
"""

from __future__ import annotations

import re

from ..textmatch import clean_title, similarity
from .http import HttpClient, RateLimiter, safe_url

API = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"

MIN_ARTIST = 0.55
MIN_TITLE = 0.60

# Shared by every MusicBrainz client in the process: the 1 req/s limit is per IP.
_limiter = RateLimiter(1.1)
_caa_limiter = RateLimiter(0.2)

# Lower ranks first: prefer the song's studio album/single over the compilations,
# live albums and soundtracks it later turned up on.
_TYPE_RANK = {"Album": 0, "Single": 1, "EP": 2}


def _quote(text: str) -> str:
    """A Lucene phrase: inside quotes only backslash and quote need escaping."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _credit(credits: list) -> tuple[str, list[str]]:
    """artist-credit -> ('Simon & Garfunkel', ['Paul Simon', 'Art Garfunkel'])."""
    full = "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in credits)
    return full.strip(), [c.get("name") or "" for c in credits]


class MusicBrainz:
    def __init__(self, user_agent: str):
        self.http = HttpClient(user_agent, _limiter)
        self.caa = HttpClient(user_agent, _caa_limiter)

    def lookup(self, artist: str, song: str) -> dict | None:
        recording = self._find_recording(artist, song)
        if recording is None:
            return None

        credit_name, _ = _credit(recording.get("artist-credit") or [])
        release = _pick_release(recording.get("releases") or [])
        result: dict = {
            "recording": {
                "mbid": recording["id"],
                "title": recording.get("title") or song,
                "artist": credit_name,
                "length_ms": recording.get("length"),
                "first_release": recording.get("first-release-date") or "",
                "isrcs": (recording.get("isrcs") or [])[:5],
                "tags": _top_names(recording.get("tags")),
                "url": f"https://musicbrainz.org/recording/{recording['id']}",
            },
        }

        if release:
            group = release.get("release-group") or {}
            result["release"] = {
                "mbid": release.get("id"),
                "group_mbid": group.get("id"),
                "title": group.get("title") or release.get("title") or "",
                "type": group.get("primary-type") or "",
                "date": release.get("date") or "",
                "country": release.get("country") or "",
                "url": f"https://musicbrainz.org/release-group/{group['id']}" if group.get("id") else "",
            }
            result["cover"] = self._cover(group.get("id"), release.get("id"))

        artist_id = next((c["artist"]["id"] for c in recording.get("artist-credit") or []
                          if c.get("artist", {}).get("id")), None)
        if artist_id:
            result["artist"] = self._artist(artist_id)
        return result

    # -- recording --------------------------------------------------------------

    def _find_recording(self, artist: str, song: str) -> dict | None:
        variants = [(artist, song)]
        cleaned = (clean_title(artist), clean_title(song))
        if cleaned != variants[0] and all(cleaned):
            variants.append(cleaned)

        # Popular songs have dozens of live recordings that all score 100 and
        # crowd the studio version out of the result page, so ask without them
        # first. Only if that finds nothing, allow live-only songs.
        queries = [f"recording:{_quote(s)} AND artist:{_quote(a)} AND NOT comment:live" for a, s in variants]
        queries += [f"recording:{_quote(s)} AND artist:{_quote(a)}" for a, s in variants]
        for query in queries:
            data = self.http.get_json(f"{API}/recording", {"query": query, "limit": 25, "fmt": "json"}) or {}
            best = _best_recording(data.get("recordings") or [], artist, song)
            if best:
                return best
        return None

    # -- cover ----------------------------------------------------------------

    def _cover(self, group_mbid: str | None, release_mbid: str | None) -> dict | None:
        """The release group's front cover (CAA picks a representative release),
        falling back to the specific release. Stored as the stable CAA URL, which
        redirects to the current archive.org location."""
        for kind, mbid in (("release-group", group_mbid), ("release", release_mbid)):
            if not mbid:
                continue
            url = f"{CAA}/{kind}/{mbid}/front-500"
            if self.caa.exists(url):
                return {"url": url, "large": f"{CAA}/{kind}/{mbid}/front-1200",
                        "source": "Cover Art Archive",
                        "source_url": f"https://musicbrainz.org/{kind}/{mbid}"}
        return None

    # -- artist ---------------------------------------------------------------

    def _artist(self, mbid: str) -> dict:
        data = self.http.get_json(f"{API}/artist/{mbid}", {"inc": "url-rels genres", "fmt": "json"}) or {}
        links: dict[str, str] = {}
        for rel in data.get("relations") or []:
            url = safe_url((rel.get("url") or {}).get("resource"))
            kind = rel.get("type")
            if url and kind in ("wikidata", "official homepage", "discogs", "bandcamp") and kind not in links:
                links[kind] = url
        span = data.get("life-span") or {}
        return {
            "mbid": mbid,
            "name": data.get("name") or "",
            "type": data.get("type") or "",
            "disambiguation": data.get("disambiguation") or "",
            "country": data.get("country") or "",
            "area": (data.get("area") or {}).get("name") or "",
            "begin_area": (data.get("begin-area") or {}).get("name") or "",
            "begin": span.get("begin") or "",
            "end": span.get("end") or "",
            "ended": bool(span.get("ended")),
            "genres": _top_names(data.get("genres")),
            "links": links,
            "url": f"https://musicbrainz.org/artist/{mbid}",
        }


def _best_recording(recordings: list, artist: str, song: str) -> dict | None:
    """Plausible matches only (full-text search always returns *something*),
    then the one that appears on a proper studio album/single - not a radio
    broadcast, compilation or bootleg - and among those the earliest released.
    Only disambiguations naming a *different version* count against a
    recording: 'album version' is exactly the one we want."""
    candidates = []
    for rec in recordings:
        full, names = _credit(rec.get("artist-credit") or [])
        a_score = max([similarity(artist, full)] + [similarity(artist, n) for n in names])
        t_score = similarity(song, rec.get("title") or "")
        if t_score < MIN_TITLE:
            t_score = similarity(clean_title(song), rec.get("title") or "")
        if a_score < MIN_ARTIST or t_score < MIN_TITLE:
            continue
        best_release = _pick_release(rec.get("releases") or [])
        candidates.append((
            1 if _OTHER_VERSION.search(rec.get("disambiguation") or "") else 0,
            _release_rank(best_release) if best_release else (2, 9, 9),
            rec.get("first-release-date") or "9999",
            -(a_score + t_score),
            -int(rec.get("score") or 0),
            len(candidates),
            rec,
        ))
    if not candidates:
        return None
    # Only among the closest matches, so an old but merely similar title
    # ('Love Songs' for 'Love Song') can't beat the exact one.
    best = min(c[3] for c in candidates)
    return min(c for c in candidates if c[3] <= best + 0.15)[-1]


_OTHER_VERSION = re.compile(r"\b(live|demo|remix|mix|edit|acoustic|instrumental|karaoke|cover|session|unplugged|rehearsal)\b", re.I)


def _release_rank(release: dict) -> tuple:
    group = release.get("release-group") or {}
    return (
        0 if release.get("status") in (None, "Official") else 1,
        len(group.get("secondary-types") or []),
        _TYPE_RANK.get(group.get("primary-type"), 3),
    )


def _pick_release(releases: list) -> dict | None:
    if not releases:
        return None
    return min(releases, key=lambda r: _release_rank(r) + (r.get("date") or "9999",))


def _top_names(items, limit: int = 6) -> list[str]:
    items = sorted(items or [], key=lambda t: -int(t.get("count") or 0))
    return [t["name"] for t in items if t.get("name")][:limit]
