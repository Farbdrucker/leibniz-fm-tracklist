"""FastAPI app: wires the Icecast poller and provider-sync workers into
background threads on startup, and exposes the read API (see api.py).

No CLI flags here - the containerized service is configured entirely via
config.toml + env-var secrets (see config.py). For an interactive local dev
session with the Rich TUI, use `python -m app.cli --tui` instead.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config, db
from .api import router
from .ingest import Poller
from .metadata import Enricher, MetadataWorker
from .providers import load_providers
from .sync_engine import SyncEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tracker.main")

_stop_sync = threading.Event()
_sync_threads: list[threading.Thread] = []
_poller: Poller | None = None
_metadata: MetadataWorker | None = None


def _sync_loop(engine: SyncEngine, interval: float, name: str) -> None:
    while not _stop_sync.wait(interval):
        try:
            stats = engine.sync()
            if stats.added or stats.duplicate or stats.missing:
                logger.info("%s sync: +%d added, %d duplicate, %d missing (%s)",
                            name, stats.added, stats.duplicate, stats.missing, stats.playlist)
        except Exception:
            logger.exception("%s sync failed", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller, _metadata
    settings = config.load(config.config_path())
    db.configure(settings.tracker.db_path)
    db.init_schema()

    _poller = Poller(settings.tracker.stream_status_url, settings.tracker.poll_interval)
    _poller.start()
    # /live reads the poller's in-memory state through here (see api.live).
    app.state.poller = _poller
    logger.info("poller started: %s every %.0fs",
                settings.tracker.stream_status_url, settings.tracker.poll_interval)

    # Same rule as providers: song info is a nice-to-have, so a broken setup is
    # logged and the song pages simply go without it.
    enricher = None
    if settings.metadata.enabled:
        try:
            enricher = Enricher(settings.metadata)
            logger.info("song metadata enabled (discogs: %s)", "on" if enricher.discogs else "off")
        except Exception:
            logger.exception("failed to initialize song metadata, continuing without")
    app.state.enricher = enricher
    _metadata = MetadataWorker(enricher)
    _metadata.start()

    for runtime in load_providers(settings):
        engine = SyncEngine(runtime.provider, runtime.settings.playlist_name_template,
                            runtime.settings.playlist_description)
        thread = threading.Thread(
            target=_sync_loop, daemon=True, name=f"sync-{runtime.settings.type}",
            args=(engine, runtime.settings.sync_interval, runtime.settings.type),
        )
        thread.start()
        _sync_threads.append(thread)
        logger.info("%s sync enabled, every %.0fs",
                    runtime.settings.type, runtime.settings.sync_interval)

    yield

    _poller.stop()
    _metadata.stop()
    _stop_sync.set()
    for thread in _sync_threads:
        thread.join(timeout=5)


app = FastAPI(title="leibniz.fm tracker", lifespan=lifespan)
app.include_router(router)
