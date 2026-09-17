# Die Daten selbst nutzen

Wer nicht nur auf die Website schauen, sondern die Titel selbst weiterverarbeiten will —
eigenes Widget, Skript, Bot, Auswertung — hat zwei Wege:

1. **Über diese Instanz** (`https://leibniz-fm.lukassanner.de`) — liefert das *Archiv*:
   alles, was seit Beginn der Aufzeichnung lief, mit Zeitstempeln. Wie weit es zurückreicht,
   verrät die älteste Zeile: `curl -s ".../api/tracks?limit=1"`.
2. **Direkt von Icecast** (`status-json.xsl`) — liefert nur das *Jetzt*: den Titel, der in
   genau diesem Moment auf dem Stream steht. Kein Umweg über meinen Server, keine
   Abhängigkeit davon, dass es diese Instanz in zwei Jahren noch gibt.

Fertige Widgets zum Einbinden (Skript, iframe, Bild) stehen in
[docs/embedding.md](embedding.md) — die hier beschriebenen Endpunkte sind die Stufe darunter.

---

## 1. Die HTTP-API dieser Instanz

Öffentlich, ohne Schlüssel, nur lesend. Basis-URL: `https://leibniz-fm.lukassanner.de`.

| Endpunkt | Zweck | CORS |
|---|---|---|
| `GET /api/tracks` | kompletter Archiv-Dump, seitenweise über `since_id` | nein |
| `GET /api/now` | die neuesten Titel + die von heute | `*` |
| `GET /api/live` | läuft der Stream gerade, und seit wann? | `*` |
| `GET /embed.svg` | dieselbe Karte als Bild (siehe embedding.md) | `*` |
| `GET /healthz` | Health-Check des Webservers | — |

**Nur `/api/now`, `/api/live` und `/embed.svg` schicken `Access-Control-Allow-Origin: *`.**
`/api/tracks` bewusst nicht: der Bulk-Dump ist für die eigene Website und für Skripte
gedacht, nicht dafür, dass fremde Seiten bei jedem Aufruf das ganze Archiv ziehen. Aus
einem Browser auf einer fremden Domain ist er deshalb nicht direkt abrufbar — aus
`curl`, Python, einem Server oder einer App schon (CORS ist eine Browser-Regel, keine
Zugriffskontrolle).

### `/api/tracks` — das Archiv

```bash
curl -s "https://leibniz-fm.lukassanner.de/api/tracks?limit=2" | jq
```

```json
{
  "tracks": [
    {"id": 1, "t": "2026-09-16T08:56:54", "a": "Ron Sexsmith", "s": "West Gwillimbury",
     "r": "Ron Sexsmith - West Gwillimbury"},
    {"id": 2, "t": "2026-09-16T08:58:15", "a": "El Michels Affair",
     "s": "Indifference feat. Shintaro Sakamoto",
     "r": "El Michels Affair - Indifference feat. Shintaro Sakamoto"}
  ],
  "next_since_id": 2
}
```

Die Schlüssel sind einbuchstabig, weil der Dump groß wird (~300 Zeilen pro Tag):
`id`, `t` = Zeitstempel, `a` = Artist, `s` = Song, `r` = der rohe Icecast-Titel.

| Parameter | Standard | Bedeutung |
|---|---|---|
| `since_id` | `0` | nur Zeilen mit `id > since_id` |
| `limit` | — | maximale Anzahl Zeilen (ältere zuerst) |

Für einen eigenen Mitschnitt also: einmal alles holen, danach nur noch Nachschub.
`next_since_id` ist der Cursor für die nächste Runde:

```python
import json, time, urllib.request

BASE = "https://leibniz-fm.lukassanner.de"
since = 0
while True:
    with urllib.request.urlopen(f"{BASE}/api/tracks?since_id={since}", timeout=15) as r:
        data = json.load(r)
    for tr in data["tracks"]:
        print(tr["t"], tr["r"])
    since = data["next_since_id"]
    time.sleep(60)          # das Archiv wächst im Minutentakt, öfter lohnt nicht
```

### `/api/now` — die neuesten Titel

```bash
curl -s "https://leibniz-fm.lukassanner.de/api/now?today=1&limit=60" | jq
```

```json
{
  "day": "2026-09-17",
  "server_time": "2026-09-17T10:03:59",
  "recent": [
    {"id": 329, "t": "2026-09-17T10:00:27", "a": "NDR", "s": "Weltnachrichten",
     "r": "NDR - Weltnachrichten"},
    {"id": 328, "t": "2026-09-17T09:58:46", "a": "Blur", "s": "Globe Alone",
     "r": "Blur - Globe Alone"}
  ],
  "today": [ {"id": 328, "t": "2026-09-17T09:58:46", "a": "Blur", "s": "…", "r": "…"} ],
  "today_total": 174
}
```

`recent` sind die zehn neuesten Titel, absteigend, **nicht** auf heute begrenzt (sonst wäre
das Widget um 00:05 Uhr leer). `today` ist der laufende Tag, chronologisch, auf `limit`
gekappt; `today_total` nennt die tatsächliche Anzahl. `today=0` spart die Tagesliste.

`day` und `server_time` kommen **vom Server**, weil nur der Tracker auf Europe/Berlin läuft.
Wer „heute" oder „vor wie vielen Minuten" aus der Uhr der Besucher:innen ableitet, liegt
außerhalb Deutschlands daneben.

### `/api/live` — läuft gerade etwas?

```json
{
  "server_time": "2026-09-17T10:03:46",
  "on_air": true,
  "listeners": 23,
  "since": "2026-09-17T10:00:27",
  "last": {"id": 329, "t": "2026-09-17T10:00:27", "a": "NDR", "s": "Weltnachrichten",
           "r": "NDR - Weltnachrichten"}
}
```

Der einzige Endpunkt, der nicht aus der Datenbank kommt: Zeilen werden nur geschrieben,
wenn sich der Titel *ändert* — die neueste Zeile sieht also gleich aus, ob der Song gerade
läuft oder der Stream vor einer Stunde ausgefallen ist. `on_air` beantwortet genau diesen
Unterschied und ist `false`, sobald die letzte erfolgreiche Icecast-Abfrage länger als drei
Poll-Intervalle zurückliegt. `since` ist der Beginn des aktuellen Titels (für ein „läuft
seit 2:28"); `listeners` kann `null` sein.

### Spielregeln

- Das ist ein privates Hobbyprojekt auf einem kleinen Server, ohne Cache davor. `/api/now`
  und `/api/live` werden intern 10 bzw. 5 Sekunden gemerkt; häufiger zu fragen bringt also
  nichts. **Alle 20–30 Sekunden reicht** — schneller als der Poller selbst (20 s) kann sich
  ohnehin nichts ändern. Für `/api/tracks` gilt: einmal voll, danach inkrementell.
- Keine Authentifizierung, kein Rate-Limit, keine Zusage auf Verfügbarkeit oder stabile
  Feldnamen. Wer sich darauf verlassen muss, hostet besser selbst (siehe unten).

### Zeitstempel: der eine Fallstrick

`t`, `ts`, `server_time` und `since` sind **naive lokale Zeit (Europe/Berlin)** — ohne `Z`,
ohne Offset. In JavaScript ist

```js
new Date("2026-09-17T14:22:08")   // FALSCH: wird je nach Browser als UTC/lokal gedeutet
```

die Quelle für Anzeigen, die um zwei Stunden danebenliegen. Richtig ist, die Zeichenkette
zu zerlegen und die Felder direkt zu setzen — genau das tun `index.html` und `embed.js`:

```js
var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(s);
var d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
```

In Python ist `datetime.fromisoformat(s)` korrekt — das Ergebnis ist naiv und meint Berliner
Wandzeit. Für ein Alter („vor 3 Minuten") `server_time` aus derselben Antwort als Gegenwart
nehmen, nicht die eigene Uhr.

### Artist, Song, Jingle

Icecast liefert eine einzige Zeichenkette. Getrennt wird am **ersten `" - "`**:
`"Ron Sexsmith - West Gwillimbury"` → Artist `Ron Sexsmith`, Song `West Gwillimbury`.
Fehlt der Trenner, bleibt `a` leer — Jingles und Moderation ohne Interpret landen so.

Damit ist es aber nicht getan: die häufigsten Nicht-Songs haben sehr wohl einen Trenner und
sehen aus wie reguläre Titel —

```
Leibniz.fm - Du verdienst mehr als Mainstream!     (Stationskennung, mehrmals pro Stunde)
NDR - Weltnachrichten                              (Nachrichtenübernahme)
```

Die Website filtert sie deshalb über eine kleine Liste bekannter „Artists" heraus
(`STATION_ARTISTS = ["leibniz.fm", "ndr"]` in `index.html`), umschaltbar über den Schalter
„Stationskennungen ausblenden". Wer selbst auswertet, braucht etwas Ähnliches — sonst ist der
meistgespielte „Künstler" des Archivs der Sender selbst. Der rohe Titel steht immer in `r`.

---

## 2. Direkt von Icecast, ohne diese Instanz

Leibniz.fm streamt über [Icecast](https://icecast.org/). Icecast veröffentlicht neben dem
Stream einen Status als JSON — dieselbe Quelle, aus der sich dieses Projekt speist:

| | |
|---|---|
| Status | `https://server3.streamserver-unlimited.de:10519/status-json.xsl` |
| Stream | `https://server3.streamserver-unlimited.de:10519/stream` |

```bash
curl -s "https://server3.streamserver-unlimited.de:10519/status-json.xsl" | jq
```

Die Antwort sieht dort heute so aus (gekürzt, Stand 17.09.2026):

```json
{
  "icestats": {
    "admin": "icemaster@localhost",
    "host": "server3.streamserver-unlimited.de",
    "server_id": "Icecast 2.4.4 with AdBreak 1.1.0.2",
    "server_start_iso8601": "2026-08-06T09:50:13+0200",
    "source": {
      "bitrate": 320,
      "listeners": 23,
      "listener_peak": 34,
      "listenurl": "http://server3.streamserver-unlimited.de:10510/live",
      "server_type": "audio/mpeg",
      "stream_start_iso8601": "2026-09-16T23:58:23+0200",
      "title": "NDR - Weltnachrichten"
    }
  }
}
```

Nur der aktuelle Titel — **keine Historie**. Wer eine Liste will, muss selbst mitschreiben.

Sechs Dinge, über die man dabei stolpert:

- **`source` ist mal ein Objekt, mal eine Liste.** Bei genau einem aktiven Mount — wie oben —
  liefert Icecast ein Objekt, bei mehreren eine Liste. Beides behandeln, sonst bricht der
  Code genau dann, wenn die Station einen zweiten Mount aufmacht.
- **`title` kann fehlen oder leer sein** (Moderation, Umschaltpause, kein Encoder verbunden).
  Ein leerer Titel heißt „gerade nichts", nicht „Fehler". Andere Icecast-Installationen
  liefern zusätzlich `yp_currently_playing`; diese hier tut es nicht, ein Fallback darauf
  schadet aber nicht.
- **Die vorhandenen Zeitstempel meinen nicht den Titel.** `stream_start_iso8601` ist der
  Moment, in dem sich der *Encoder* verbunden hat, `server_start_iso8601` der Start des
  Icecast-Prozesses. Wann der laufende Song begonnen hat, steht **nirgends** — das weiß nur,
  wer gepollt hat. Der Zeitpunkt der eigenen Abfrage ist die beste Näherung, bei 20 Sekunden
  Intervall also auf ±20 s genau.
- **Titelwechsel selbst erkennen.** Wer jede Abfrage speichert, bekommt denselben Song
  dutzendfach. Die ganze Logik ist ein Vergleich mit dem letzten Wert (siehe unten).
- **`listenurl` ist nicht die Stream-URL, die man selbst benutzt.** Sie zeigt hier auf
  `http://…:10510/live` — den internen Mount. Zum Hören nimmt die Station selbst
  `https://server3.streamserver-unlimited.de:10519/stream`.
- **Aus dem Browser heraus geht das nicht.** Der Server schickt keinen
  `Access-Control-Allow-Origin`-Header (nachgeprüft mit einem `Origin`-Header im Request),
  also blockiert der Browser jedes `fetch()` von einer fremden Domain. Es braucht einen
  eigenen kleinen Proxy — oder eben `/api/now` von oben, das genau dafür `*` schickt. Dazu
  kommt der ungewöhnliche Port `10519`, den manche Firmen- und Uni-Netze dichtmachen.

  Nachprüfen lässt sich das so — **nicht** mit `curl -I`, auf `HEAD` antwortet dieser
  Icecast mit `400 Bad Request`:

  ```bash
  curl -s -o /dev/null -D - -H "Origin: https://example.com" \
    "https://server3.streamserver-unlimited.de:10519/status-json.xsl" | grep -i access-control
  ```

### Minimaler Mitschnitt, ohne Abhängigkeiten

Das Wesentliche dieses Projekts in 30 Zeilen — abfragen, Wechsel erkennen, Zeile anhängen:

```python
#!/usr/bin/env python3
"""Schreibt jeden Titelwechsel auf leibniz.fm nach tracklist.csv."""
import csv, json, time, urllib.request
from datetime import datetime

URL = "https://server3.streamserver-unlimited.de:10519/status-json.xsl"
INTERVAL = 20.0

def current_title():
    with urllib.request.urlopen(URL, timeout=10) as r:
        sources = json.load(r).get("icestats", {}).get("source", [])
    if isinstance(sources, dict):          # ein Mount -> Objekt statt Liste
        sources = [sources]
    for src in sources:
        title = (src.get("title") or src.get("yp_currently_playing") or "").strip()
        if title:
            return title
    return ""

def split(title):
    """'Ron Sexsmith - West Gwillimbury' -> Tupel; ohne Trenner: ('', title)."""
    if " - " not in title:                 # Jingle, Moderation, Stationskennung
        return "", title
    artist, song = title.split(" - ", 1)
    return artist.strip(), song.strip()

last = ""
with open("tracklist.csv", "a", newline="", encoding="utf-8") as fh:
    out = csv.writer(fh)
    while True:
        try:
            title = current_title()
            if title and title != last:    # nur Wechsel, nicht jede Abfrage
                artist, song = split(title)
                out.writerow([datetime.now().isoformat(timespec="seconds"),
                              artist, song, title])
                fh.flush()
                last = title
        except Exception as exc:           # Netz weg, Server weg, kaputtes JSON
            print("Abfrage fehlgeschlagen:", exc)
        time.sleep(INTERVAL)
```

Für „was läuft jetzt?" auf der Kommandozeile reicht auch:

```bash
curl -s "https://server3.streamserver-unlimited.de:10519/status-json.xsl" \
  | jq -r '[.icestats.source] | flatten | .[] | select(.title != null) | .title' | head -1
```

Ein Wort zum Takt: 20 Sekunden ist die Einstellung dieses Projekts und ein fairer
Kompromiss — kurz genug, dass kein Song untergeht (der kürzeste Jingle dauert länger),
lang genug, dass es niemandem wehtut. Es ist der Server einer fremden Radiostation;
im Sekundentakt zu pollen bringt keine besseren Daten, nur Last.

### Oder: eine eigene Instanz

Wer das komplette Ding will — Datenbank, Website, Kalender, Suche, optionale
Spotify-Playlists — startet es in drei Befehlen selbst; die Anleitung steht in der
[README](../README.md). Die Stream-Quelle ist nicht eingebrannt, sondern steht in
`config.toml`:

```toml
[tracker]
stream_status_url = "https://server3.streamserver-unlimited.de:10519/status-json.xsl"
poll_interval = 20.0
```

Damit lässt sich derselbe Aufbau auf jede andere Icecast-Station richten.

---

## Und die Rechtslage?

Dieses Projekt ist privat und steht in keiner Verbindung zu Leibniz.fm e.V. Es liest einen
öffentlich zugänglichen Statusendpunkt und speichert, was dort steht: Uhrzeit, Interpret,
Titel. Das ist eine Notiz darüber, was im Radio lief — kein Mitschnitt der Musik. Wer damit
etwas Eigenes baut, sollte es genauso halten: freundlich pollen, die Quelle nennen, und
keine Audiodaten mitschneiden.
