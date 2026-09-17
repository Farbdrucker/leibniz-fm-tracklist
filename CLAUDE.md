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

- **`tracker/`** — owns the data. A `Poller` thread (`ingest.py`) hits Icecast every `poll_interval`s and inserts a row only when the title *changes*; separate per-provider sync threads push tracks to streaming playlists; a `MetadataWorker` (`metadata/`) slugs new rows and fetches song info. All are started from the FastAPI `lifespan` in `main.py`. Its read API (`api.py`) is deliberately just `/health`, `/tracks`, `/now`, `/live`, and `/songs/{artist}/{song}`.
- **`web/`** — a thin shell: serves the static UI (plus `song.html` for every `/{artist}/{song}` path) and proxies `/api/tracks`, `/api/now`, `/api/live`, `/api/songs/...` to the tracker. The browser never talks to the tracker directly, which is why *the tracker* needs no CORS config and stays off any published port. `web` sets one CORS header, on `/api/now` only — see "Embedding" below.
- **`caddy/`** — TLS termination + reverse proxy, domain from `$DOMAIN`.

### Invariants that span multiple files

**Timestamps are naive local (Europe/Berlin) wall-clock, never UTC.** `ingest.py` writes `datetime.now().isoformat()`; the tracker Dockerfile installs `tzdata` and sets `TZ=Europe/Berlin` precisely because the slim base image otherwise silently defaults to UTC (this caused a real 2h offset bug). Correspondingly, the frontend parses timestamps with a regex into `new Date(y, m, d, h, min, s)` — **never** `new Date(isoString)`, which would apply a timezone shift. Breaking either side desynchronizes displayed times. `web/app/static/embed.js` carries the same `ISO_RE` parse, and additionally derives a track's age as `server_time - ts` (both naive Berlin, parsed identically) rather than against `Date.now()`, since its visitors are not necessarily in Germany. Note that the `web` image has **no** `tzdata`/`TZ` — it runs on UTC and must never generate or reformat a timestamp.

**All filtering, search, highlighting, and the calendar heatmap are client-side.** `/tracks` is an unfiltered bulk dump (`since_id`/`limit` only); the browser fetches everything once and does the rest in JS. This is intentional — that logic exists once, not in both Python and JS. Don't move it server-side without a reason.

`/now` is the one sanctioned carve-out, and it reimplements *none* of that logic — no search, no station detection, no aggregation. It answers only "the newest few rows" and "one indexed day", which the bulk dump can't do cheaply, because the embedded widgets run on a foreign page that must not re-download the whole archive every 30s per visitor. It also returns `day` and `server_time`, so the widget never derives "today" or a track's age from the *visitor's* clock. Adding a filter to `/now` would break the invariant; adding one to `/tracks` still would too.

**The UI is dependency-free static files**: `web/app/static/index.html` (the archive) and `song.html` (song pages) — HTML + CSS + ES5-style vanilla JS, no framework, no build step. Design tokens live as CSS custom properties in `:root`, duplicated in both files. Google Fonts is the only external resource besides the cover/artist images on song pages.

**Tracks without a `" - "` separator have an empty artist** (`split_title()`) — these are jingles, moderation, or station IDs. They're excluded from provider sync at the SQL level (`tracks_for_sync`: `WHERE artist != ''`), and the UI treats them as "Stationskennungen" via its own `STATION_ARTISTS` list, hideable by a toggle.

### Song pages and metadata

Every track with an artist has a page at `/<artist-slug>/<song-slug>` (e.g. `/arctic-monkeys/fluorescent-adolescent`).

- **The slug rule exists twice**: `slugify()` in `tracker/app/textmatch.py` and in `web/app/static/index.html` (a copy in `song.html` only highlights the tracklist). The page builds links in JS, the tracker resolves them against `tracks.slug` — change one without the other and links 404. `song_slug()` returns `''` for rows without an artist. After changing the rule, `UPDATE tracks SET slug = NULL`; the worker recomputes.
- **`tracks.slug` is filled by `MetadataWorker`, not by `ingest.py`** (and `/songs` calls `db.assign_slugs()` before every lookup, so a just-started song never 404s). The column was added by an in-place migration in `db.init_schema()`.
- **Sources** (`tracker/app/metadata/`): MusicBrainz + Cover Art Archive (backbone; its artist entry links to Wikidata), Discogs (only with `DISCOGS_CONSUMER_KEY/_SECRET`; label, format, tracklist, videos), Wikidata + Wikipedia (German first). Each is isolated in `Enricher.lookup`; `merge()` produces the one display-ready document `song.html` renders. Rate limiters are module-level, shared by worker and on-demand threads (MusicBrainz: 1 req/s per IP).
- **Cache** is `song_metadata` (JSON per slug) with a refresh policy per status (`REFRESH_AFTER`). An all-error refetch never overwrites earlier `found`/`partial` data. `Enricher.ensure` single-flights per slug.
- **The worker does not crawl the archive**: on first start its cursor (`sync_state` row `metadata`) begins 10 rows back; older songs are fetched on demand when a page is opened. `index.html` fetches info only for the newest track (the "Zuletzt gespielt" hero).
- `/songs` is the second sanctioned server-side lookup next to `/now`: an indexed identity lookup ("plays of this song"), no filtering. `web`'s proxy for it is **not** memoized — `_memoized`'s global lock would stall the embeds behind a multi-second first fetch.
- `song.html` renders third-party data: DOM via `createElement`/`textContent` only, links/images only if `http(s)` (also enforced server-side by `safe_url`). Discogs requires the visible "Data provided by Discogs" credit; Wikipedia text is CC BY-SA — both in the page footer/source lines.

### Embedding on leibniz.fm

`web/app/static/embed.js` is a second, independent frontend: three widgets (`data-lfm="live"`,
`"now"`, `"today"`) that the station's WordPress site pastes into a Custom HTML block as a plain
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
  the memo + single-flight `asyncio.Lock` in `web/app/main.py` (10s for `/now`, 5s for `/live`).
- **`/live` is the only endpoint not backed by SQLite.** The `tracks` table records title *changes*, so the
  newest row is identical whether the song is playing or the stream died an hour ago — "is it on air right now"
  exists solely in the running `Poller`'s memory (`snapshot()`). Note `snapshot()` includes `last_error` and poll
  counters; `api.live` strips them, and anything else exposing it publicly must too. The `live` widget's ticking
  "läuft seit" takes its *base* from the server (`server_time - since`) and only the delta since the fetch from
  the visitor's clock — a duration, so no timezone is involved. The listener count is opt-in
  (`data-listeners="show"`), because whether to publish it is an editorial call, not a technical one.
- `web/app/static/embed.html` is the iframe fallback for WordPress installs that strip `<script>` (users without
  `unfiltered_html`). It deliberately needs **zero JS in the host page** — so no `postMessage` auto-resize; the
  iframe gets a fixed height and this page scrolls internally.
- `/embed.js` keeps a **stable URL** (no `embed.v2.js`): the point of a hosted embed is shipping a fix without
  anyone reopening WordPress. The Caddyfile gives it a 5-minute `Cache-Control` so a bad deploy self-heals.
- `web/app/svg.py` + `GET /embed.svg` render the same card server-side as an **image**, for hosts that sanitize
  markup so hard the widget can never run — GitHub READMEs strip `<script>`, `<iframe>`, `<style>` and all CSS.
  It is a third rendering path and will drift from `embed.js` unless both are changed together; that is the price
  of the medium, not an oversight. Constraints it inherits: GitHub re-serves it through the Camo proxy, which
  fetches only this one URL (so no `@font-face`, no linked CSS — generic font stacks only) and caches hard (so
  the card is "recent", never live, whatever `Cache-Control` says). Light/dark is two URLs behind GitHub's
  `<picture media="...">`, not inheritance. SVG is XML built from Icecast metadata, so everything interpolated
  goes through `escape()`; `_fit()` ellipsizes by estimated glyph advance because SVG neither wraps nor clips.

### Adding a streaming provider

`providers/base.py` defines a narrow `StreamingProvider` Protocol — a provider answers only three questions (`search_track`, `ensure_day_playlist`, `add_tracks`). Everything generic — day grouping, per-day dedup, the cross-day search cache, and the resume cursor — lives once in `sync_engine.SyncEngine`, backed by the `provider_playlists` / `provider_tracks` / `provider_search_cache` / `sync_state` tables. So a new platform is: one module next to `spotify.py`, one entry in `_FACTORIES` (`providers/__init__.py`), one `[[providers]]` block in `config.toml`.

A provider that fails to initialize is logged and skipped, never fatal — **running with zero providers is the default and fully supported mode**; ingestion must keep working regardless.

### Config and secrets

`config.py` loads non-secret settings from `config.toml` (bind-mounted read-only, path from `$CONFIG_PATH`). Secrets never appear in the TOML: each `[[providers]]` entry resolves `<PREFIX>_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` from the environment, where `PREFIX` defaults to the provider's `type` upper-cased (overridable via `secrets_env_prefix`), with a `_FILE` suffix fallback for Docker-secret-style mounts.

### SQLite concurrency

One file, three concurrent accessors (poller thread writing, sync thread reading+writing, FastAPI handlers reading), hence WAL mode and a 30s busy timeout. Callers open one connection per logical operation via the `db.connect()` context manager and pass the connection into the helpers, so a multi-step operation commits as a single transaction — notably each `SyncEngine.sync()` is all-or-nothing, so a crash mid-batch replays the whole batch next run.
