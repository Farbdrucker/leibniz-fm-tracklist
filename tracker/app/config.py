"""TOML config loading + env-var secret resolution.

Secrets never live in the TOML file. Each [[providers]] entry names a
`secrets_env_prefix` (defaults to its `type` upper-cased); the prefix is
used to resolve `<PREFIX>_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN`
env vars, falling back to a `_FILE` suffix for Docker-secret-style mounts.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TrackerSettings:
    stream_status_url: str
    poll_interval: float = 20.0
    db_path: str = "/data/tracklist.db"


@dataclass
class ApiSettings:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class ProviderSettings:
    type: str
    enabled: bool = True
    playlist_name_template: str = "Leibniz.fm {date}"
    playlist_description: str = ""
    sync_interval: float = 1800.0
    min_artist_similarity: float = 0.55
    min_title_similarity: float = 0.60
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    refresh_token_file: str = ""


@dataclass
class Settings:
    tracker: TrackerSettings
    api: ApiSettings = field(default_factory=ApiSettings)
    providers: list[ProviderSettings] = field(default_factory=list)


def _env_secret(prefix: str, name: str) -> str:
    direct = os.environ.get(f"{prefix}_{name}")
    if direct:
        return direct
    file_path = os.environ.get(f"{prefix}_{name}_FILE")
    if file_path and Path(file_path).exists():
        return Path(file_path).read_text().strip()
    return ""


def _load_provider(raw: dict) -> ProviderSettings:
    ptype = raw["type"]
    prefix = raw.get("secrets_env_prefix", ptype.upper())
    return ProviderSettings(
        type=ptype,
        enabled=raw.get("enabled", True),
        playlist_name_template=raw.get("playlist_name_template", "Leibniz.fm {date}"),
        playlist_description=raw.get("playlist_description", ""),
        sync_interval=raw.get("sync_interval", 1800.0),
        min_artist_similarity=raw.get("min_artist_similarity", 0.55),
        min_title_similarity=raw.get("min_title_similarity", 0.60),
        client_id=_env_secret(prefix, "CLIENT_ID"),
        client_secret=_env_secret(prefix, "CLIENT_SECRET"),
        refresh_token=_env_secret(prefix, "REFRESH_TOKEN"),
        refresh_token_file=os.environ.get(f"{prefix}_REFRESH_TOKEN_FILE", ""),
    )


def load(path: str | Path) -> Settings:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    tracker_raw = raw.get("tracker", {})
    tracker = TrackerSettings(
        stream_status_url=tracker_raw["stream_status_url"],
        poll_interval=tracker_raw.get("poll_interval", 20.0),
        db_path=tracker_raw.get("db_path", "/data/tracklist.db"),
    )

    api_raw = raw.get("api", {})
    api = ApiSettings(
        host=api_raw.get("host", "0.0.0.0"),
        port=api_raw.get("port", 8000),
    )

    providers = [_load_provider(p) for p in raw.get("providers", [])]

    return Settings(tracker=tracker, api=api, providers=providers)


def config_path() -> str:
    return os.environ.get("CONFIG_PATH", "./config.toml")
