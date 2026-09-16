"""Library plugins: getting your Letterboxd data in and keeping it current."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectsummer.core.enrich import enrich_all
from projectsummer.core.ingest import ingest_export
from projectsummer.core.resolve import DEFAULT_DELAY, resolve_all
from projectsummer.core.sync import sync_feed
from projectsummer.core.registry import plugin


@plugin(name="ingest", category="library")
def ingest(export_path: Path) -> dict[str, Any]:
    """Import a Letterboxd CSV export.

    Download your export from Letterboxd (Settings -> Data -> Export Your
    Data) and point this straight at the .zip -- there is no need to unzip it
    first. An already-unzipped directory works too.

    This loads the raw data only. Films are identified by Letterboxd URIs at
    this stage; run enrichment afterwards to resolve them to TMDB metadata.

    Args:
        export_path: Your export .zip, or an unzipped directory containing
            diary.csv and watchlist.csv.

    Returns:
        Counts of what was loaded, plus any files that were skipped.

    Raises:
        ExportNotFoundError: If the path holds no Letterboxd CSVs.
    """
    return ingest_export(export_path).as_dict()


@plugin(name="resolve", category="library")
def resolve(limit: int | None = None, delay: float = DEFAULT_DELAY) -> dict[str, Any]:
    """Look up the TMDB id for each imported film.

    A Letterboxd export contains no film ids, only boxd.it links, so each
    one has to be looked up on its Letterboxd page. This is the only part of
    the tool that reads Letterboxd's website.

    Every result is cached, so a film is looked up once and never again --
    not on a re-import, and not on a later export. Interrupting is safe: a
    second run continues where the first stopped.

    Expect roughly one film per second. For a first import of a thousand
    films that is around ten minutes, once.

    Args:
        limit: Only resolve this many films. Useful for a trial run before
            committing to the whole library.
        delay: Seconds to wait between requests. Lower it only if you have
            a good reason to.

    Returns:
        Counts of what resolved, what failed, and what is still outstanding.
    """
    return resolve_all(limit=limit, delay=delay)


@plugin(name="enrich", category="library")
def enrich(limit: int | None = None) -> dict[str, Any]:
    """Fetch film metadata from TMDB and build the queryable tables.

    Run this after `resolve`. It fetches cast, crew, genres, runtime and
    ratings for every resolved film, overlays your own viewing data, and
    rebuilds your diary -- one row per viewing, so rewatches are kept.

    Roughly a thousand films per minute. Safe to interrupt: a second run
    fetches only what is still missing.

    Needs a TMDB API token in TMDB_API_KEY. A free account provides one at
    https://www.themoviedb.org/settings/api

    Args:
        limit: Only fetch this many films, for a trial run.

    Returns:
        Counts of what was fetched and built.

    Raises:
        MissingTokenError: If no TMDB token is configured.
    """
    return enrich_all(limit=limit)


@plugin(name="sync", category="library")
def sync(username: str | None = None) -> dict[str, Any]:
    """Catch up with your recent Letterboxd activity.

    Reads your public RSS feed, which carries TMDB ids directly -- so unlike
    the initial import this needs no lookups against Letterboxd's website. It
    covers roughly your last fifty diary entries and reviews; watchlist
    additions and likes are not published in any feed, so those still come
    from a fresh export.

    Films you have never logged before need their metadata from TMDB, so
    TMDB_API_KEY is required if the feed contains any.

    Args:
        username: Whose feed to read. Defaults to LETTERBOXD_USERNAME, or the
            username recorded when you imported an export.

    Returns:
        What the feed held and what was new.

    Raises:
        NoUsernameError: If no username is configured or importable.
        FeedUnavailableError: If the feed cannot be fetched.
    """
    return sync_feed(username=username)
