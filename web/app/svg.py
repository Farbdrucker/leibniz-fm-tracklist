"""Server-rendered SVG cards, for places that cannot run the JS widget.

GitHub strips <script>, <iframe>, <style> and every CSS attribute from README
markup, so embed.js can never run there. What survives the sanitizer is <img>,
and GitHub re-serves every image through its Camo proxy - which is exactly the
mechanism the familiar "now playing on Spotify" README cards use.

Consequences that shape this module:

  * No external resources. Camo fetches this one URL and nothing else, so an
    @font-face or a linked stylesheet would silently do nothing. Only generic
    font stacks (resolved from the *viewer's* local fonts) work.
  * No interactivity, no theme inheritance. Light/dark is handled by serving
    two URLs and letting GitHub's <picture media="..."> pick one.
  * Text must be fitted here, since SVG does not wrap or ellipsize. `_fit`
    estimates width from an average glyph advance - approximate by nature, but
    the alternative is shipping font metrics.
  * Everything interpolated is XML-escaped. This is machine-generated markup
    built from Icecast stream metadata, i.e. from outside the system.
"""

from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape

# (background, surface, text, muted, accent, rule)
THEMES = {
    "light": ("#ffffff", "#f6f8fa", "#1f2328", "#59636e", "#1a7f37", "#d1d9e0"),
    "dark": ("#0d1117", "#151b23", "#f0f6fc", "#9198a1", "#3fb950", "#3d444d"),
}

FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif"

# Average glyph advance as a fraction of font-size, for the stack above. Rough
# on purpose: it only has to stop long titles from running off the card.
_ADVANCE = {400: 0.52, 600: 0.56, 700: 0.58}


def _fit(text: str, max_px: float, size: float, weight: int = 400) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    per = size * _ADVANCE.get(weight, 0.52)
    limit = max(1, int(max_px / per))
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _parse(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except (TypeError, ValueError):
        return None


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return (f"{h}:{m:02d}:" if h else f"{m}:") + f"{s:02d}"


def _ago(seconds: float) -> str:
    mins = max(0, int(seconds // 60))
    if mins < 1:
        return "gerade eben"
    if mins == 1:
        return "vor 1 Minute"
    if mins < 60:
        return f"vor {mins} Minuten"
    hours = mins // 60
    if hours == 1:
        return "vor 1 Stunde"
    if hours < 24:
        return f"vor {hours} Stunden"
    days = hours // 24
    return "vor 1 Tag" if days == 1 else f"vor {days} Tagen"


def _text(x: float, y: float, content: str, size: float, fill: str,
          weight: int = 400, opacity: float = 1.0) -> str:
    if not content:
        return ""
    o = "" if opacity >= 1 else f' opacity="{opacity}"'
    return (
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}"{o}>{escape(content)}</text>'
    )


def card(data: dict, kind: str = "live", theme: str = "light",
         width: int = 480, listeners: bool = False) -> str:
    """Render one card. `data` is a /live or /now payload."""
    bg, surface, fg, muted, accent, rule = THEMES.get(theme, THEMES["light"])
    height = 104
    pad = 18
    inner = width - 2 * pad

    server_time = _parse(data.get("server_time"))

    if kind == "live":
        track = data.get("last") or {}
        on_air = bool(data.get("on_air"))
        since = _parse(data.get("since"))
        label = "Es läuft gerade" if on_air else "Stream gerade offline"
        eyebrow = label.upper()
        if on_air and since and server_time:
            stat = "läuft seit " + _clock((server_time - since).total_seconds())
            count = data.get("listeners")
            if listeners and isinstance(count, int):
                stat += " · " + ("1 hört zu" if count == 1 else f"{count} hören zu")
        elif track and server_time:
            played = _parse(track.get("t"))
            stat = "zuletzt " + _ago((server_time - played).total_seconds()) if played else ""
        else:
            stat = ""
        dot_fill, dot_op = (accent, 1.0) if on_air else (muted, 0.45)
    else:
        recent = data.get("recent") or []
        track = recent[0] if recent else {}
        label = "Zuletzt gespielt"
        eyebrow = label.upper()
        played = _parse(track.get("t"))
        stat = _ago((server_time - played).total_seconds()) if played and server_time else ""
        dot_fill, dot_op = accent, 1.0

    artist = _fit(track.get("a") or "", inner - 20, 16, 700)
    song = _fit(track.get("s") or track.get("r") or "Noch keine Titel aufgezeichnet.", inner - 20, 13.5)

    pulse = (
        '<animate attributeName="opacity" values="1;0.25;1" dur="2s" '
        'repeatCount="indefinite"/>' if kind == "live" and dot_op == 1.0 else ""
    )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label)}: {escape((track.get("r") or "").strip())}">',
        f'<rect width="{width}" height="{height}" rx="12" fill="{bg}"/>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="11.5" '
        f'fill="{surface}" stroke="{rule}"/>',
        f'<circle cx="{pad + 4}" cy="{pad + 9}" r="4" fill="{dot_fill}" '
        f'opacity="{dot_op}">{pulse}</circle>',
        _text(pad + 16, pad + 13, eyebrow, 9.5, muted),
        _text(pad, pad + 38, artist, 16, fg, 700),
        _text(pad, pad + 58, song, 13.5, fg, 400, 0.8),
        _text(pad, pad + 78, stat, 11, muted),
        "</svg>",
    ]
    return "".join(p for p in parts if p)
