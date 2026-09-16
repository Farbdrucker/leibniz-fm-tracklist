"""Provider factory: builds StreamingProvider instances from config.

Adding a new platform (Tidal, Deezer, ...) means writing one new module next
to spotify.py implementing the StreamingProvider protocol, then registering
its factory in _FACTORIES below plus a [[providers]] entry in config.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import ProviderSettings, Settings
from .base import StreamingProvider
from .spotify import Spotify, SpotifyProvider


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
    runtimes = []
    for cfg in settings.providers:
        if not cfg.enabled:
            continue
        factory = _FACTORIES.get(cfg.type)
        if factory is None:
            raise ValueError(f"unknown provider type: {cfg.type!r}")
        runtimes.append(ProviderRuntime(provider=factory(cfg), settings=cfg))
    return runtimes
