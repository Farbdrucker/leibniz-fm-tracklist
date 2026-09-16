# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal (non-affiliated) archive of what the German community radio station [leibniz.fm](https://leibniz.fm/) plays. The station streams via Icecast, whose `status-json.xsl` endpoint exposes the currently playing `title`; this project polls it every 20s, stores every title change in SQLite, and serves the result as a browsable tracklist website. Deployed at https://leibniz-fm.lukassanner.de/.

UI copy, date formatting, and the timezone are all German — keep new user-facing strings in German.

## Commands

Everything is containerized, and **source is `COPY`'d into the images (no bind mounts for code)** — any Python or HTML change requires a rebuild, not just a restart:

```bash
docker compose up -d --build          # build + start the whole stack
docker compose up -d --build web      # after a UI-only change (web/app/static/index.html)
docker compose up -d --build tracker  # after a tracker change
docker compose logs -f tracker        # or: web, caddy
docker compose down
```

First-time setup (both files are gitignored; commit only the `.example` versions):

```bash
cp config.example.toml config.toml    # must exist as a FILE before first `up`, or Docker
                                      # creates it as a directory (config.load() detects this)
cp .env.example .env                  # then set DOMAIN=...
```

`DOMAIN=localhost` makes Caddy issue a cert from its own internal CA — the practical way to view the site locally at https://localhost (browsers show an untrusted-cert warning you click through). A real domain triggers Let's Encrypt and needs ports 80/443 publicly reachable.

`tracker` publishes no ports, so reach it only from inside the network. Neither image has `curl` installed, so use Python (as the Dockerfile healthchecks do):

```bash
docker compose exec tracker python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
curl -sk https://localhost/healthz    # web healthcheck, from the host
```

One-off tasks:

```bash
# Spotify refresh token — run on the host, never in a container (opens a browser)
SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... python scripts/spotify_login.py

# Import the legacy playlist.csv / spotify_cache.json into SQLite (run in the built image
# so the schema always matches). Full docs in tracker/app/migrate.py's docstring.
docker compose run --rm -v "$(pwd)":/legacy:ro tracker \
    python -m app.migrate --csv /legacy/playlist.csv --cache /legacy/spotify_cache.json --db /data/tracklist.db
```

Local (non-Docker) tracker dev, with the Rich terminal dashboard — runs the same poller as the container but without the provider-sync threads. Run from `tracker/` (the package is `app`), and point `--config` at a config whose `db_path` is writable locally (the default `/data/tracklist.db` is a container path):

```bash
pip install -r tracker/requirements-dev.txt
cd tracker && python -m app.cli --tui --config ../config.toml
```

**There is no test suite, linter, or build tooling** in this repo — no pytest, ruff, Makefile, or pyproject.toml, and no npm/bundler for the frontend. Verify changes by running the stack and exercising it.

## Architecture

Three services on a private Docker network (`internal`), where **only Caddy publishes ports**:

```
browser ──443──> caddy ──> web:8080 ──> tracker:8000 ──> SQLite (/data/tracklist.db)
                         (static UI +      (Icecast poller +     ▲
                          /api proxy)       provider sync) ──────┘
```

- **`tracker/`** — owns the data. A `Poller` thread (`ingest.py`) hits Icecast every `poll_interval`s and inserts a row only when the title *changes*; separate per-provider sync threads push tracks to streaming playlists. Both are started from the FastAPI `lifespan` in `main.py`. Its read API (`api.py`) is deliberately just `/health`, `/tracks`, and `/now`.
- **`web/`** — a thin shell: serves the static UI and proxies `/api/tracks` + `/api/now` to the tracker. The browser never talks to the tracker directly, which is why *the tracker* needs no CORS config and stays off any published port. `web` sets one CORS header, on `/api/now` only — see "Embedding" below.
- **`caddy/`** — TLS termination + reverse proxy, domain from `$DOMAIN`.

### Invariants that span multiple files

**Timestamps are naive local (Europe/Berlin) wall-clock, never UTC.** `ingest.py` writes `datetime.now().isoformat()`; the tracker Dockerfile installs `tzdata` and sets `TZ=Europe/Berlin` precisely because the slim base image otherwise silently defaults to UTC (this caused a real 2h offset bug). Correspondingly, the frontend parses timestamps with a regex into `new Date(y, m, d, h, min, s)` — **never** `new Date(isoString)`, which would apply a timezone shift. Breaking either side desynchronizes displayed times. `web/app/static/embed.js` carries the same `ISO_RE` parse, and additionally derives a track's age as `server_time - ts` (both naive Berlin, parsed identically) rather than against `Date.now()`, since its visitors are not necessarily in Germany. Note that the `web` image has **no** `tzdata`/`TZ` — it runs on UTC and must never generate or reformat a timestamp.

**All filtering, search, highlighting, and the calendar heatmap are client-side.** `/tracks` is an unfiltered bulk dump (`since_id`/`limit` only); the browser fetches everything once and does the rest in JS. This is intentional — that logic exists once, not in both Python and JS. Don't move it server-side without a reason.

`/now` is the one sanctioned carve-out, and it reimplements *none* of that logic — no search, no station detection, no aggregation. It answers only "the newest few rows" and "one indexed day", which the bulk dump can't do cheaply, because the embedded widgets run on a foreign page that must not re-download the whole archive every 30s per visitor. It also returns `day` and `server_time`, so the widget never derives "today" or a track's age from the *visitor's* clock. Adding a filter to `/now` would break the invariant; adding one to `/tracks` still would too.

**The entire UI is one dependency-free file**: `web/app/static/index.html` (~850 lines of HTML + CSS + ES5-style vanilla JS, no framework, no build step). Design tokens live as CSS custom properties in `:root`. Google Fonts is the only external resource.

**Tracks without a `" - "` separator have an empty artist** (`split_title()`) — these are jingles, moderation, or station IDs. They're excluded from provider sync at the SQL level (`tracks_for_sync`: `WHERE artist != ''`), and the UI treats them as "Stationskennungen" via its own `STATION_ARTISTS` list, hideable by a toggle.

### Embedding on leibniz.fm

`web/app/static/embed.js` is a second, independent frontend: two widgets (`data-lfm="now"` and
`data-lfm="today"`) that the station's WordPress site pastes into a Custom HTML block as a plain
`<script src>`. Constraints that shaped it, none of which apply to `index.html`:

- **It runs inside someone else's origin.** The DOM is built with `createElement`/`textContent`, never
  `innerHTML` — an escaping slip here would execute in a logged-in WP editor's session. Errors degrade to one
  quiet line plus a link; never surface a diagnostic (`index.html`'s `offlineNotice` prints `docker compose logs`).
- **It must inherit the host theme.** It renders into a shadow root, which lets inheritable CSS (`color`,
  `font-family`, `font-size`, `line-height`) through while keeping the theme's generic selectors out. Everything
  is sized in **`em`** — `rem` inside a shadow tree resolves against the *outer* document root — and coloured from
  `currentColor`/`color-mix`, so it works on a light or dark theme with no branching. `--lfm-accent`,
  `--lfm-max-width`, `--lfm-radius` pierce the boundary and can be set from WP's "Additional CSS".
- **CORS:** `Access-Control-Allow-Origin: *` is set as a plain header on `/api/now` only, not via
  `CORSMiddleware` (which would also cover `/api/tracks` and the static mount). `*` rather than an allowlist
  because ACAO is a browser policy, not access control — the data is public and unauthenticated, while an
  allowlist would add a `www`/non-`www` footgun and break Gutenberg's editor preview (`Origin: null`).
- **Load:** there is no shared cache in the stack, so `Cache-Control` is near-decorative. What protects SQLite is
  the ~10s memo + single-flight `asyncio.Lock` in `web/app/main.py`.
- `web/app/static/embed.html` is the iframe fallback for WordPress installs that strip `<script>` (users without
  `unfiltered_html`). It deliberately needs **zero JS in the host page** — so no `postMessage` auto-resize; the
  iframe gets a fixed height and this page scrolls internally.
- `/embed.js` keeps a **stable URL** (no `embed.v2.js`): the point of a hosted embed is shipping a fix without
  anyone reopening WordPress. The Caddyfile gives it a 5-minute `Cache-Control` so a bad deploy self-heals.

### Adding a streaming provider

`providers/base.py` defines a narrow `StreamingProvider` Protocol — a provider answers only three questions (`search_track`, `ensure_day_playlist`, `add_tracks`). Everything generic — day grouping, per-day dedup, the cross-day search cache, and the resume cursor — lives once in `sync_engine.SyncEngine`, backed by the `provider_playlists` / `provider_tracks` / `provider_search_cache` / `sync_state` tables. So a new platform is: one module next to `spotify.py`, one entry in `_FACTORIES` (`providers/__init__.py`), one `[[providers]]` block in `config.toml`.

A provider that fails to initialize is logged and skipped, never fatal — **running with zero providers is the default and fully supported mode**; ingestion must keep working regardless.

### Config and secrets

`config.py` loads non-secret settings from `config.toml` (bind-mounted read-only, path from `$CONFIG_PATH`). Secrets never appear in the TOML: each `[[providers]]` entry resolves `<PREFIX>_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` from the environment, where `PREFIX` defaults to the provider's `type` upper-cased (overridable via `secrets_env_prefix`), with a `_FILE` suffix fallback for Docker-secret-style mounts.

### SQLite concurrency

One file, three concurrent accessors (poller thread writing, sync thread reading+writing, FastAPI handlers reading), hence WAL mode and a 30s busy timeout. Callers open one connection per logical operation via the `db.connect()` context manager and pass the connection into the helpers, so a multi-step operation commits as a single transaction — notably each `SyncEngine.sync()` is all-or-nothing, so a crash mid-batch replays the whole batch next run.
