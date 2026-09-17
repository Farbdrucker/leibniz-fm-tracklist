"""Fuzzy text matching used for search verification and dedup keys.

Provider-agnostic: any StreamingProvider implementation can reuse these.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

_BRACKETS = re.compile(r"[\(\[].*?[\)\]]")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*", re.I)
_NOISE = re.compile(r"[^a-z0-9äöüß ]+")
_SLUG_TRANSLIT = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))
_SLUG_SEP = re.compile(r"[\W_]+")


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
    """Key for dedup checks, independent of spelling/case."""
    return f"{normalize(artist)}|{normalize(song)}"


def clean_title(text: str) -> str:
    """'Song (Radio Edit) feat. X' -> 'Song' - keeps case and punctuation,
    unlike normalize(), so it still works as a search query."""
    text = _FEAT.sub(" ", _BRACKETS.sub(" ", text))
    return " ".join(text.split()).strip(" -")


def slugify(text: str) -> str:
    """'Arctic Monkeys' -> 'arctic-monkeys', 'Die Ärzte' -> 'die-aerzte'.

    MUST stay identical to slugify() in web/app/static/index.html, which builds
    the /<artist>/<song> links the tracker resolves here. Letters of any script
    survive (a Japanese title keeps its characters), everything else collapses
    to a single '-'. If you change the rules, reset the stored slugs with
    `UPDATE tracks SET slug = NULL` - the metadata worker recomputes them.
    """
    text = unicodedata.normalize("NFC", text).lower()
    for src, dst in _SLUG_TRANSLIT:
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    return _SLUG_SEP.sub("-", text).strip("-") or "-"


def song_slug(artist: str, song: str) -> str:
    """Path of a song's page, without the leading slash. '' for rows without an
    artist (jingles, moderation), which have no page."""
    if not artist:
        return ""
    return f"{slugify(artist)}/{slugify(song)}"
