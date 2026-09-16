"""Provider factory: builds StreamingProvider instances from config.

Adding a new platform (Tidal, Deezer, ...) means writing one new module next
to spotify.py implementing the StreamingProvider protocol, then registering
its factory in _FACTORIES below plus a [[providers]] entry in config.toml.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import ProviderSettings, Settings
from .base import StreamingProvider
from .spotify import Spotify, SpotifyProvider

logger = logging.getLogger("tracker.providers")


def _build_spotify(cfg: ProviderSettings) -> StreamingProvider:
    token_file = Path(cfg.refresh_token_file) if cfg.refresh_token_file else None
    sp = Spotify(cfg.client_id, cfg.client_secret, cfg.refresh_token, token_file=token_file)
    return SpotifyProvider(sp, cfg.min_artist_similarity, cfg.min_title_similarity)


_FACTORIES = {
    "spotify": _build_spotify,
}


@dataclass
class ProviderRuntime:
    provider: StreamingProvider
    settings: ProviderSettings


def load_providers(settings: Settings) -> list[ProviderRuntime]:
    """Build every enabled, successfully-configured provider.

    A provider that fails to initialize (e.g. missing credentials) is
    logged and skipped rather than raised - ingestion must keep running
    with zero streaming providers configured, which is the default,
    fully supported way to run the tracker.
    """
    runtimes = []
    for cfg in settings.providers:
        if not cfg.enabled:
            continue
        factory = _FACTORIES.get(cfg.type)
        if factory is None:
            logger.error("unknown provider type %r, skipping", cfg.type)
            continue
        try:
            provider = factory(cfg)
        except Exception:
            logger.exception("failed to initialize provider %r, skipping", cfg.type)
            continue
        runtimes.append(ProviderRuntime(provider=provider, settings=cfg))
    return runtimes
