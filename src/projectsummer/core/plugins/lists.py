"""List plugins: your Letterboxd lists, and canonical lists to compare against."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectsummer.core import db
from projectsummer.core.list_builder import ListFilters, build_list, parse_date
from projectsummer.core.errors import NoResultError
from projectsummer.core.registry import plugin


@plugin(name="lists", category="lists")
def show_lists() -> list[dict[str, Any]]:
    """Show every list, with how many films each holds.

    Returns:
        One row per list, ordered by size.
    """
    rows = db.query(
        """
        SELECT l.slug,
               l.name,
               count(e.entry_position) AS films,
               l.ranked,
               l.source,
               l.created_date
        FROM lists l
        LEFT JOIN list_entries e ON e.list_id = l.list_id
        GROUP BY ALL
        ORDER BY films DESC, l.name
        """
    )
    if not rows:
        raise NoResultError(
            "No lists yet. Import a Letterboxd export, which includes any "
            "lists you have made."
        )
    return rows


@plugin(category="lists")
def set_list_ranked(slug: str, ranked: bool = True) -> dict[str, Any]:
    """Mark a list as ranked, so its positions are treated as an ordering.

    Letterboxd's export gives every list a Position column whether or not the
    list is actually ranked, and exports no flag saying which is which -- all
    list files are structurally identical. So this cannot be detected on
    import; it is something you tell the tool once per list.

    Positions are always stored, so marking a list ranked later loses nothing.

    Args:
        slug: The list's slug, as shown by the `lists` command. This is the
            export's filename, e.g. "wes-anderson-ranked".
        ranked: Whether the list's positions are a deliberate ordering. Pass
            false to unset it again.

    Returns:
        The list, with its new state.

    Raises:
        NoResultError: If no list has that slug.
    """
    updated = db.query(
        "UPDATE lists SET ranked = ? WHERE slug = ? RETURNING slug, name, ranked",
        [ranked, slug],
    )
    if not updated:
        known = db.query("SELECT slug FROM lists ORDER BY slug LIMIT 10")
        suggestions = ", ".join(row["slug"] for row in known) or "none"
        raise NoResultError(
            f"No list with slug {slug!r}. Known slugs include: {suggestions}."
        )
    return updated[0]


@plugin(name="list_builder", category="lists")
def list_builder(
    output: Path,
    director: list[str] | None = None,
    actor: list[str] | None = None,
    keyword: list[str] | None = None,
    genre: list[str] | None = None,
    watched_from: str | None = None,
    watched_to: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_rating: float | None = None,
    status: str = "any",
    order_by: str = "release",
    reverse: bool = False,
    limit: int | None = None,
    sql: str | None = None,
) -> dict[str, Any]:
    """Build a Letterboxd-importable list from your library.

    Narrow your library with filters, or give a SQL query that returns a
    tmdb_id column. Every filter must hold, including repeats: two --actor
    options means films featuring both. The result is written as a CSV that
    Letterboxd's list importer accepts, matched exactly by TMDB id.

    Import it on Letterboxd by creating a new list and choosing Import. Only
    film identity is written, so importing cannot change your diary.

    Args:
        output: Where to write the list. Must end in .csv.
        director: Only films by this director. Repeat to require several.
            Exact name, case-insensitive; a near miss suggests the real name.
        actor: Only films with this actor in the leading cast. Repeatable.
        keyword: Only films carrying this TMDB keyword. Repeatable.
        genre: Only films in this genre. Repeatable.
        watched_from: Only films with a diary entry on or after this date,
            written YYYY-MM-DD.
        watched_to: Only films with a diary entry on or before this date.
        year_from: Only films released in or after this year.
        year_to: Only films released in or before this year.
        min_rating: Only films you rated at least this, from 0.5 to 5.
        status: Which films to start from: any, watched, watchlist or liked.
        order_by: List order: release, watched, rating, tmdb-rating or title.
        reverse: Reverse the list order.
        limit: Keep only this many films.
        sql: A single SELECT returning a tmdb_id column, used instead of the
            filters. Rows become the list in the order the query returns them.

    Returns:
        How many films were written, where, and the first few titles.

    Raises:
        NoResultError: If nothing matches, or a name is not in your library.
        InvalidFilterError: If a filter value is malformed.
        InvalidQueryError: If the query is not a single SELECT with tmdb_id.
    """
    filters = ListFilters(
        directors=tuple(director or ()),
        actors=tuple(actor or ()),
        keywords=tuple(keyword or ()),
        genres=tuple(genre or ()),
        watched_from=parse_date(watched_from, "watched-from"),
        watched_to=parse_date(watched_to, "watched-to"),
        year_from=year_from,
        year_to=year_to,
        min_rating=min_rating,
        status=status,
        order_by=order_by,
        reverse=reverse,
        limit=limit,
    )
    return build_list(output, filters=filters, sql=sql)
