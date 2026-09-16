"""Local dev entrypoint: `python -m app.cli [--tui]`.

Runs the same ingestion loop as the container's main.py, optionally with the
Rich terminal dashboard (requirements-dev.txt) for interactive use on a
machine with a terminal. Not part of the container's default CMD.
"""

from __future__ import annotations

import argparse
import time

from . import config, db
from .ingest import Poller


def main() -> None:
    parser = argparse.ArgumentParser(description="leibniz.fm tracker - local dev runner")
    parser.add_argument("--config", default=config.config_path(), help="path to config.toml")
    parser.add_argument("--tui", action="store_true", help="show the Rich live dashboard")
    args = parser.parse_args()

    settings = config.load(args.config)
    db.configure(settings.tracker.db_path)
    db.init_schema()

    poller = Poller(settings.tracker.stream_status_url, settings.tracker.poll_interval)
    poller.start()

    try:
        if args.tui:
            from .tui import run_tui
            run_tui(poller)
        else:
            print(f"polling {settings.tracker.stream_status_url} "
                  f"every {settings.tracker.poll_interval:.0f}s - Ctrl+C to stop")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()


if __name__ == "__main__":
    main()
