"""Build Letterboxd-importable lists from your library.

Two ways in, deliberately kept apart:

* **Filters** -- director, actor, keyword, genre, watched dates, release
  years, your rating, status. All of them must hold (AND), including repeats
  of the same filter: two `--actor`s means films with both.
* **A SQL query** that returns a `tmdb_id` column. Whatever it returns, in the
  order it returns it, becomes the list.

Both produce the same file, in the format Letterboxd's importer documents
(https://letterboxd.com/about/importing-data/). The details that matter:

* Films are matched exactly when an id is present, so every row carries
  `tmdbID` -- and every enriched film has one, so nothing is left to guessing.
* Internal quotes are escaped with a backslash, **not** doubled as standard
  CSV does. Python's `csv` module doubles them by default.
* There is no position column; the importer appends in file order, so the
  file's order is the list's order.
* Files are capped at 1MB. Larger lists are split into numbered parts, which
  keep their order when imported one after another into the same list.
* The same importer also writes to your diary. Rating, watched date and review
  columns are left out on purpose, so importing a list cannot touch it.

Nothing here writes to the database.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from projectsummer.core import db
from projectsummer.core.errors import LetterboxdError, NoResultError

#: Letterboxd's documented limit is 1MB; stay safely under it.
MAX_FILE_BYTES = 1_000_000

#: Columns written, in order. Identity columns only -- see module docstring.
IMPORT_COLUMNS = ("tmdbID", "imdbID", "Title", "Year", "Directors")

STATUSES = ("any", "watched", "watchlist", "liked")

#: Sort key and its natural direction. `reverse` flips the direction.
ORDERINGS: dict[str, tuple[str, str]] = {
    "release": ("f.release_date", "ASC"),
    "watched": ("v.first_viewing", "ASC"),
    "rating": ("f.my_rating", "DESC"),
    "tmdb-rating": ("f.tmdb_rating", "DESC"),
    "title": ("f.title", "ASC"),
}

#: Filterable list columns, keyed by the filter's name.
NAME_COLUMNS = {
    "director": "directors",
    "actor": "cast_members",
    "keyword": "keywords",
    "genre": "genres",
}


class InvalidFilterError(LetterboxdError):
    """A filter value is malformed or not one of the allowed choices."""


class InvalidQueryError(LetterboxdError):
    """A SQL query is not a single SELECT, or lacks a tmdb_id column."""


@dataclass(frozen=True, slots=True)
class ListFilters:
    """Everything a filter-built list can be narrowed by. All must hold."""

    directors: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    watched_from: date | None = None
    watched_to: date | None = None
    year_from: int | None = None
    year_to: int | None = None
    min_rating: float | None = None
    status: str = "any"
    order_by: str = "release"
    reverse: bool = False
    limit: int | None = None

    @property
    def names(self) -> dict[str, tuple[str, ...]]:
        return {
            "director": self.directors,
            "actor": self.actors,
            "keyword": self.keywords,
            "genre": self.genres,
        }

    def is_default(self) -> bool:
        return self == ListFilters()


@dataclass(frozen=True, slots=True)
class BuiltList:
    """A list ready to write, plus what went into it."""

    films: list[dict[str, Any]]
    not_in_library: int = 0
    description: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ parsing

def parse_date(value: str | None, label: str) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise InvalidFilterError(
            f"{label} must be a date like 2024-01-31, not {value!r}."
        ) from None


def validate(filters: ListFilters) -> None:
    """Reject values that would otherwise produce a silently wrong list."""
    if filters.status not in STATUSES:
        raise InvalidFilterError(
            f"status must be one of {', '.join(STATUSES)}, not {filters.status!r}."
        )
    if filters.order_by not in ORDERINGS:
        raise InvalidFilterError(
            f"order-by must be one of {', '.join(ORDERINGS)}, not {filters.order_by!r}."
        )
    if filters.watched_from and filters.watched_to and filters.watched_from > filters.watched_to:
        raise InvalidFilterError("watched-from is after watched-to, so nothing could match.")
    if filters.year_from and filters.year_to and filters.year_from > filters.year_to:
        raise InvalidFilterError("year-from is after year-to, so nothing could match.")
    if filters.limit is not None and filters.limit < 1:
        raise InvalidFilterError("limit must be at least 1.")


# ------------------------------------------------------------- name checks

def check_names_exist(filters: ListFilters) -> None:
    """Fail on any name that appears nowhere in the library, with suggestions.

    Without this a misspelt director produces an empty list, and an empty
    list looks exactly like "you have not seen any of their films".
    """
    for kind, names in filters.names.items():
        column = NAME_COLUMNS[kind]
        for name in names:
            found = db.query(
                f"SELECT 1 FROM films WHERE list_contains("
                f"list_transform({column}, x -> lower(x)), lower(?)) LIMIT 1",
                [name],
            )
            if found:
                continue
            suggestions = suggest(column, name)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise NoResultError(f"No {kind} named {name!r} in your library.{hint}")


def suggest(column: str, name: str, limit: int = 5) -> list[str]:
    """Names close to `name`: containing it first, then likely typos.

    Substring matching catches a surname ("nolan"); similarity catches a
    misspelling ("christoper nolan"). Neither alone covers both -- a surname
    scores only 0.42 against a full name on Jaro-Winkler.
    """
    rows = db.query(
        f"""
        WITH names AS (SELECT DISTINCT unnest({column}) AS name FROM films)
        SELECT name,
               contains(lower(name), lower(?)) AS partial,
               jaro_winkler_similarity(lower(name), lower(?)) AS similarity
        FROM names
        WHERE contains(lower(name), lower(?))
           OR jaro_winkler_similarity(lower(name), lower(?)) >= 0.85
        ORDER BY partial DESC, similarity DESC, name
        LIMIT ?
        """,
        [name, name, name, name, limit],
    )
    return [row["name"] for row in rows]


# ----------------------------------------------------------------- building

_FILM_FIELDS = "f.tmdb_id, f.imdb_id, f.title, f.year, f.directors"


def build_from_filters(filters: ListFilters) -> BuiltList:
    """Select films matching every filter, in the requested order."""
    validate(filters)
    db.require_films()
    check_names_exist(filters)

    conditions: list[str] = []
    params: list[Any] = [
        filters.watched_from, filters.watched_from,
        filters.watched_to, filters.watched_to,
    ]

    for kind, names in filters.names.items():
        column = NAME_COLUMNS[kind]
        for name in names:
            conditions.append(
                f"list_contains(list_transform(f.{column}, x -> lower(x)), lower(?))"
            )
            params.append(name)

    if filters.watched_from or filters.watched_to:
        conditions.append("v.tmdb_id IS NOT NULL")
    if filters.year_from is not None:
        conditions.append("f.year >= ?")
        params.append(filters.year_from)
    if filters.year_to is not None:
        conditions.append("f.year <= ?")
        params.append(filters.year_to)
    if filters.min_rating is not None:
        conditions.append("f.my_rating >= ?")
        params.append(filters.min_rating)
    if filters.status != "any":
        conditions.append(
            {"watched": "f.watched", "watchlist": "f.on_watchlist", "liked": "f.liked"}[
                filters.status
            ]
        )

    column, direction = ORDERINGS[filters.order_by]
    if filters.reverse:
        direction = "DESC" if direction == "ASC" else "ASC"

    sql = f"""
        WITH viewings AS (
            SELECT tmdb_id, min(watched_date) AS first_viewing
            FROM diary_entries
            WHERE (CAST(? AS DATE) IS NULL OR watched_date >= ?)
              AND (CAST(? AS DATE) IS NULL OR watched_date <= ?)
            GROUP BY tmdb_id
        )
        SELECT {_FILM_FIELDS}
        FROM films f
        LEFT JOIN viewings v ON v.tmdb_id = f.tmdb_id
        {"WHERE " + " AND ".join(conditions) if conditions else ""}
        ORDER BY {column} {direction} NULLS LAST, f.title, f.tmdb_id
        {"LIMIT " + str(int(filters.limit)) if filters.limit else ""}
    """
    films = db.query(sql, params)
    if not films:
        raise NoResultError(
            "No films in your library match all of: " + "; ".join(describe(filters)) + "."
        )
    return BuiltList(films=films, description=describe(filters))


def describe(filters: ListFilters) -> list[str]:
    """Plain-language summary of the filters, for reports and errors."""
    parts: list[str] = []
    for kind, names in filters.names.items():
        parts.extend(f"{kind} {name}" for name in names)
    if filters.watched_from or filters.watched_to:
        parts.append(f"watched {filters.watched_from or '...'} to {filters.watched_to or '...'}")
    if filters.year_from is not None or filters.year_to is not None:
        parts.append(f"released {filters.year_from or '...'} to {filters.year_to or '...'}")
    if filters.min_rating is not None:
        parts.append(f"your rating >= {filters.min_rating}")
    if filters.status != "any":
        parts.append(f"status {filters.status}")
    return parts or ["everything in your library"]


def build_from_sql(sql: str) -> BuiltList:
    """Use a query's `tmdb_id` column, in the order it returns rows.

    The query is checked by DuckDB's own parser: exactly one statement, and
    that statement a SELECT. Inspecting the text for keywords would miss
    `SELECT 1; DROP TABLE films` or a `DELETE ... RETURNING`; the parser does
    not.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        raise InvalidQueryError(f"That query does not parse: {error}") from None

    if len(statements) != 1:
        raise InvalidQueryError(
            f"Give exactly one query; found {len(statements)} statements."
        )
    if statements[0].type != duckdb.StatementType.SELECT:
        raise InvalidQueryError(
            f"Only a SELECT can build a list; this is a {statements[0].type.name}."
        )

    db.require_films()
    try:
        rows = db.query(sql)
    except duckdb.Error as error:
        raise InvalidQueryError(f"The query failed: {error}") from None

    if rows and "tmdb_id" not in rows[0]:
        raise InvalidQueryError(
            "The query must return a tmdb_id column. It returned: "
            + ", ".join(rows[0].keys())
        )

    ordered: list[int] = []
    seen: set[int] = set()
    for row in rows:
        tmdb_id = row["tmdb_id"]
        if tmdb_id is not None and tmdb_id not in seen:
            seen.add(tmdb_id)
            ordered.append(int(tmdb_id))

    if not ordered:
        raise NoResultError("The query returned no films.")

    known = {
        row["tmdb_id"]: row
        for row in db.query(
            f"SELECT {_FILM_FIELDS} FROM films f "
            "WHERE f.tmdb_id IN (SELECT unnest(?::BIGINT[]))",
            [ordered],
        )
    }
    # A film missing from the library can still be imported: Letterboxd
    # matches on tmdbID exactly, and the title is only for humans.
    films = [
        known.get(tmdb_id)
        or {"tmdb_id": tmdb_id, "imdb_id": None, "title": None, "year": None, "directors": None}
        for tmdb_id in ordered
    ]
    return BuiltList(
        films=films,
        not_in_library=sum(1 for tmdb_id in ordered if tmdb_id not in known),
        description=["custom SQL query"],
    )


# ------------------------------------------------------------------ writing

def _row(film: dict[str, Any]) -> list[Any]:
    directors = film.get("directors") or []
    return [
        film["tmdb_id"],
        film.get("imdb_id") or "",
        film.get("title") or "",
        film["year"] if film.get("year") is not None else "",
        ", ".join(directors),
    ]


def _encode(values: list[Any]) -> str:
    """One CSV line in Letterboxd's dialect.

    Text is always quoted and internal quotes are backslash-escaped, as the
    importer documents -- not doubled, which is what `csv` does by default.
    """
    buffer = io.StringIO()
    csv.writer(
        buffer,
        quoting=csv.QUOTE_NONNUMERIC,
        doublequote=False,
        escapechar="\\",
        lineterminator="\n",
    ).writerow(values)
    return buffer.getvalue()


def write_import_files(films: list[dict[str, Any]], output: Path) -> list[Path]:
    """Write `films` to one or more importable CSVs, splitting at 1MB.

    Returns the paths written, in import order.
    """
    if output.suffix.lower() != ".csv":
        # Guards against pointing this at, say, your export .zip and
        # overwriting it.
        raise InvalidFilterError(f"The output file must end in .csv, not {output.name!r}.")

    header = _encode(list(IMPORT_COLUMNS))
    chunks: list[list[str]] = [[]]
    size = len(header.encode("utf-8"))

    for film in films:
        line = _encode(_row(film))
        line_size = len(line.encode("utf-8"))
        if chunks[-1] and size + line_size > MAX_FILE_BYTES:
            chunks.append([])
            size = len(header.encode("utf-8"))
        chunks[-1].append(line)
        size += line_size

    output.parent.mkdir(parents=True, exist_ok=True)
    if len(chunks) == 1:
        paths = [output]
    else:
        paths = [
            output.with_name(f"{output.stem}-{index}{output.suffix}")
            for index in range(1, len(chunks) + 1)
        ]

    for path, lines in zip(paths, chunks):
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(header)
            handle.writelines(lines)
    return paths


def build_list(
    output: Path,
    filters: ListFilters | None = None,
    sql: str | None = None,
) -> dict[str, Any]:
    """Build a list by filters or by SQL, and write it for import."""
    filters = filters or ListFilters()
    if sql is not None and not filters.is_default():
        raise InvalidFilterError(
            "Use either a SQL query or filters, not both. Put the conditions "
            "in the query instead."
        )

    built = build_from_sql(sql) if sql is not None else build_from_filters(filters)
    paths = write_import_files(built.films, Path(output))

    preview = [
        f"{film['title']} ({film['year']})" if film.get("title") else f"TMDB {film['tmdb_id']}"
        for film in built.films[:10]
    ]
    report: dict[str, Any] = {
        "films": len(built.films),
        "built_from": "; ".join(built.description),
        "files": [str(path) for path in paths],
        "first_films": preview,
    }
    if built.not_in_library:
        report["not_in_library"] = built.not_in_library
    if len(paths) > 1:
        report["note"] = (
            "Letterboxd limits imports to 1MB, so the list is split. Import the "
            "files into the same list in order."
        )
    return report
