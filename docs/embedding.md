# Widgets einbetten

Die Tracklist lässt sich auf fremden Seiten einbinden — als Skript-Widget (WordPress & Co.),
als iframe-Fallback oder als serverseitig gerendertes Bild (GitHub-README).

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

## In einer GitHub-README

GitHub entfernt in READMEs `<script>`, `<iframe>`, `<style>` und jedes CSS — das JS-Widget kann dort
also nicht laufen. Bilder überleben die Filterung, deshalb gibt es `/embed.svg`: dieselbe Karte,
serverseitig als SVG gerendert. GitHub liefert sie über seinen Camo-Proxy aus.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://leibniz-fm.lukassanner.de/embed.svg?w=live&amp;theme=dark">
  <img alt="Was gerade auf leibniz.fm läuft" src="https://leibniz-fm.lukassanner.de/embed.svg?w=live&amp;theme=light">
</picture>

```html
<picture>
  <source media="(prefers-color-scheme: dark)"
          srcset="https://leibniz-fm.lukassanner.de/embed.svg?w=live&amp;theme=dark">
  <img alt="Was gerade auf leibniz.fm läuft"
       src="https://leibniz-fm.lukassanner.de/embed.svg?w=live&amp;theme=light">
</picture>
```

Parameter: `w=live|now`, `theme=light|dark`, `width=` (280–900), `listeners=1`.

Drei Einschränkungen, die in der Natur der Sache liegen:

- **Nicht live.** Camo cacht. Die Antwort schickt `Cache-Control: no-store`, was die Aktualisierung
  deutlich häufiger macht, aber nicht erzwingt — die Karte ist „vor ein paar Minuten", nie „jetzt".
- **Keine Interaktion**, kein Spotify-Link, kein Hover. Es ist ein Bild. Ein Link drumherum geht:
  `<a href="https://leibniz-fm.lukassanner.de/"><picture>…</picture></a>`.
- **Keine eigenen Schriften.** Camo lädt nur diese eine URL, `@font-face` bliebe wirkungslos. Die Karte
  nutzt den System-Font-Stack, sieht unter macOS/Windows/Linux also leicht unterschiedlich aus.
