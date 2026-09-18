"""List plugins: your Letterboxd lists, and canonical lists to compare against."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from projectsummer.core import db
from projectsummer.core.list_builder import (
    BuiltListResult,
    ListFilters,
    build_list,
    parse_date,
)
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import NoResultError
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result


class ListSummary(Result):
    """One of your lists."""

    slug: str
    """Identifies the list: the export's filename, e.g. "wes-anderson-ranked"."""
    name: str
    """The list's title."""
    films: int
    """How many films it holds."""
    ranked: bool
    """Whether its order is a deliberate ranking. Letterboxd does not export
    this, so it is false until set with set_list_ranked."""
    source: Literal["letterboxd", "imported"]
    """`letterboxd` for your own lists; `imported` for canonical lists added to
    compare against."""
    created_date: date | None
    """When the list was created on Letterboxd."""


class Lists(Result):
    """Every list in your library."""

    lists: list[ListSummary]
    """The lists, largest first."""


class ListRanking(Result):
    """A list's ranked state after a change."""

    slug: str
    """The list's slug."""
    name: str
    """The list's title."""
    ranked: bool
    """Whether its positions are now treated as a ranking."""


@plugin(name="lists", category="lists")
def show_lists() -> Lists:
    """Show every list, with how many films each holds.

    Returns:
        Every list, ordered by size.
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
    return Lists(lists=rows)


@plugin(category="lists", access="write", mcp=True, idempotent=True)
def set_list_ranked(slug: str, ranked: bool = True) -> ListRanking:
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
    return ListRanking(**updated[0])


# Writes a file rather than the database, but that is still a change the
# user did not make themselves, so it is opted in explicitly. Destructive
# because it overwrites a file already at that path.
@plugin(
    name="list_builder", category="lists", access="write", mcp=True,
    destructive=True, idempotent=True,
)
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
) -> BuiltListResult:
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


class OverlapFilm(Result):
    """A film on one or both sides of a comparison."""

    tmdb_id: int
    """TMDB's id for the film."""
    title: str
    """The film's title."""
    year: int | None
    """Release year."""
    your_rating: float | None
    """Your rating, or None if you have not rated it."""
    watched: bool
    """Whether you have watched it."""


class OverlapSide(Result):
    """One side of a comparison: a list, or a set of your films."""

    name: str
    """The list's title, or what the set holds, e.g. "films you have watched"."""
    slug: str | None
    """The list's slug, or None for a set that is not a list."""
    films: int
    """Films on this side that are in your library."""
    only_here: int
    """Of those, films the other side does not have."""


class ListOverlap(Result):
    """What two sides of your library have in common."""

    first: OverlapSide
    """The list asked about."""
    second: OverlapSide
    """What it was compared with."""
    shared: int
    """Films on both sides."""
    percent_of_first: float
    """Share of the first side that is on both, 0 to 100."""
    percent_of_second: float
    """Share of the second side that is on both."""
    shared_films: list[OverlapFilm]
    """Some of the films on both sides, your highest-rated first."""
    only_in_first: list[OverlapFilm]
    """Some of the films only the first side has."""
    only_in_second: list[OverlapFilm]
    """Some of the films only the second side has."""


#: Sets that can stand in for a list on either side of a comparison.
_SETS: dict[str, tuple[str, str]] = {
    "watched": ("films you have watched", "watched"),
    "watchlist": ("your watchlist", "on_watchlist"),
    "liked": ("films you have liked", "liked"),
    "rated": ("films you have rated", "my_rating IS NOT NULL"),
}

#: Most films listed in each of the three samples.
MAX_EXAMPLES = 50


@plugin(category="lists")
def list_overlap(first: str, second: str = "watched", top: int = 5) -> ListOverlap:
    """Compare a list with another list, or with what you have watched.

    Answers "how much of this list have I seen?" and "what do these two lists
    share?". Either side may be a list's slug, as shown by `lists`, or one of
    watched, watchlist, liked and rated.

    Only films enriched into your library count, since a list row that was
    never resolved to a TMDB id cannot be compared with anything.

    Args:
        first: The list to ask about: a slug, or watched, watchlist, liked or
            rated.
        second: What to compare it with. Defaults to the films you have
            watched, which is how much of the list you have seen.
        top: How many films to show in each of the three samples, up to 50.

    Returns:
        How many films the two sides share, how much of each that is, and
        some films from each part.

    Raises:
        NoResultError: If a list does not exist or has no films in your
            library.
        InvalidArgumentError: If both sides are the same, or top is out of
            range.
    """
    if not 1 <= top <= MAX_EXAMPLES:
        raise InvalidArgumentError(f"top must be between 1 and {MAX_EXAMPLES}, not {top}.")
    if first == second:
        raise InvalidArgumentError("Give two different sides to compare.")

    left_name, left_ids = _side(first)
    right_name, right_ids = _side(second)
    shared = left_ids & right_ids

    return ListOverlap(
        first=OverlapSide(
            name=left_name, slug=None if first in _SETS else first,
            films=len(left_ids), only_here=len(left_ids - right_ids),
        ),
        second=OverlapSide(
            name=right_name, slug=None if second in _SETS else second,
            films=len(right_ids), only_here=len(right_ids - left_ids),
        ),
        shared=len(shared),
        percent_of_first=_percent(len(shared), len(left_ids)),
        percent_of_second=_percent(len(shared), len(right_ids)),
        shared_films=_films(shared, top),
        only_in_first=_films(left_ids - right_ids, top),
        only_in_second=_films(right_ids - left_ids, top),
    )


def _side(name: str) -> tuple[str, set[int]]:
    """What a side is called, and the films it holds."""
    if name in _SETS:
        label, condition = _SETS[name]
        rows = db.query(f"SELECT tmdb_id FROM films WHERE {condition}")
        if not rows:
            raise NoResultError(f"There are no {label} in your library yet.")
        return label, {row["tmdb_id"] for row in rows}

    found = db.query("SELECT list_id, name FROM lists WHERE slug = ?", [name])
    if not found:
        known = db.query("SELECT slug FROM lists ORDER BY slug LIMIT 10")
        suggestions = ", ".join(row["slug"] for row in known) or "none"
        raise NoResultError(
            f"No list with slug {name!r}, and it is not one of "
            f"{', '.join(_SETS)}. Known slugs include: {suggestions}."
        )
    rows = db.query(
        """
        SELECT DISTINCT e.tmdb_id
        FROM list_entries e
        JOIN films f USING (tmdb_id)
        WHERE e.list_id = ?
        """,
        [found[0]["list_id"]],
    )
    if not rows:
        raise NoResultError(
            f"The list {name!r} holds no films that are in your library yet."
        )
    return found[0]["name"], {row["tmdb_id"] for row in rows}


def _percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _films(ids: set[int], top: int) -> list[OverlapFilm]:
    """A sample of films, your highest-rated first so the best are shown."""
    return [
        OverlapFilm(**row)
        for row in db.query(
            """
            SELECT tmdb_id, title, year, my_rating AS your_rating, watched
            FROM films
            WHERE tmdb_id IN (SELECT unnest(?::BIGINT[]))
            ORDER BY my_rating DESC NULLS LAST, title
            LIMIT ?
            """,
            [sorted(ids), top],
        )
    ]
