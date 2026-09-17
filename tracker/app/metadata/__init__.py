"""Song info for the /<artist>/<song> pages: cover art, album, artist bio.

Sources, all optional: MusicBrainz + Cover Art Archive (musicbrainz.py),
Discogs (discogs.py, needs DISCOGS_CONSUMER_KEY/_SECRET), Wikidata + Wikipedia
(wikimedia.py). Orchestration, caching and the background worker live in
enricher.py.
"""

from .enricher import Enricher, MetadataWorker

__all__ = ["Enricher", "MetadataWorker"]
