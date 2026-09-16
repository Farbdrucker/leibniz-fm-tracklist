/* leibniz.fm tracklist - embeddable widgets for a foreign page (leibniz.fm).
 *
 *   <script src="https://leibniz-fm.lukassanner.de/embed.js" data-lfm="now" async></script>
 *   <script src="https://leibniz-fm.lukassanner.de/embed.js" data-lfm="today" async></script>
 *
 * Attributes: data-lfm="now|today", data-limit, data-interval (s), data-heading,
 *             data-stations="hide|show", data-target="#css-selector".
 *
 * Three rules this file exists to respect, all of them learned the hard way in
 * the main UI (static/index.html):
 *
 *  1. Timestamps from the API are *naive Europe/Berlin wall-clock*. They are
 *     parsed with a regex into new Date(y, m, d, ...) and never with
 *     new Date(isoString), which would shift them. "Today" is whatever the
 *     server says it is - never the visitor's own midnight.
 *  2. The DOM is built with createElement/textContent, not innerHTML. This code
 *     runs inside leibniz.fm's origin, where an escaping slip would execute in
 *     a logged-in editor's session; the track titles come from whatever the
 *     Icecast stream metadata happens to contain.
 *  3. Styling lives in a shadow root and is written in em/currentColor, so the
 *     widget inherits the host theme's typeface and colours. rem is avoided on
 *     purpose: inside a shadow tree it resolves against the *outer* document
 *     root, so a theme with html{font-size:62.5%} would shrink the widget.
 */
(function () {
  "use strict";

  if (!window.fetch || !document.createElement("div").attachShadow) return;

  /* ---------------- this instance ---------------- */

  // Must be read synchronously: document.currentScript is null inside callbacks,
  // and optimiser plugins (Rocket Loader & co.) can null it out entirely.
  var SELF = document.currentScript || (function () {
    var all = document.querySelectorAll("script[data-lfm]");
    for (var i = 0; i < all.length; i++) {
      if (!all[i].hasAttribute("data-lfm-init")) return all[i];
    }
    return null;
  })();
  if (!SELF || !SELF.src) return;
  SELF.setAttribute("data-lfm-init", "1");

  var BASE;
  try {
    BASE = new URL(SELF.src, location.href).origin;
  } catch (e) {
    return;
  }

  function attr(name, fallback) {
    var v = SELF.getAttribute(name);
    return v === null || v === "" ? fallback : v;
  }
  function num(name, fallback, min, max) {
    var v = parseInt(SELF.getAttribute(name), 10);
    if (isNaN(v) || v < min || v > max) return fallback;
    return v;
  }

  var KIND = attr("data-lfm", "now").toLowerCase() === "today" ? "today" : "now";
  var LIMIT = num("data-limit", 60, 1, 500);
  // Station IDs are filtered client-side (the server has no notion of them), so
  // ask for headroom and slice to LIMIT after filtering - otherwise data-limit
  // would silently deliver far fewer rows than requested.
  var FETCH_LIMIT = KIND === "today" ? Math.min(500, LIMIT * 2 + 20) : 1;
  var INTERVAL = num("data-interval", 30, 10, 3600) * 1000;
  var HIDE_STATIONS = attr("data-stations", "hide") !== "show";
  var HEADING = attr("data-heading", null);
  var TARGET = attr("data-target", null);

  // Past this, "Zuletzt gespielt" would be a lie - the poller may be down, or
  // the station simply off air overnight.
  var STALE_MINUTES = 180;
  var STATION_ARTISTS = ["leibniz.fm", "ndr"];

  /* ---------------- shared across every embed on the page ---------------- */

  var SHARED = window.__lfmEmbed || (window.__lfmEmbed = { cache: {}, inflight: {} });
  var MEMO_MS = 10000;

  function fetchNow() {
    var today = KIND === "today" ? 1 : 0;
    var key = today + "|" + FETCH_LIMIT;
    var hit = SHARED.cache[key];
    if (hit && Date.now() - hit.at < MEMO_MS) return Promise.resolve(hit.data);
    if (SHARED.inflight[key]) return SHARED.inflight[key];

    var ctrl = window.AbortController ? new AbortController() : null;
    var timeout = setTimeout(function () { if (ctrl) ctrl.abort(); }, 8000);

    var p = fetch(BASE + "/api/now?today=" + today + "&limit=" + FETCH_LIMIT, {
      signal: ctrl ? ctrl.signal : undefined,
      credentials: "omit"
    }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    }).then(function (data) {
      clearTimeout(timeout);
      delete SHARED.inflight[key];
      SHARED.cache[key] = { at: Date.now(), data: data };
      return data;
    }, function (err) {
      clearTimeout(timeout);
      delete SHARED.inflight[key];
      throw err;
    });

    SHARED.inflight[key] = p;
    return p;
  }

  /* ---------------- API payload -> track objects ---------------- */

  var ISO_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/;

  function parseNaive(stamp) {
    var m = ISO_RE.exec(String(stamp == null ? "" : stamp).trim());
    if (!m) return null;
    return {
      date: new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]),
      day: m[1] + "-" + m[2] + "-" + m[3],
      time: m[4] + ":" + m[5]
    };
  }

  function toTrack(it) {
    var p = it && parseNaive(it.t);
    if (!p) return null;
    var artist = String(it.a == null ? "" : it.a).trim();
    var song = String(it.s == null ? "" : it.s).trim();
    var raw = String(it.r == null ? "" : it.r).trim();
    return {
      id: it.id,
      day: p.day,
      date: p.date,
      time: p.time,
      artist: artist,
      song: song || raw,
      raw: raw || (artist ? artist + " - " + song : song),
      isStation: artist === "" || STATION_ARTISTS.indexOf(artist.toLowerCase()) !== -1
    };
  }

  function toTracks(items) {
    var out = [];
    for (var i = 0; i < (items || []).length; i++) {
      var t = toTrack(items[i]);
      if (t) out.push(t);
    }
    return out;
  }

  /* ---------------- German formatting ---------------- */

  var fmtToday = new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "numeric", month: "long" });
  var fmtShort = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit" });

  function dayDate(key) {
    var p = String(key || "").split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }
  function keyOf(d) {
    var m = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
  }
  function prevKey(key) {
    var d = dayDate(key);
    d.setDate(d.getDate() - 1);
    return keyOf(d);
  }

  // Age is server_time minus the track's ts - both naive Berlin, parsed the same
  // way - so the visitor's own clock and timezone never enter into it.
  function ageMinutes(track, data) {
    var srv = parseNaive(data && data.server_time);
    if (!srv || !track) return null;
    return Math.max(0, Math.round((srv.date - track.date) / 60000));
  }

  function agoLabel(mins) {
    if (mins === null) return "";
    if (mins < 1) return "gerade eben";
    if (mins === 1) return "vor 1 Minute";
    if (mins < 60) return "vor " + mins + " Minuten";
    var h = Math.floor(mins / 60);
    if (h === 1) return "vor 1 Stunde";
    if (h < 24) return "vor " + h + " Stunden";
    return "";
  }

  function whenLabel(track, data) {
    if (track.day === data.day) return track.time;
    if (track.day === prevKey(data.day)) return "Gestern " + track.time;
    return fmtShort.format(dayDate(track.day)) + " " + track.time;
  }

  /* ---------------- DOM building ---------------- */

  function elm(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  var SP_PATH = "M12 2a10 10 0 100 20 10 10 0 000-20zm4.586 14.424a.623.623 0 01-.857.207c-2.348-1.435-5.304-1.76-8.785-.964a.623.623 0 11-.277-1.215c3.809-.871 7.077-.496 9.713 1.115a.623.623 0 01.206.857zm1.223-2.722a.78.78 0 01-1.072.257c-2.687-1.652-6.785-2.131-9.965-1.166a.78.78 0 11-.452-1.492c3.632-1.102 8.147-.568 11.232 1.329a.78.78 0 01.257 1.072zm.105-2.835c-3.223-1.914-8.54-2.09-11.617-1.156a.935.935 0 11-.542-1.79c3.532-1.072 9.404-.865 13.115 1.338a.935.935 0 11-.956 1.608z";

  function spotifyLink(t) {
    var q = t.artist ? t.artist + " " + t.song : t.raw;
    var a = elm("a", "sp");
    a.href = "https://open.spotify.com/search/" + encodeURIComponent(q);
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = "Auf Spotify suchen";
    a.setAttribute("aria-label", "Auf Spotify suchen: " + t.raw);
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", SP_PATH);
    svg.appendChild(path);
    a.appendChild(svg);
    return a;
  }

  function trackRow(t, data) {
    var row = elm("div", "track" + (t.isStation ? " station" : ""));
    row.appendChild(elm("div", "time", whenLabel(t, data)));
    var meta = elm("div", "meta");
    if (t.artist) meta.appendChild(elm("div", "artist", t.artist));
    meta.appendChild(elm("div", "song", t.song));
    row.appendChild(meta);
    // No Spotify search for jingles/station IDs - it would never find anything.
    if (!t.isStation) row.appendChild(spotifyLink(t));
    return row;
  }

  function archiveLink(text) {
    var p = elm("p", "foot");
    var a = elm("a", null, text);
    a.href = BASE + "/";
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    p.appendChild(a);
    return p;
  }

  /* ---------------- the two widgets ---------------- */

  function renderNow(root, data) {
    var recent = toTracks(data.recent);
    var current = null;
    for (var i = 0; i < recent.length; i++) {
      if (!HIDE_STATIONS || !recent[i].isStation) { current = recent[i]; break; }
    }
    if (!current) current = recent[0] || null;

    if (!current) {
      root.appendChild(elm("p", "muted", "Noch keine Titel aufgezeichnet."));
      root.appendChild(archiveLink("Zum Playlist-Archiv →"));
      return;
    }

    var mins = ageMinutes(current, data);
    var stale = mins !== null && mins >= STALE_MINUTES;

    var box = elm("div", "now");
    box.appendChild(elm("div", "badge", whenLabel(current, data)));

    var titles = elm("div", "titles");
    var ago = agoLabel(mins);
    var eyebrow = stale
      ? (ago ? "Gerade läuft nichts · zuletzt " + ago : "Gerade läuft nichts")
      : (ago ? "Zuletzt gespielt · " + ago : "Zuletzt gespielt");
    titles.appendChild(elm("p", "eyebrow", eyebrow));
    if (current.artist) titles.appendChild(elm("div", "artist", current.artist));
    titles.appendChild(elm("div", "song", current.song));
    box.appendChild(titles);
    if (!current.isStation) box.appendChild(spotifyLink(current));

    root.appendChild(box);
    root.appendChild(archiveLink("Ganzes Archiv ansehen →"));
  }

  function renderToday(root, data) {
    var rows = toTracks(data.today);
    if (HIDE_STATIONS) {
      rows = rows.filter(function (t) { return !t.isStation; });
    }
    rows.reverse(); // newest first - the interesting end for a visitor
    if (rows.length > LIMIT) rows = rows.slice(0, LIMIT);

    var head = elm("div", "head");
    head.appendChild(elm("h2", null, HEADING || "Heute — " + fmtToday.format(dayDate(data.day))));
    if (rows.length) {
      head.appendChild(elm("span", "count", rows.length + " Titel"));
    }
    root.appendChild(head);

    if (!rows.length) {
      root.appendChild(elm("p", "muted", "Heute läuft noch nichts Neues im Archiv — schau später wieder vorbei."));
      root.appendChild(archiveLink("Zum Playlist-Archiv →"));
      return;
    }

    var list = elm("div", "rows");
    for (var i = 0; i < rows.length; i++) list.appendChild(trackRow(rows[i], data));
    root.appendChild(list);

    var total = data.today_total;
    root.appendChild(archiveLink(
      total > rows.length ? "Alle " + total + " Titel ansehen →" : "Ganzes Archiv ansehen →"
    ));
  }

  /* ---------------- styles ---------------- */

  var CSS = [
    ":host{display:block;font:inherit;color:inherit}",
    "*{box-sizing:border-box}",
    ".lfm{font:inherit;color:inherit;line-height:1.45;max-width:var(--lfm-max-width,42em)}",
    ".head{display:flex;align-items:baseline;justify-content:space-between;gap:.6em;flex-wrap:wrap;margin:0 0 .6em}",
    ".head h2{margin:0;font:inherit;font-size:1.15em;font-weight:600}",
    ".count{font-size:.8em;opacity:.65;white-space:nowrap}",
    ".now{display:flex;gap:.8em;align-items:center;padding:.1em 0 .2em}",
    ".badge{flex:none;font-size:.78em;font-weight:600;padding:.35em .7em;border-radius:var(--lfm-radius,999px);",
    "  color:var(--lfm-accent,currentColor);background:rgba(128,128,128,.16);",
    "  background:color-mix(in srgb,var(--lfm-accent,currentColor) 14%,transparent);",
    "  font-variant-numeric:tabular-nums;white-space:nowrap}",
    ".titles{min-width:0;flex:1 1 auto}",
    ".eyebrow{margin:0 0 .15em;font-size:.72em;letter-spacing:.06em;text-transform:uppercase;opacity:.6}",
    ".now .artist{font-weight:700;font-size:1.15em;line-height:1.2;overflow-wrap:anywhere}",
    ".now .song{font-size:1em;opacity:.8;overflow-wrap:anywhere}",
    ".track{display:grid;grid-template-columns:4.2em minmax(0,1fr) auto;gap:.1em .8em;align-items:center;",
    "  padding:.5em 0;border-top:1px solid rgba(128,128,128,.28);",
    "  border-top:1px solid color-mix(in srgb,currentColor 16%,transparent)}",
    ".track .time{font-size:.8em;opacity:.6;font-variant-numeric:tabular-nums}",
    ".track .meta{min-width:0}",
    ".track .artist{font-weight:600;font-size:.94em;overflow-wrap:anywhere}",
    ".track .song{font-size:.88em;opacity:.75;overflow-wrap:anywhere}",
    ".track.station .artist,.track.station .song{opacity:.5;font-style:italic}",
    ".sp{flex:none;width:1.95em;height:1.95em;border-radius:999px;display:inline-flex;align-items:center;",
    "  justify-content:center;color:inherit;opacity:.45;text-decoration:none;",
    "  border:1px solid rgba(128,128,128,.35);",
    "  border:1px solid color-mix(in srgb,currentColor 24%,transparent);",
    "  transition:opacity .15s,color .15s}",
    ".sp:hover,.sp:focus-visible{opacity:1;color:var(--lfm-accent,currentColor)}",
    ".sp svg{width:.95em;height:.95em;display:block}",
    ".foot{margin:.7em 0 0;font-size:.8em;opacity:.75}",
    ".foot a{color:inherit}",
    ".muted{opacity:.7;font-size:.92em;margin:.2em 0}",
    "@media (max-width:26em){.track{grid-template-columns:3.6em minmax(0,1fr) auto}}"
  ].join("\n");

  /* ---------------- mount ---------------- */

  var host = document.createElement("div");
  host.className = "lfm-embed";
  host.setAttribute("lang", "de");

  var anchor = TARGET ? document.querySelector(TARGET) : null;
  if (anchor) anchor.appendChild(host);
  else if (SELF.parentNode) SELF.parentNode.insertBefore(host, SELF);
  else return;

  var shadow = host.attachShadow({ mode: "open" });
  var style = document.createElement("style");
  style.textContent = CSS;
  shadow.appendChild(style);
  var root = elm("div", "lfm");
  shadow.appendChild(root);

  /* ---------------- polling ---------------- */

  var timer = null, failures = 0, lastSig = null;

  function signature(data) {
    var top = (data.recent && data.recent[0] && data.recent[0].id) || 0;
    return top + ":" + data.day + ":" + (data.today_total || 0) +
           ":" + ((data.today && data.today.length) || 0);
  }

  function draw(data) {
    while (root.firstChild) root.removeChild(root.firstChild);
    (KIND === "today" ? renderToday : renderNow)(root, data);
  }

  function drawOffline() {
    while (root.firstChild) root.removeChild(root.firstChild);
    // Deliberately no diagnostics: this renders inside someone else's page.
    root.appendChild(elm("p", "muted", "Die Playlist ist gerade nicht erreichbar."));
    root.appendChild(archiveLink("Zum Playlist-Archiv →"));
  }

  function schedule(ms) {
    if (timer) { clearTimeout(timer); timer = null; }
    if (document.hidden) return; // resumes via visibilitychange
    timer = setTimeout(tick, ms);
  }

  function tick() {
    timer = null;
    fetchNow().then(function (data) {
      failures = 0;
      var sig = signature(data);
      // Nothing new: leave the DOM (and the reader's scroll position) alone.
      if (sig !== lastSig) { lastSig = sig; draw(data); }
      schedule(INTERVAL);
    }, function () {
      failures++;
      if (lastSig === null) drawOffline();
      schedule(Math.min(300000, INTERVAL * Math.pow(2, Math.min(failures, 4))));
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) schedule(0);
    else if (timer) { clearTimeout(timer); timer = null; }
  });
  window.addEventListener("pagehide", function () {
    if (timer) { clearTimeout(timer); timer = null; }
  });

  tick();
})();
