"""Wikidata + Wikipedia: a German (else English) description and intro text
for the artist, plus a photo from Wikimedia Commons.

Reached via the Wikidata link on the artist's MusicBrainz entry, never by name
search - "Nirvana" by name is a coin toss, a QID isn't.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from .http import HttpClient, RateLimiter, safe_url

WIKIDATA = "https://www.wikidata.org/w/api.php"
LANGS = ("de", "en")

_limiter = RateLimiter(0.25)
_QID = re.compile(r"/(Q\d+)$")


class Wikimedia:
    def __init__(self, user_agent: str):
        self.http = HttpClient(user_agent, _limiter)

    def artist(self, wikidata_url: str) -> dict | None:
        match = _QID.search(wikidata_url or "")
        if not match:
            return None
        qid = match.group(1)
        data = self.http.get_json(WIKIDATA, {
            "action": "wbgetentities", "ids": qid, "format": "json",
            "props": "descriptions|claims|sitelinks/urls",
            "languages": "|".join(LANGS), "sitefilter": "|".join(f"{lang}wiki" for lang in LANGS),
        }) or {}
        entity = (data.get("entities") or {}).get(qid) or {}
        if not entity or "missing" in entity:
            return None

        descriptions = entity.get("descriptions") or {}
        claims = entity.get("claims") or {}
        result: dict = {
            "qid": qid,
            "url": f"https://www.wikidata.org/wiki/{qid}",
            "description": next((descriptions[lang]["value"] for lang in LANGS if lang in descriptions), ""),
            "website": safe_url(_claim(claims, "P856")),
        }

        image = _claim(claims, "P18")
        if isinstance(image, str) and image:
            name = image.replace(" ", "_")
            result["image"] = {
                "url": f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(name)}?width=600",
                "source_url": f"https://commons.wikimedia.org/wiki/File:{quote(name)}",
            }

        sitelinks = entity.get("sitelinks") or {}
        for lang in LANGS:
            link = sitelinks.get(f"{lang}wiki")
            if link and link.get("title"):
                summary = self._summary(lang, link["title"])
                if summary:
                    result["wikipedia"] = summary
                    break
        return result

    def _summary(self, lang: str, title: str) -> dict | None:
        data = self.http.get_json(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}"
        )
        if not data or data.get("type") == "disambiguation" or not data.get("extract"):
            return None
        return {
            "lang": lang,
            "title": data.get("title") or title,
            "extract": data["extract"][:1500],
            "url": safe_url(((data.get("content_urls") or {}).get("desktop") or {}).get("page"))
                   or f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
        }


def _claim(claims: dict, prop: str):
    """First value of a simple (string-typed) Wikidata statement."""
    for statement in claims.get(prop) or []:
        value = ((statement.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if value:
            return value
    return None
