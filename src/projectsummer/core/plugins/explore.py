"""Explore plugins: look at the data directly, rather than through a feature."""

from __future__ import annotations

from datetime import date

import duckdb

from projectsummer.core import db
from projectsummer.core.db import LibraryState
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result


class UnavailableError(LetterboxdError):
    """Something optional could not be started."""


class UiServer(Result):
    """Where DuckDB's web UI is being served."""

    url: str
    """The address to open in a browser."""
    database: str
    """The database file the UI is attached to."""
    state: LibraryState
    """How far through the pipeline that database is."""


class Overview(Result):
    """What your library holds, and how far through the pipeline it is."""

    state: LibraryState
    """`empty`: nothing imported. `staged`: an export is imported but not yet
    enriched, so nothing is queryable. `ready`: films are enriched."""
    films_imported: int
    """Films in the imported export."""
    diary_entries_imported: int
    """Diary rows in the imported export."""
    resolved_identities: int
    """Imported films whose TMDB id has been looked up."""
    films_enriched: int
    """Films with TMDB metadata: the queryable library."""
    diary_entries_enriched: int
    """Diary rows attached to an enriched film."""
    lists: int
    """Lists in the library."""
    list_entries: int
    """Films across all lists."""
    watched: int
    """Imported films marked watched."""
    watchlist: int
    """Imported films on the watchlist."""
    liked: int
    """Imported films liked."""
    rated: int
    """Imported films with a rating."""
    first_watch: date | None
    """The earliest watched date in the imported diary."""
    last_watch: date | None
    """The most recent watched date in the imported diary."""


# "write" because loading the ui extension downloads and installs it, which a
# read-only connection forbids.
@plugin(category="explore", serves=True, access="write")
def ui(open_browser: bool = True) -> UiServer:
    """Open DuckDB's built-in web UI on your library.

    A SQL notebook and table browser over every table and view, against the
    database this command is using. Its column explorer shows a histogram and
    summary statistics for each column of a result; it does not plot one
    column against another. It
    is part of DuckDB itself, so there is nothing extra to install beyond the
    extension, which is downloaded once on first use.

    The server runs for as long as the command does.

    Args:
        open_browser: Also open the UI in your default browser. Turn this off
            on a machine with no browser, and use the returned URL yourself.

    Returns:
        The URL the UI is served on, and the database it is attached to.

    Raises:
        UnavailableError: If the `ui` extension cannot be installed or
            loaded, which usually means no internet on first use.
    """
    conn = db.get_connection()

    try:
        conn.execute("INSTALL ui")
        conn.execute("LOAD ui")
    except duckdb.Error as error:
        raise UnavailableError(
            "Could not load DuckDB's `ui` extension. It is downloaded the "
            f"first time it is used, so this usually means no internet "
            f"connection. Underlying error: {error}"
        ) from None

    # start_ui opens a browser as well; start_ui_server just serves.
    statement = "CALL start_ui()" if open_browser else "CALL start_ui_server()"
    conn.execute(statement)

    # get_ui_url is a table function, so it belongs in FROM, not SELECT.
    url = conn.execute("SELECT * FROM get_ui_url()").fetchone()[0]
    return UiServer(url=url, database=str(db.database_path()), state=db.library_state())


@plugin(category="explore")
def overview() -> Overview:
    """Summarise what is in your library, without opening anything.

    Works at any stage of the pipeline, so it is also the quickest way to see
    whether an import landed and whether enrichment still needs running.

    Returns:
        Counts for each stage of the pipeline, and the span of your diary.
    """
    counts = db.query(
        """
        SELECT
            (SELECT count(*) FROM staging_films)  AS films_imported,
            (SELECT count(*) FROM staging_diary)  AS diary_entries_imported,
            (SELECT count(*) FROM film_identity WHERE tmdb_id IS NOT NULL)
                                                  AS resolved_identities,
            (SELECT count(*) FROM films)          AS films_enriched,
            (SELECT count(*) FROM diary_entries)  AS diary_entries_enriched,
            (SELECT count(*) FROM lists)          AS lists,
            (SELECT count(*) FROM list_entries)   AS list_entries,
            count(*) FILTER (watched)             AS watched,
            count(*) FILTER (on_watchlist)        AS watchlist,
            count(*) FILTER (liked)               AS liked,
            count(*) FILTER (my_rating IS NOT NULL) AS rated,
            (SELECT min(watched_date) FROM staging_diary) AS first_watch,
            (SELECT max(watched_date) FROM staging_diary) AS last_watch
        FROM staging_films
        """
    )[0]
    return Overview(state=db.library_state(), **counts)
