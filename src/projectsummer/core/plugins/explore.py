"""Explore plugins: look at the data directly, rather than through a feature."""

from __future__ import annotations

from typing import Any

import duckdb

from projectsummer.core import db
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.registry import plugin


class UnavailableError(LetterboxdError):
    """Something optional could not be started."""


@plugin(category="explore", serves=True)
def ui(open_browser: bool = True) -> dict[str, Any]:
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
    return {"url": url, "database": str(db.database_path()), "state": db.library_state()}


@plugin(category="explore")
def overview() -> list[dict[str, Any]]:
    """Summarise what is in your library, without opening anything.

    Works at any stage of the pipeline, so it is also the quickest way to see
    whether an import landed and whether enrichment still needs running.

    Returns:
        One row per table, with how many records it holds and what that means.
    """
    counts = {
        "films (enriched)": "SELECT count(*) FROM films",
        "diary entries (enriched)": "SELECT count(*) FROM diary_entries",
        "films (imported)": "SELECT count(*) FROM staging_films",
        "diary entries (imported)": "SELECT count(*) FROM staging_diary",
        "lists": "SELECT count(*) FROM lists",
        "list entries": "SELECT count(*) FROM list_entries",
        "resolved identities": "SELECT count(*) FROM film_identity WHERE tmdb_id IS NOT NULL",
    }
    rows = [
        {"what": label, "count": db.query(sql)[0]["count_star()"]}
        for label, sql in counts.items()
    ]

    watched = db.query(
        "SELECT count(*) FILTER (watched) AS watched,"
        " count(*) FILTER (on_watchlist) AS watchlist,"
        " count(*) FILTER (liked) AS liked,"
        " count(*) FILTER (my_rating IS NOT NULL) AS rated"
        " FROM staging_films"
    )[0]
    rows.extend(
        {"what": f"  of which {label}", "count": value}
        for label, value in watched.items()
    )

    span = db.query(
        "SELECT min(watched_date) AS first, max(watched_date) AS last FROM staging_diary"
    )[0]
    if span["first"]:
        rows.append({"what": "first logged watch", "count": str(span["first"])})
        rows.append({"what": "most recent watch", "count": str(span["last"])})

    rows.append({"what": "pipeline state", "count": db.library_state()})
    return rows
