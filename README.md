# Leibniz.fm Tracklist

[Leibniz.FM e.V.](https://leibniz.fm/) ist ein Bürgerradio, siehe auch [#ueber-uns](https://leibniz.fm/ueber-uns/). 
Dieses Projekt ist aus reinem privaten Interesse und in keiner Kooperation mit Leibniz.fm e.V..

Mein Ziel war es eine Übersicht der gespielten Songs zu bekommen. 
Beim hören von leibniz.fm, habe ich mich erwischt wie ich drei mal den gleichen Song via shazam gesucht habe, [The Organ - Memorize The City](https://www.youtube.com/watch?v=5R0GRN8cJCU).

Unter [leibniz.fm/kontakt](https://leibniz.fm/kontakt/) steht geschrieben 

> 3. Wie heißt dieser tolle Song, der vorhin lief?
> Merke Dir die Uhrzeit und eine Textzeile – und schreibe uns an musik@leibniz.fm. Unsere Redaktion wird den Titel für Dich finden.

Da dachte ich, das müsste doch einfacher gehen. 

Leibniz.fm nutzt [Icecast](https://icecast.org/) freie server software um multimedia zu streamen. 
Icecast liefert neben dem `/stream` auch eine `status-json.xsl` ([hier](https://server3.streamserver-unlimited.de:10519/status-json.xsl)), eine `json` in der auch der `title` des aktuellen Tracks mitgegeben wird.

Dieses Projekt fragt alle `20` Sekunden die `status-json` ab und fügt den aktuellen Track einer Datenbank hinzu. Gleichzeitig gibt es einen minimalen Webserver der die Tracklist als Website darstellt.
Die Tracklist ist aktuell auf https://leibniz-fm.lukassanner.de/ zu erreichen.



## Install
```
# clone the repo
git clone https://github.com/Farbdrucker/leibniz-fm-tracklist.git

# navigate to the repo
cd leibniz-fm-tracklist

# copy the example .env and edit the domain for caddy
cp .env.example .env
vim .env # edit DOMAIN=localhost or your domain

# build the docker container
docker compose up -d

# navigate to the website, eg https://localhost/
open https://localhost/
```

## Einbetten (z.B. in WordPress)

Drei Widgets lassen sich auf einer fremden Seite einbinden — einfach in einen **Custom-HTML-Block**
kopieren, es wird nichts weiter installiert:

```html
<!-- „Es läuft gerade" — live vom Stream, mit Laufzeit und On-Air-Status -->
<script src="https://leibniz-fm.lukassanner.de/embed.js" data-lfm="live" async></script>

<!-- „Zuletzt gespielt" — kompakt, rein aus dem Archiv -->
<script src="https://leibniz-fm.lukassanner.de/embed.js" data-lfm="now" async></script>

<!-- Die Titel von heute -->
<script src="https://leibniz-fm.lukassanner.de/embed.js" data-lfm="today" async></script>
```

**`live` vs. `now`:** `now` zeigt den neuesten Eintrag aus dem Archiv — der sieht gleich aus, egal ob
der Song gerade läuft oder der Stream vor einer Stunde ausgefallen ist. `live` fragt zusätzlich den
Zustand des Streams ab: Sendet er gerade? Seit wann läuft der Titel? Fällt der Stream aus, steht dort
„Stream gerade offline" statt eines alten Titels, der so tut, als liefe er noch.

Die Widgets übernehmen Schrift und Farben der umgebenden Seite (Shadow DOM, alles in `em` und
`currentColor`), aktualisieren sich alle 30 Sekunden und pausieren, solange der Tab im Hintergrund ist.

| Attribut | Standard | Bedeutung |
|---|---|---|
| `data-lfm` | `now` | `live` = läuft gerade, `now` = zuletzt gespielt, `today` = Titel von heute |
| `data-limit` | `60` | maximale Anzahl Titel (nur `today`) |
| `data-interval` | `30` (`live`: `20`) | Sekunden zwischen zwei Abfragen |
| `data-stations` | `hide` | `show` zeigt auch Stationskennungen und Jingles |
| `data-listeners` | `hide` | `show` blendet bei `live` die Hörerzahl ein („42 hören zu") |
| `data-heading` | — | eigene Überschrift statt „Heute — …" |
| `data-target` | — | CSS-Selektor des Zielelements, falls das Widget woanders erscheinen soll |

Die Hörerzahl ist bewusst **standardmäßig aus** — ob die öffentlich sichtbar sein soll, ist eine
redaktionelle Entscheidung, keine technische.

Feinjustierung ist ohne Code möglich, über *Design → Customizer → Zusätzliches CSS*:

```css
.lfm-embed { --lfm-accent: #4ade80; --lfm-max-width: 36em; }
```

**Falls WordPress das `<script>` entfernt** (bei Nutzer:innen ohne `unfiltered_html` — typisch für
Multisite-Installationen oder wenn ein Security-Plugin dazwischenfunkt), gibt es einen iframe-Fallback.
Der übernimmt das Theme nicht, deshalb lassen sich Farben per Parameter mitgeben (Hex **ohne** `#`):

```html
<iframe src="https://leibniz-fm.lukassanner.de/embed.html?w=now&fg=222222&accent=e0245e"
        style="width:100%;height:150px;border:0" loading="lazy"
        title="Zuletzt auf leibniz.fm gespielt"></iframe>
```

Ob der Block Skripte durchlässt, klärt am schnellsten ein Test: `<script>document.write('OK')</script>`
in einen Custom-HTML-Block, Seite (privat) veröffentlichen und **im Frontend** ansehen — nicht im Editor,
dessen Vorschau läuft in einer Sandbox und ist kein verlässlicher Test.
