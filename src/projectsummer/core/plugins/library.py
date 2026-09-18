"""Library plugins: getting your Letterboxd data in and keeping it current."""

from __future__ import annotations

from pathlib import Path

from projectsummer import config
from projectsummer.core import progress
from projectsummer.core.enrich import EnrichResult, MissingTokenError, enrich_all
from projectsummer.core.ingest import IngestResult, ingest_export
from projectsummer.core.resolve import DEFAULT_DELAY, ResolveResult, resolve_all
from projectsummer.core.results import Result
from projectsummer.core.sync import SyncResult, sync_feed
from projectsummer.core.registry import plugin


class SetupResult(Result):
    """What each step of a first-time setup did."""

    ingest: IngestResult
    """What was imported from the export."""
    resolve: ResolveResult
    """Which films' TMDB ids were looked up on Letterboxd."""
    enrich: EnrichResult
    """What was fetched from TMDB and built."""


@plugin(name="ingest", category="library", access="write")
def ingest(export_path: Path) -> IngestResult:
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
    return ingest_export(export_path)


@plugin(name="resolve", category="library", access="write", open_world=True)
def resolve(limit: int | None = None, delay: float = DEFAULT_DELAY) -> ResolveResult:
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


@plugin(name="enrich", category="library", access="write", open_world=True)
def enrich(limit: int | None = None, retry_missing: bool = False) -> EnrichResult:
    """Fetch film metadata from TMDB and build the queryable tables.

    Run this after `resolve`. It fetches cast, crew, genres, runtime and
    ratings for every resolved film, overlays your own viewing data, and
    rebuilds your diary -- one row per viewing, so rewatches are kept. Then
    it fetches each credited person's details: birthday, birthplace, other
    names.

    Films take a couple of minutes per thousand. People take longer -- a
    library of 1,400 films credits about 14,000 people, one request each,
    roughly twenty minutes -- but only once. Safe to interrupt: a second run fetches
    only what is still missing, and people come last, so everything else is
    already done.

    Needs a TMDB API token in TMDB_API_KEY. A free account provides one at
    https://www.themoviedb.org/settings/api

    A film TMDB has no record of is remembered as missing and not asked for
    again, unless retry_missing says to ask once more.

    Args:
        limit: Only fetch this many films, and this many people, for a trial run.
        retry_missing: Ask again for the films TMDB previously said it did not
            have.

    Returns:
        Counts of what was fetched and built.

    Raises:
        MissingTokenError: If no TMDB token is configured.
    """
    return enrich_all(limit=limit, retry_missing=retry_missing)


# Exposed over MCP: it only adds what Letterboxd has already published, it
# takes seconds rather than minutes, and it is how an agent gets a fresh view.
@plugin(
    name="sync", category="library", access="write", mcp=True,
    idempotent=True, open_world=True,
)
def sync(username: str | None = None) -> SyncResult:
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


@plugin(name="setup", category="library", access="write", open_world=True)
def setup(export_path: Path) -> SetupResult:
    """Build your library from a Letterboxd export in one go.

    Runs ingest, resolve and enrich in turn: the whole first-time setup. For a
    library of a thousand films expect around forty minutes, most of it
    looking films up on Letterboxd and fetching people from TMDB. Interrupting
    is safe, and running setup again with the same export picks up where it
    stopped, since each step skips what is already done.

    The TMDB token is checked before anything starts, so a missing one is
    found now rather than after the Letterboxd lookups.

    Args:
        export_path: Your export .zip, or an unzipped directory containing
            diary.csv and watchlist.csv.

    Returns:
        What each of the three steps did.

    Raises:
        MissingTokenError: If no TMDB token is configured.
        ExportNotFoundError: If the path holds no Letterboxd CSVs.
    """
    if not config.tmdb_token():
        raise MissingTokenError(
            "No TMDB API token, which setup needs for its last step. Create a "
            "free account at https://www.themoviedb.org/settings/api, then set "
            "TMDB_API_KEY in your environment or in a .env file, and run "
            "setup again."
        )

    progress.note("Step 1 of 3: importing your export")
    imported = ingest_export(export_path)
    progress.note("Step 2 of 3: looking up TMDB ids on Letterboxd")
    resolved = resolve_all()
    progress.note("Step 3 of 3: fetching metadata and people from TMDB")
    enriched = enrich_all()
    return SetupResult(ingest=imported, resolve=resolved, enrich=enriched)
