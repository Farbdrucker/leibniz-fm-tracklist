"""Rich terminal dashboard - local dev tool only, never used by the container.

Run via `python -m app.cli --tui`. Reads live poll stats from the Poller
instance and recent history from SQLite; the poller thread it displays is
the same one used for ingestion, so there is no second polling loop.
"""

from __future__ import annotations

import time
from datetime import datetime

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import db
from .ingest import Poller

HISTORY = 12

BANNER = r"""

,--.   ,------.,--.,-----.  ,--.  ,--.,--.,-------.    ,------.,--.   ,--.
|  |   |  .---'|  ||  |) /_ |  ,'.|  ||  |`--.   /     |  .---'|   `.'   |
|  |   |  `--, |  ||  .-.  \|  |' '  ||  |  /   /      |  `--, |  |'.'|  |
|  '--.|  `---.|  ||  '--' /|  | `   ||  | /   `--..--.|  |`   |  |   |  |
`-----'`------'`--'`------' `--'  `--'`--'`-------''--'`--'    `--'   `--'

"""


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:d}:{seconds % 60:02d}"


def render_banner() -> Align:
    return Align.center(Text(BANNER.strip("\n"), style="bold cyan", no_wrap=True))


def render_now_playing(current) -> Panel:
    if current is None:
        body = Align.center(Text("waiting for data ...", style="dim"), vertical="middle")
        return Panel(body, title="Now playing", border_style="grey37", height=9)

    started = datetime.fromisoformat(current["ts"])
    elapsed = (datetime.now() - started).total_seconds()
    artist = Text(current["artist"] or "no artist", style="bold cyan", justify="center")
    song = Text(current["song"], style="bold white", justify="center")
    timer = Text(f"playing for {mmss(elapsed)}", style="dim", justify="center")

    if not current["artist"]:
        artist = Text("- announcement / jingle -", style="yellow", justify="center")

    body = Group(Text(""), artist, song, Text(""), timer)
    return Panel(body, title="Now playing", border_style="cyan", height=9)


def render_history(history, current) -> Panel:
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("Time", style="dim", width=8, no_wrap=True)
    table.add_column("Artist", style="cyan", ratio=2, no_wrap=True)
    table.add_column("Title", style="white", ratio=3, no_wrap=True)
    table.add_column("Duration", style="dim", width=8, justify="right", no_wrap=True)

    if not history:
        table.add_row("", Text("nothing recorded yet", style="dim"), "", "")

    for i, row in enumerate(history):
        started = datetime.fromisoformat(row["ts"])
        if i > 0:
            end = datetime.fromisoformat(history[i - 1]["ts"])
        elif current is not None:
            end = datetime.fromisoformat(current["ts"])
        else:
            end = datetime.now()
        table.add_row(
            started.strftime("%H:%M:%S"),
            row["artist"] or "-",
            row["song"],
            mmss((end - started).total_seconds()),
        )

    return Panel(table, title=f"History (last {HISTORY})", border_style="grey37")


def render_footer(poller: Poller, total: int) -> Panel:
    with poller.lock:
        listeners = "?" if poller.listeners is None else str(poller.listeners)
        bitrate = "?" if poller.bitrate is None else f"{poller.bitrate} kbit/s"
        mount = poller.mount or "?"
        polls, errors, logged = poller.polls, poller.errors, poller.logged
        last_error = poller.last_error
        next_poll = poller.next_poll

    left = Text.assemble(("Listeners: ", "dim"), (listeners, "bold green"),
                         ("   Mount: ", "dim"), (mount, "white"),
                         ("   ", ""), (bitrate, "white"))

    if next_poll:
        rest = max(0, next_poll - datetime.now().timestamp())
        mid = Text.assemble(("next poll in ", "dim"), (f"{rest:4.0f}s", "white"))
    else:
        mid = Text("")
    mid.justify = "center"

    right = Text.assemble(("New: ", "dim"), (str(logged), "bold white"),
                          ("   Total: ", "dim"), (str(total), "white"),
                          ("   Polls: ", "dim"), (str(polls), "white"),
                          ("   Errors: ", "dim"),
                          (str(errors), "red" if errors else "dim"))
    right.justify = "right"

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(left, mid, right)

    rows = [grid]
    if last_error:
        rows.append(Text(last_error, style="red", overflow="ellipsis", no_wrap=True))
    rows.append(Text("local dev mode - Ctrl+C to stop", style="dim"))

    return Panel(Group(*rows), border_style="grey37")


def render(poller: Poller) -> Layout:
    with db.connect() as conn:
        rows = db.recent_tracks(conn, HISTORY + 1)
        total = db.track_count(conn)

    current = rows[0] if rows else None
    history = rows[1:] if len(rows) > 1 else []

    layout = Layout()
    layout.split_column(
        Layout(render_banner(), name="banner", size=7),
        Layout(render_now_playing(current), name="now", size=9),
        Layout(render_history(history, current), name="history"),
        Layout(render_footer(poller, total), name="footer", size=6),
    )
    return layout


def run_tui(poller: Poller) -> None:
    console = Console()
    with Live(render(poller), console=console, refresh_per_second=4, screen=True) as live:
        try:
            while True:
                live.update(render(poller))
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
