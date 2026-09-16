"""Discovery plugins: help decide what to watch next."""

from __future__ import annotations

from typing import Any

from letterboxd_utility_tools.core import db
from letterboxd_utility_tools.core.errors import NoResultError
from letterboxd_utility_tools.core.registry import plugin

#: Columns returned for a picked film. Kept in one place so every discovery
#: plugin describes a film the same way.
_FILM_COLUMNS = """
    tmdb_id,
    title,
    year,
    runtime,
    genres,
    directors,
    tmdb_rating,
    overview,
    letterboxd_uri
"""


@plugin(category="discovery")
def random_watchlist_pick(
    genre: str | None = None,
    max_runtime: int | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Pick a random film from your watchlist.

    Films you have already logged are excluded, so the result is always
    something still unwatched.

    Args:
        genre: Only consider films in this genre. Case-insensitive,
            e.g. "horror" matches TMDB's "Horror".
        max_runtime: Only consider films at or under this many minutes.
        seed: Make the pick reproducible. The same seed and filters always
            return the same film.

    Returns:
        The chosen film, plus `candidates_considered`: how many watchlist
        films matched the filters.

    Raises:
        NoResultError: If no watchlist film matches the filters.
        EmptyDatabaseError: If no enriched films are available yet.
    """
    db.require_films()
    conn = db.get_connection()

    conditions = ["on_watchlist", "NOT watched"]
    params: list[Any] = []

    if genre is not None:
        conditions.append(
            "list_contains(list_transform(genres, g -> lower(g)), lower(?))"
        )
        params.append(genre)

    if max_runtime is not None:
        conditions.append("runtime IS NOT NULL AND runtime <= ?")
        params.append(max_runtime)

    if seed is not None:
        # DuckDB's setseed takes a value in [-1, 1]; map any int into it.
        conn.execute("SELECT setseed(?)", [((seed % 2_000_001) - 1_000_000) / 1_000_000])

    sql = f"""
        SELECT {_FILM_COLUMNS.strip()},
               count(*) OVER () AS candidates_considered
        FROM films
        WHERE {" AND ".join(conditions)}
        ORDER BY random()
        LIMIT 1
    """

    rows = db.query(sql, params)
    if rows:
        return rows[0]

    raise NoResultError(_describe_no_match(genre, max_runtime))


def _describe_no_match(genre: str | None, max_runtime: int | None) -> str:
    """Build a message that says which filters were too narrow."""
    filters = []
    if genre is not None:
        filters.append(f"genre {genre!r}")
    if max_runtime is not None:
        filters.append(f"runtime <= {max_runtime} min")

    if not filters:
        return "Your watchlist is empty (or every film on it is already watched)."
    return f"No unwatched watchlist film matches {' and '.join(filters)}."
