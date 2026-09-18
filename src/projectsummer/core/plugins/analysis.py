"""Analysis plugins: what your viewing and your ratings say.

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


class TasteRow(Result):
    """What you make of one director, genre, decade and so on."""

    facet: Literal["directors", "actors", "genres", "decades", "languages", "countries"]
    """What this row is."""
    standing: Literal["highest", "lowest"]
    """Whether it is among your highest- or lowest-rated of that facet."""
    name: str
    """The director, actor, genre, decade, language code or country."""
    films: int
    """Rated films behind the average."""
    average_rating: float
    """Your average rating for them, 0.5 to 5."""
    tmdb_average: float | None
    """TMDB's average for the same films, rescaled to your 0.5 to 5 so the two
    can be compared. None if TMDB rates none of them."""
    difference: float | None
    """Your average minus TMDB's: positive where you are the kinder one."""


class ContrarianFilm(Result):
    """A film you and TMDB disagree about."""

    tmdb_id: int
    """TMDB's id for the film."""
    title: str
    """The film's title."""
    year: int | None
    """Release year."""
    your_rating: float
    """Your rating, 0.5 to 5."""
    tmdb_rating: float
    """TMDB's average, rescaled to your 0.5 to 5."""
    difference: float
    """Your rating minus TMDB's."""


class Taste(Result):
    """What your ratings say about you."""

    rated_films: int
    """Watched films you have rated: everything here is drawn from these."""
    average_rating: float | None
    """Your average rating across them."""
    tmdb_average: float | None
    """TMDB's average across the same films, on your scale."""
    generosity: float | None
    """Your average minus TMDB's. Positive means you rate above the crowd."""
    rows: list[TasteRow]
    """Each facet's highest-rated, best first, then its lowest-rated, worst
    first. A row is never in both, so a facet with few values may show fewer
    than asked for."""
    loved_more_than_most: list[ContrarianFilm]
    """Films you rate furthest above TMDB."""
    liked_less_than_most: list[ContrarianFilm]
    """Films you rate furthest below TMDB."""


#: Rated, watched films: everything `taste` measures comes from these. TMDB's
#: rating is halved throughout, so its 0 to 10 average can be read next to
#: your own 0.5 to 5.
_RATED = """
    SELECT tmdb_id, my_rating, tmdb_rating / 2 AS crowd_rating, tmdb_vote_count,
           title, year, genres, original_language, production_countries
    FROM films
    WHERE watched AND my_rating IS NOT NULL
"""

#: How each facet names a row: an expression over a rated film, or the credit
#: role whose people it counts.
_FACETS: dict[str, str] = {
    "directors": "director",
    "actors": "actor",
    "genres": "unnest(genres)",
    "decades": "CAST(year // 10 * 10 AS VARCHAR) || 's'",
    "languages": "original_language",
    "countries": "unnest(production_countries)",
}

#: Least TMDB votes a film needs before disagreeing with it means much.
MIN_VOTES = 100


@plugin(category="analysis")
def taste(
    facet: Literal[
        "all", "directors", "actors", "genres", "decades", "languages", "countries"
    ] = "all",
    top: int = 3,
    min_films: int = 3,
    actor_min_films: int = 5,
) -> Taste:
    """Show what you rate highly and what you do not, and where you differ from the crowd.

    Your highest- and lowest-rated directors, actors, genres, release decades,
    original languages and production countries, each alongside TMDB's average
    for the same films, plus the films you and TMDB disagree about most.

    Ratings are your current ones on watched films, so a film rated years ago
    counts as it stands today. TMDB's 0 to 10 average is halved everywhere, so
    both sit on your 0.5 to 5 scale.

    Args:
        facet: One facet to show, or "all" for every one.
        top: How many to list each way, from 1 to 20.
        min_films: Least rated films a director, genre or decade needs to be
            listed, so a single film cannot top a list.
        actor_min_films: The same for actors, who are far more numerous and so
            noisier at a low count.

    Returns:
        Each facet's highest and lowest, the films you disagree with TMDB
        about most, and how your ratings compare with the crowd's overall.

    Raises:
        InvalidArgumentError: If top is out of range, or a minimum is below 1.
        EmptyDatabaseError: If the library holds no films yet.
    """
    if not 1 <= top <= MAX_TOP:
        raise InvalidArgumentError(f"top must be between 1 and {MAX_TOP}, not {top}.")
    if min_films < 1 or actor_min_films < 1:
        raise InvalidArgumentError("A minimum number of films must be at least 1.")
    db.require_films()

    overall = db.query(
        f"""
        SELECT count(*)                                      AS rated_films,
               round(avg(my_rating), 2)                      AS average_rating,
               round(avg(crowd_rating), 2)                   AS tmdb_average,
               round(avg(my_rating) - avg(crowd_rating), 2)  AS generosity
        FROM ({_RATED})
        """
    )[0]

    wanted = list(_FACETS) if facet == "all" else [facet]
    return Taste(
        **overall,
        rows=[
            row
            for name in wanted
            for row in _facet(name, top, actor_min_films if name == "actors" else min_films)
        ],
        loved_more_than_most=_contrarian(top, "DESC"),
        liked_less_than_most=_contrarian(top, "ASC"),
    )


def _facet(name: str, top: int, min_films: int) -> list[TasteRow]:
    """One facet's highest and lowest, with no row appearing in both."""
    rows = db.query(
        f"""
        SELECT name,
               count(*)                                     AS films,
               round(avg(my_rating), 2)                     AS average_rating,
               round(avg(crowd_rating), 2)                  AS tmdb_average,
               round(avg(my_rating) - avg(crowd_rating), 2) AS difference
        FROM ({_source(name)})
        WHERE name IS NOT NULL AND name <> ''
        GROUP BY key, name
        HAVING count(*) >= ?
        ORDER BY average_rating DESC, films DESC, name
        """,
        [min_films],
    )
    highest = [TasteRow(facet=name, standing="highest", **row) for row in rows[:top]]
    lowest = [TasteRow(facet=name, standing="lowest", **row) for row in reversed(rows[top:])]
    return highest + lowest[:top]


def _source(name: str) -> str:
    """The rated films cut one way: one row per value, per film."""
    expression = _FACETS[name]
    if name not in ("directors", "actors"):
        return (
            f"SELECT {expression} AS key, {expression} AS name, my_rating, crowd_rating "
            f"FROM ({_RATED})"
        )
    # Grouped by person_id, so two people of one name stay apart. DISTINCT
    # because a person can hold one role twice on the same film.
    return f"""
        SELECT DISTINCT r.tmdb_id, p.person_id AS key, p.name AS name,
               r.my_rating, r.crowd_rating
        FROM ({_RATED}) r
        JOIN film_credits c USING (tmdb_id)
        JOIN people p USING (person_id)
        WHERE c.role = '{expression}'
    """


def _contrarian(top: int, direction: str) -> list[ContrarianFilm]:
    """The films furthest from TMDB's view, one way or the other.

    Films nobody has voted on are left out: disagreeing with three strangers
    says nothing.
    """
    return [
        ContrarianFilm(**row)
        for row in db.query(
            f"""
            SELECT tmdb_id, title, year, my_rating AS your_rating,
                   round(crowd_rating, 2) AS tmdb_rating,
                   round(my_rating - crowd_rating, 2) AS difference
            FROM ({_RATED})
            WHERE crowd_rating IS NOT NULL AND tmdb_vote_count >= ?
            ORDER BY difference {direction}, tmdb_vote_count DESC
            LIMIT ?
            """,
            [MIN_VOTES, top],
        )
    ]
