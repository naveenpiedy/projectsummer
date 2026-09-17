"""Analysis plugins: what your viewing says over time.

Each answers a question an agent could also answer with `query`, but with one
fixed definition of every figure, so the CLI and every conversation count a
rewatch or an average rating the same way.
"""

from __future__ import annotations

from typing import Any, Literal

from projectsummer.core import db
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import MissingArgumentError
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result

#: Most values a breakdown may list per period.
MAX_TOP = 20


class Share(Result):
    """One value of a breakdown, and how many viewings it accounts for."""

    name: str
    """The genre, language code, country or decade, e.g. "Horror", "ta",
    "India" or "1990s"."""
    viewings: int
    """Viewings in the period of a film with this value."""


class TrendPeriod(Result):
    """Your viewing in one year or one month."""

    period: str
    """The year, e.g. "2024", or the month, e.g. "2024-03"."""
    viewings: int
    """Diary entries watched in the period, rewatches included."""
    films: int
    """Different films watched in the period."""
    first_watches: int
    """Viewings not marked as a rewatch."""
    rewatches: int
    """Viewings marked as a rewatch."""
    rated: int
    """Viewings given a rating at the time."""
    average_rating: float | None
    """Average rating at the time of viewing, 0.5 to 5; None if nothing was
    rated."""
    hours: float
    """Total running time of the viewings, in hours. A film with no runtime
    on TMDB adds nothing."""
    genres: list[Share]
    """The most-watched genres, most viewings first. A film counts once for
    each of its genres."""
    languages: list[Share]
    """The most-watched original languages, as ISO 639-1 codes, e.g. "en",
    "ta", "ja"."""
    countries: list[Share]
    """The most-watched production countries. A film counts once for each."""
    decades: list[Share]
    """The most-watched release decades, e.g. "1990s"."""


class Trends(Result):
    """Your viewing, period by period."""

    by: Literal["year", "month"]
    """Whether each period is a year or a month."""
    year: int | None
    """The year the periods are limited to, if any."""
    periods: list[TrendPeriod]
    """Every period in order, oldest first, including ones with no viewings
    so that gaps show."""


# Each breakdown is one expression over a viewing. A list column is unnested,
# so a film counts once for each value it holds.
_BREAKDOWNS = {
    "genres": "unnest(genres)",
    "languages": "original_language",
    "countries": "unnest(production_countries)",
    "decades": "CAST(release_year // 10 * 10 AS VARCHAR) || 's'",
}

_NO_VIEWINGS: dict[str, Any] = {
    "viewings": 0, "films": 0, "first_watches": 0, "rewatches": 0, "rated": 0,
    "average_rating": None, "hours": 0.0,
}


@plugin(category="analysis")
def trends(
    by: Literal["year", "month"] = "year",
    year: int | None = None,
    top: int = 3,
) -> Trends:
    """Show how your viewing changes over time, by year or by month.

    For each period: how many films you watched, first watches and rewatches,
    your average rating at the time, hours watched, and the genres, original
    languages, production countries and release decades you watched most.
    Counts come from your diary, so only viewings with a watched date count.

    Args:
        by: "year" for one period per year of your diary, or "month" for the
            twelve months of one year.
        year: The year to show. Needed by month; by year it narrows the
            result to that one year.
        top: How many genres, languages, countries and decades to list per
            period, from 1 to 20.

    Returns:
        One entry per period, oldest first.

    Raises:
        MissingArgumentError: If by is "month" and no year is given.
        InvalidArgumentError: If top is out of range.
    """
    if by == "month" and year is None:
        raise MissingArgumentError(
            "By month, give the year to show, e.g. year=2024.",
            argument="year",
            question="Which year",
        )
    if not 1 <= top <= MAX_TOP:
        raise InvalidArgumentError(f"top must be between 1 and {MAX_TOP}, not {top}.")

    filters = "d.watched_date IS NOT NULL"
    params: list[Any] = ["%Y" if by == "year" else "%Y-%m"]
    if year is not None:
        filters += " AND year(d.watched_date) = ?"
        params.append(year)

    viewings = f"""
        SELECT strftime(d.watched_date, ?) AS period, d.tmdb_id, d.rating,
               d.rewatch, f.runtime, f.genres, f.original_language,
               f.production_countries, f.year AS release_year
        FROM diary_entries d
        JOIN films f USING (tmdb_id)
        WHERE {filters}
    """

    summary = {
        row.pop("period"): row
        for row in db.query(
            f"""
            SELECT period,
                   count(*)                             AS viewings,
                   count(DISTINCT tmdb_id)              AS films,
                   count(*) FILTER (WHERE NOT rewatch)  AS first_watches,
                   count(*) FILTER (WHERE rewatch)      AS rewatches,
                   count(rating)                        AS rated,
                   round(avg(rating), 2)                AS average_rating,
                   round(coalesce(sum(runtime), 0) / 60, 1) AS hours
            FROM ({viewings})
            GROUP BY period
            """,
            params,
        )
    }

    breakdowns: dict[str, dict[str, list[Share]]] = {}
    for field, expression in _BREAKDOWNS.items():
        shares: dict[str, list[Share]] = {}
        for row in db.query(
            f"""
            SELECT period, name, count(*) AS viewings
            FROM (SELECT period, {expression} AS name FROM ({viewings}))
            WHERE name IS NOT NULL AND name <> ''
            GROUP BY period, name
            QUALIFY row_number() OVER (
                PARTITION BY period ORDER BY count(*) DESC, name
            ) <= ?
            ORDER BY period, viewings DESC, name
            """,
            [*params, top],
        ):
            shares.setdefault(row["period"], []).append(
                Share(name=row["name"], viewings=row["viewings"])
            )
        breakdowns[field] = shares

    return Trends(
        by=by,
        year=year,
        periods=[
            TrendPeriod(
                period=period,
                **summary.get(period, _NO_VIEWINGS),
                **{field: shares.get(period, []) for field, shares in breakdowns.items()},
            )
            for period in _periods(by, year, summary)
        ],
    )


def _periods(by: str, year: int | None, summary: dict[str, Any]) -> list[str]:
    """Every period to report, so a year or month with no viewings still shows."""
    if by == "month":
        return [f"{year}-{month:02d}" for month in range(1, 13)]
    if year is not None:
        return [str(year)]
    if not summary:
        return []
    years = [int(period) for period in summary]
    return [str(each) for each in range(min(years), max(years) + 1)]
