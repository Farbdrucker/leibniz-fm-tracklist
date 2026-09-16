"""Fuzzy text matching used for search verification and dedup keys.

Provider-agnostic: any StreamingProvider implementation can reuse these.
"""

from __future__ import annotations

import difflib
import re

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
    """Key for dedup checks, independent of spelling/case."""
    return f"{normalize(artist)}|{normalize(song)}"
