#!/usr/bin/env python3
"""
Radio-Tracker fuer Icecast-Streams (z.B. leibniz.fm).

Fragt regelmaessig die Icecast-Statusseite ab, erkennt Titelwechsel,
schreibt sie in eine CSV und zeigt alles live im Terminal an.

Benutzung:
    python radio_tracker.py
    python radio_tracker.py --interval 15 --csv meine_playlist.csv

Beenden mit Strg+C.
"""

from __future__ import annotations

import argparse
import csv
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

try:
    import spotify_client
except ImportError:          # Skript laeuft auch ohne Spotify-Modul
    spotify_client = None
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STATUS_URL = "https://server3.streamserver-unlimited.de:10519/status-json.xsl"
CSV_PATH = "playlist.csv"
INTERVAL = 20.0  # Sekunden zwischen zwei Abfragen
HISTORY = 12     # wie viele vergangene Titel angezeigt werden

BANNER = r"""
                                                                           
,--.   ,------.,--.,-----.  ,--.  ,--.,--.,-------.    ,------.,--.   ,--. 
|  |   |  .---'|  ||  |) /_ |  ,'.|  ||  |`--.   /     |  .---'|   `.'   | 
|  |   |  `--, |  ||  .-.  \|  |' '  ||  |  /   /      |  `--, |  |'.'|  | 
|  '--.|  `---.|  ||  '--' /|  | `   ||  | /   `--..--.|  |`   |  |   |  | 
`-----'`------'`--'`------' `--'  `--'`--'`-------''--'`--'    `--'   `--' 
                                                                           
"""


# --------------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------------

@dataclass
class Track:
    raw: str
    artist: str
    song: str
    started: datetime

    @property
    def elapsed(self) -> float:
        return (datetime.now() - self.started).total_seconds()


@dataclass
class State:
    """Alles, was der Poller-Thread schreibt und die Anzeige liest."""
    current: Track | None = None
    history: deque[Track] = field(default_factory=lambda: deque(maxlen=HISTORY))
    listeners: int | None = None
    listener_peak: int | None = None
    bitrate: int | None = None
    mount: str = ""
    stream_start: str = ""
    last_poll: datetime | None = None
    next_poll: datetime | None = None
    last_error: str = ""
    polls: int = 0
    errors: int = 0
    logged: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    # Spotify
    spotify_on: bool = False
    pending: list = field(default_factory=list)   # (datum, interpret, song)
    playlist: str = ""
    sp_added: int = 0
    sp_dupes: int = 0
    sp_missing: int = 0
    sp_last: datetime | None = None
    sp_next: float | None = None
    sp_error: str = ""
    sp_busy: bool = False


def split_title(raw: str) -> tuple[str, str]:
    """'Caesars - Boo Boo Goo Goo' -> ('Caesars', 'Boo Boo Goo Goo').

    Ohne Trenner (Moderation, Jingle, Sendungsname) bleibt der Interpret leer.
    """
    if " - " in raw:
        artist, song = raw.split(" - ", 1)
        return artist.strip(), song.strip()
    return "", raw.strip()


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:d}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# Abfrage
# --------------------------------------------------------------------------

def fetch_status(url: str, timeout: float = 10.0) -> dict:
    """Liefert die erste Quelle mit einem Titel, plus deren Metadaten.

    Icecast gibt 'source' als Liste zurueck, wenn mehrere Mounts aktiv sind,
    und als einzelnes Objekt, wenn es nur einen gibt - beides abfangen.
    """
    data = requests.get(url, timeout=timeout).json()
    sources = data.get("icestats", {}).get("source", [])
    if isinstance(sources, dict):
        sources = [sources]

    for src in sources:
        title = (src.get("title") or src.get("yp_currently_playing") or "").strip()
        if title:
            return {
                "title": title,
                "listeners": src.get("listeners"),
                "listener_peak": src.get("listener_peak"),
                "bitrate": src.get("bitrate"),
                "mount": src.get("listenurl", "").rsplit("/", 1)[-1],
                "stream_start": src.get("stream_start", ""),
            }
    return {}


def poller(state: State, url: str, interval: float,
           writer: csv.writer, fh, stop: threading.Event) -> None:
    """Laeuft im Hintergrund und aktualisiert den State."""
    while not stop.is_set():
        now = datetime.now()
        try:
            info = fetch_status(url)
            with state.lock:
                state.polls += 1
                state.last_poll = now
                state.last_error = ""
                if info:
                    state.listeners = info["listeners"]
                    state.listener_peak = info["listener_peak"]
                    state.bitrate = info["bitrate"]
                    state.mount = info["mount"]
                    state.stream_start = info["stream_start"]

                    raw = info["title"]
                    if state.current is None or raw != state.current.raw:
                        if state.current is not None:
                            state.history.appendleft(state.current)
                        artist, song = split_title(raw)
                        state.current = Track(raw, artist, song, now)
                        writer.writerow([now.isoformat(timespec="seconds"),
                                         artist, song, raw])
                        fh.flush()
                        state.logged += 1
                        if artist:
                            state.pending.append((now.date(), artist, song))
        except Exception as exc:
            with state.lock:
                state.polls += 1
                state.errors += 1
                state.last_poll = now
                state.last_error = f"{type(exc).__name__}: {exc}"

        with state.lock:
            state.next_poll = datetime.now().timestamp() + interval

        stop.wait(interval)


def spotify_worker(state: State, sync, interval: float, stop: threading.Event) -> None:
    """Schiebt alle `interval` Sekunden die gesammelten Titel zu Spotify.

    Laeuft bewusst in einem eigenen Thread: eine Runde kann bei vielen neuen
    Titeln eine Weile dauern, und solange soll weder die Abfrage des Streams
    noch die Anzeige stehenbleiben.
    """
    while not stop.is_set():
        with state.lock:
            state.sp_next = datetime.now().timestamp() + interval
        if stop.wait(interval):
            break

        with state.lock:
            batch = list(state.pending)
            state.pending.clear()
            state.sp_busy = True
        if not batch:
            with state.lock:
                state.sp_busy = False
            continue

        try:
            stats = sync.sync(batch)
            with state.lock:
                state.sp_added += stats["added"]
                state.sp_dupes += stats["duplicate"]
                state.sp_missing += stats["missing"]
                state.playlist = stats["playlist"] or state.playlist
                state.sp_last = datetime.now()
                state.sp_error = ""
        except Exception as exc:
            with state.lock:
                # Nicht verloren geben: beim naechsten Lauf erneut versuchen.
                state.pending[:0] = batch
                state.sp_error = f"{type(exc).__name__}: {exc}"
        finally:
            with state.lock:
                state.sp_busy = False


# --------------------------------------------------------------------------
# Anzeige
# --------------------------------------------------------------------------

def render_now_playing(state: State) -> Panel:
    if state.current is None:
        body = Align.center(Text("warte auf Daten ...", style="dim"), vertical="middle")
        return Panel(body, title="Jetzt laeuft", border_style="grey37", height=9)

    t = state.current
    artist = Text(t.artist or "ohne Interpret", style="bold cyan", justify="center")
    song = Text(t.song, style="bold white", justify="center")
    timer = Text(f"laeuft seit {mmss(t.elapsed)}", style="dim", justify="center")

    if not t.artist:
        # Vermutlich Moderation oder Jingle - optisch absetzen.
        artist = Text("- Wortbeitrag / Jingle -", style="yellow", justify="center")

    body = Group(Text(""), artist, song, Text(""), timer)
    return Panel(body, title="Jetzt laeuft", border_style="cyan", height=9)


def render_history(state: State) -> Panel:
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("Zeit", style="dim", width=8, no_wrap=True)
    table.add_column("Interpret", style="cyan", ratio=2, no_wrap=True)
    table.add_column("Titel", style="white", ratio=3, no_wrap=True)
    table.add_column("Dauer", style="dim", width=6, justify="right", no_wrap=True)

    if not state.history:
        table.add_row("", Text("noch nichts mitgeschnitten", style="dim"), "", "")

    for i, t in enumerate(state.history):
        end = state.history[i - 1].started if i > 0 else (
            state.current.started if state.current else datetime.now())
        table.add_row(
            t.started.strftime("%H:%M:%S"),
            t.artist or "-",
            t.song,
            mmss((end - t.started).total_seconds()),
        )

    return Panel(table, title=f"Verlauf (letzte {HISTORY})", border_style="grey37")


def render_footer(state: State, csv_path: str) -> Panel:
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_column(ratio=1)

    listeners = "?" if state.listeners is None else str(state.listeners)
    peak = "" if state.listener_peak is None else f" (max {state.listener_peak})"
    bitrate = "?" if state.bitrate is None else f"{state.bitrate} kbit/s"

    left = Text.assemble(("Hoerer: ", "dim"), (listeners, "bold green"), (peak, "dim"),
                         ("   Mount: ", "dim"), (state.mount or "?", "white"),
                         ("   ", ""), (bitrate, "white"))

    if state.next_poll:
        rest = max(0, state.next_poll - datetime.now().timestamp())
        mid = Text.assemble(("naechste Abfrage in ", "dim"), (f"{rest:4.0f}s", "white"))
    else:
        mid = Text("")
    mid.justify = "center"

    right = Text.assemble(("Titel: ", "dim"), (str(state.logged), "bold white"),
                          ("   Abfragen: ", "dim"), (str(state.polls), "white"),
                          ("   Fehler: ", "dim"),
                          (str(state.errors), "red" if state.errors else "dim"))
    right.justify = "right"

    table.add_row(left, mid, right)

    rows = [table]

    if state.spotify_on:
        if state.sp_busy:
            status, style = "synchronisiert ...", "yellow"
        elif state.sp_next:
            rest = max(0, state.sp_next - datetime.now().timestamp())
            status, style = f"naechster Sync in {mmss(rest)}", "dim"
        else:
            status, style = "wartet", "dim"
        rows.append(Text.assemble(
            ("Spotify: ", "bold green"),
            (state.playlist or "noch keine Playlist", "white"),
            ("   +", "dim"), (str(state.sp_added), "green"),
            ("   Dubletten: ", "dim"), (str(state.sp_dupes), "white"),
            ("   nicht gefunden: ", "dim"),
            (str(state.sp_missing), "yellow" if state.sp_missing else "white"),
            ("   offen: ", "dim"), (str(len(state.pending)), "white"),
            ("   ", ""), (status, style),
        ))
        if state.sp_error:
            rows.append(Text(f"Spotify: {state.sp_error}", style="red",
                             overflow="ellipsis", no_wrap=True))

    if state.last_error:
        rows.append(Text(state.last_error, style="red", overflow="ellipsis", no_wrap=True))
    rows.append(Text(f"-> {csv_path}    Strg+C zum Beenden", style="dim"))

    return Panel(Group(*rows), border_style="grey37")


def render_banner() -> Align:
    return Align.center(Text(BANNER.strip("\n"), style="bold cyan", no_wrap=True))


def render(state: State, csv_path: str) -> Layout:
    with state.lock:
        layout = Layout()
        layout.split_column(
            Layout(render_banner(), name="banner", size=7),
            Layout(render_now_playing(state), name="now", size=9),
            Layout(render_history(state), name="history"),
            Layout(render_footer(state, csv_path), name="footer", size=7),
        )
    return layout


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def resume_last_title(path: Path) -> str:
    """Letzten Rohtitel aus der CSV lesen, damit er nach einem Neustart
    nicht doppelt geschrieben wird."""
    if not path.exists():
        return ""
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        for row in reversed(rows):
            if len(row) >= 4:
                return row[3]
    except Exception:
        pass
    return ""


def todays_rows(path: Path) -> list[tuple[date, str, str]]:
    """Titel von heute aus der CSV holen - falls der Tracker schon lief,
    bevor Spotify eingeschaltet wurde. Dubletten faengt der Sync selbst ab."""
    if not path.exists():
        return []
    today = date.today()
    rows: list[tuple[date, str, str]] = []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 4 or row[0] == "zeit" or not row[1]:
                    continue
                stamp = datetime.fromisoformat(row[0])
                if stamp.date() == today:
                    rows.append((today, row[1], row[2]))
    except Exception:
        return []
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Icecast-Playlist mitschreiben")
    parser.add_argument("--url", default=STATUS_URL, help="Icecast status-json.xsl")
    parser.add_argument("--interval", type=float, default=INTERVAL,
                        help="Sekunden zwischen zwei Abfragen")
    parser.add_argument("--csv", default=CSV_PATH, help="Zieldatei")
    parser.add_argument("--spotify", action="store_true",
                        help="Titel in Spotify-Tagesplaylists uebertragen")
    parser.add_argument("--spotify-interval", type=float, default=1800.0,
                        help="Sekunden zwischen zwei Spotify-Laeufen (Standard 30 min)")
    parser.add_argument("--playlist-name", default="Leibniz.fm {date}",
                        help="Vorlage, {date} und {datum} werden ersetzt")
    parser.add_argument("--no-backfill", action="store_true",
                        help="beim Start die heutigen CSV-Zeilen nicht nachtragen")
    args = parser.parse_args()

    console = Console()
    path = Path(args.csv)
    is_new = not path.exists()

    state = State()
    previous = resume_last_title(path)
    if previous:
        artist, song = split_title(previous)
        state.current = Track(previous, artist, song, datetime.now())

    stop = threading.Event()

    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(["zeit", "interpret", "song", "roh"])
            fh.flush()

        thread = threading.Thread(target=poller, daemon=True,
                                  args=(state, args.url, args.interval, writer, fh, stop))
        thread.start()

        if args.spotify:
            if spotify_client is None:
                console.print("[red]spotify_client.py nicht gefunden.[/red]")
                return
            try:
                sync = spotify_client.PlaylistSync(
                    spotify_client.from_env(), name_template=args.playlist_name)
            except Exception as exc:
                console.print(f"[red]Spotify nicht startklar:[/red] {exc}")
                return
            state.spotify_on = True
            if not args.no_backfill:
                state.pending.extend(todays_rows(path))
            threading.Thread(target=spotify_worker, daemon=True,
                             args=(state, sync, args.spotify_interval, stop)).start()

        try:
            with Live(render(state, args.csv), console=console,
                      refresh_per_second=4, screen=True) as live:
                while True:
                    live.update(render(state, args.csv))
                    stop.wait(0.25)
        except KeyboardInterrupt:
            stop.set()

    console.print(f"[green]Fertig.[/green] {state.logged} Titel nach {args.csv} geschrieben.")


if __name__ == "__main__":
    main()
