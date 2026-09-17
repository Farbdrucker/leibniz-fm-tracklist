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

## Titelseiten

Jeder Titel hat eine eigene Seite, z. B. `/arctic-monkeys/fluorescent-adolescent`: Cover,
Album mit Tracklist, Infos zur Band und wann der Song auf leibniz.fm lief. Die Daten kommen
aus [MusicBrainz](https://musicbrainz.org/), dem [Cover Art Archive](https://coverartarchive.org/),
[Wikidata](https://www.wikidata.org/)/Wikipedia und — mit `DISCOGS_CONSUMER_KEY`/`_SECRET`
in der `.env` — [Discogs](https://www.discogs.com/). Neu gespielte Titel werden sofort
nachgeschlagen, ältere beim ersten Aufruf ihrer Seite.

## Einbetten

Die Tracklist gibt es auch als Widget für fremde Seiten — als `<script>`-Embed für WordPress,
als iframe-Fallback und als Bild (`/embed.svg`) für GitHub-READMEs:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://leibniz-fm.lukassanner.de/embed.svg?w=live&amp;theme=dark">
  <img alt="Was gerade auf leibniz.fm läuft" src="https://leibniz-fm.lukassanner.de/embed.svg?w=live&amp;theme=light">
</picture>

Anleitung, Attribute und Einschränkungen: [docs/embedding.md](docs/embedding.md).

## Daten selbst nutzen

Die Titel lassen sich auch ohne die Website weiterverarbeiten — über die öffentliche API
dieser Instanz (`/api/tracks`, `/api/now`, `/api/live`) oder ganz ohne Umweg über mich,
direkt aus Icecasts `status-json.xsl`. Endpunkte, Datenmodell, Fallstricke und ein
30-Zeilen-Mitschnitt zum Selberbauen: [docs/data.md](docs/data.md).
