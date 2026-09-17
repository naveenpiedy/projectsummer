"""Describing the library's schema, a little at a time.

An agent writing SQL needs three different things, and paying for all three
up front would spend thousands of tokens on columns it never touches:

1. **Which tables exist** and what each is for -- enough to pick one.
2. **One table's columns**, with types and what they mean.
3. **What one column actually holds.** Types say `genres` is a list of text;
   only the data says TMDB spells it "Science Fiction", not "Sci-Fi". A
   filter written from a guess returns no rows and no error, which reads as
   "you have watched no science fiction". So this level shows the most
   common values, or searches for a specific one.

Descriptions come from `COMMENT ON` statements in schema.sql, read back from
DuckDB's catalog, so there is one copy of them and it lives beside the
schema.
"""

from __future__ import annotations

import difflib
import re
from typing import Literal

from projectsummer.core import db
from projectsummer.core.errors import LetterboxdError, NoResultError
from projectsummer.core.querying import Scalar, to_scalar
from projectsummer.core.results import Result

#: The tables worth querying, in the order worth reading about them.
PUBLIC_RELATIONS = (
    "films",
    "diary_entries",
    "film_watch_stats",
    "people",
    "film_credits",
    "lists",
    "list_entries",
    "profile",
)

#: Pipeline machinery. Queryable, but a wrong place to look for answers:
#: `staging_films` looks like `films` and holds pre-enrichment data.
INTERNAL_RELATIONS = frozenset(
    {
        "staging_films",
        "staging_diary",
        "film_identity",
        "film_credit_fetches",
        "sync_state",
        "integrity_orphans",
    }
)

TOP_VALUES = 10
SEARCH_RESULTS = 20

#: Element types where a minimum and maximum mean something.
_ORDERED_TYPE = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|"
    r"UBIGINT|FLOAT|DOUBLE|DECIMAL.*|DATE|TIMESTAMP.*)$"
)


class InvalidArgumentError(LetterboxdError):
    """Arguments that cannot be combined, or a value out of range."""


# ------------------------------------------------------------------ results

class TableSummary(Result):
    """A table or view, in brief."""

    name: str
    """The table's name, as used in SQL."""
    kind: Literal["table", "view"]
    """Whether it is a stored table or a view derived from others."""
    description: str | None
    """What the table holds and how it joins to the others."""
    rows: int
    """How many rows it holds now."""
    internal: bool
    """True for pipeline machinery, which is not where answers live."""


class ColumnSummary(Result):
    """A column, in brief."""

    name: str
    """The column's name, as used in SQL."""
    type: str
    """DuckDB type, e.g. VARCHAR, DATE, VARCHAR[] for a list."""
    is_list: bool
    """True for a list column: filter with list_contains(column, value), or
    unnest(column) to work with its items."""
    nullable: bool
    """Whether the column may be NULL."""
    description: str | None
    """What the column means."""


class TableDetail(TableSummary):
    """A table or view, with its columns."""

    columns: list[ColumnSummary]
    """Every column, in table order."""


class ValueCount(Result):
    """One value found in a column."""

    value: Scalar
    """The value, exactly as stored: use this spelling in a filter."""
    rows: int
    """Rows holding it. For a list column, rows whose list contains it."""


class ColumnValues(Result):
    """What a column actually holds."""

    table: str
    """The table."""
    column: str
    """The column."""
    type: str
    """DuckDB type."""
    is_list: bool
    """True for a list column; values are its individual items."""
    description: str | None
    """What the column means."""
    rows: int
    """Rows in the table."""
    rows_without_value: int
    """Rows where the column is NULL, or an empty list."""
    distinct_values: int
    """How many different values appear."""
    minimum: Scalar
    """Smallest value, for numbers and dates; otherwise null."""
    maximum: Scalar
    """Largest value, for numbers and dates; otherwise null."""
    search: str | None
    """The search text, if one was given."""
    matching_values: int | None
    """With a search: how many different values matched."""
    values: list[ValueCount]
    """Most common values first; with a search, values containing the text
    first, then close misspellings."""
    values_truncated: bool
    """True if more values exist than are shown."""


class SchemaDescription(Result):
    """Part of the library's schema, at the level of detail asked for."""

    level: Literal["tables", "columns", "values"]
    """Which of the three levels this is."""
    tables: list[TableSummary] | None
    """At the tables level: every table and view."""
    table: TableDetail | None
    """At the columns level: the table asked about."""
    column: ColumnValues | None
    """At the values level: the column asked about."""
    next_step: str
    """How to go one level deeper, or how to use what was found."""


# ------------------------------------------------------------------ describe

def describe(
    table: str | None = None,
    column: str | None = None,
    search: str | None = None,
    include_internal: bool = False,
) -> SchemaDescription:
    """Describe the schema at the level the arguments ask for."""
    if column is not None and table is None:
        raise InvalidArgumentError("Name the table as well as the column.")
    if search is not None and column is None:
        raise InvalidArgumentError(
            "search looks for values within one column; name the table and column too."
        )
    if search is not None and not re.sub(r"[\W_]", "", search):
        raise InvalidArgumentError(
            "search needs at least one letter or digit to look for."
        )

    if table is None:
        return SchemaDescription(
            level="tables",
            tables=list_tables(include_internal),
            table=None,
            column=None,
            next_step=(
                "Call describe_schema with table=<name> to see a table's columns. "
                "films, diary_entries and film_watch_stats answer most questions; "
                "people and film_credits answer questions about who made them."
            ),
        )

    detail = describe_table(table)
    if column is None:
        return SchemaDescription(
            level="columns",
            tables=None,
            table=detail,
            column=None,
            next_step=(
                "Call describe_schema with table and column to see the values a "
                "column holds -- worth doing before filtering on text, whose exact "
                "spelling matters. Add search=<text> to look for a specific value."
            ),
        )

    values = column_values(detail, column, search)
    usage = (
        f"list_contains({values.column}, <value>)" if values.is_list
        else f"{values.column} = <value>"
    )
    return SchemaDescription(
        level="values",
        tables=None,
        table=None,
        column=values,
        next_step=f"Filter with {usage}, using a value exactly as shown.",
    )


def list_tables(include_internal: bool = False) -> list[TableSummary]:
    """Every table and view, public ones first in reading order."""
    summaries = []
    for name, kind, description in _relations():
        internal = name in INTERNAL_RELATIONS
        if internal and not include_internal:
            continue
        summaries.append(
            TableSummary(
                name=name,
                kind=kind,
                description=description,
                rows=_count(name),
                internal=internal,
            )
        )

    order = {name: index for index, name in enumerate(PUBLIC_RELATIONS)}
    summaries.sort(key=lambda s: (s.internal, order.get(s.name, len(order)), s.name))
    return summaries


def describe_table(name: str) -> TableDetail:
    """One table or view, with its columns.

    Raises:
        NoResultError: If there is no such table, naming the closest ones.
    """
    relations = {rel_name: (kind, description) for rel_name, kind, description in _relations()}
    if name not in relations:
        raise NoResultError(
            f"No table or view named {name!r}.{_did_you_mean(name, relations)}"
        )

    kind, description = relations[name]
    columns = [
        ColumnSummary(
            name=row["column_name"],
            type=row["data_type"],
            is_list=row["data_type"].endswith("[]"),
            nullable=row["is_nullable"],
            description=row["comment"],
        )
        for row in db.query(
            """
            SELECT column_name, data_type, is_nullable, comment
            FROM duckdb_columns()
            WHERE database_name = current_database()
              AND schema_name = 'main'
              AND table_name = ?
            ORDER BY column_index
            """,
            [name],
        )
    ]
    return TableDetail(
        name=name,
        kind=kind,
        description=description,
        rows=_count(name),
        internal=name in INTERNAL_RELATIONS,
        columns=columns,
    )


def column_values(table: TableDetail, name: str, search: str | None = None) -> ColumnValues:
    """What one column holds: its most common values, or those matching a search.

    Raises:
        NoResultError: If the table has no such column, naming the closest.
    """
    columns = {column.name: column for column in table.columns}
    if name not in columns:
        raise NoResultError(
            f"{table.name} has no column named {name!r}.{_did_you_mean(name, columns)}"
        )
    column = columns[name]

    # Both names are now known to exist in the catalog, so quoting them into
    # the SQL cannot inject anything.
    source = _quote(table.name)
    field = _quote(column.name)
    element_type = column.type.removesuffix("[]")

    if column.is_list:
        values_cte = f"SELECT unnest({field}) AS value FROM {source}"
        empty = f"{field} IS NULL OR len({field}) = 0"
    else:
        values_cte = f"SELECT {field} AS value FROM {source}"
        empty = f"{field} IS NULL"

    totals = db.query(
        f"""
        SELECT count(*) AS rows, count(*) FILTER ({empty}) AS without_value
        FROM {source}
        """
    )[0]
    ordered = bool(_ORDERED_TYPE.match(element_type))
    spread = db.query(
        f"""
        WITH v AS ({values_cte})
        SELECT count(DISTINCT value) AS distinct_values,
               {"min(value)" if ordered else "NULL"} AS minimum,
               {"max(value)" if ordered else "NULL"} AS maximum
        FROM v
        """
    )[0]

    # A list holding the same value twice is still one row holding it.
    counted = (
        f"SELECT DISTINCT rowid AS row_id, unnest({field}) AS value FROM {source}"
        if column.is_list and table.kind == "table"
        else values_cte
    )
    matching: int | None = None
    if search is None:
        limit = TOP_VALUES
        found = db.query(
            f"""
            WITH v AS ({counted})
            SELECT value, count(*) AS rows
            FROM v
            WHERE value IS NOT NULL
            GROUP BY value
            ORDER BY rows DESC, value
            LIMIT ?
            """,
            [limit + 1],
        )
    else:
        limit = SEARCH_RESULTS
        # Text is compared as letters and digits only, in any alphabet, so
        # "ravi kumar" finds "K. S. Ravikumar". A misspelling is judged
        # against the whole value and against each word in it, so "kubrik"
        # finds "Stanley Kubrick" without the first name.
        found = db.query(
            f"""
            WITH wanted AS (SELECT {_letters_only("?")} AS text),
            v AS ({counted}),
            tallied AS (
                SELECT value, count(*) AS rows
                FROM v
                WHERE value IS NOT NULL
                GROUP BY value
            ),
            scored AS (
                SELECT c.value, c.rows,
                       contains({_letters_only("CAST(c.value AS VARCHAR)")}, w.text)
                           AS contains_text,
                       greatest(
                           jaro_winkler_similarity(
                               {_letters_only("CAST(c.value AS VARCHAR)")}, w.text),
                           coalesce(list_max(list_transform(
                               string_split_regex(
                                   lower(CAST(c.value AS VARCHAR)), '{_NOT_LETTERS}+'),
                               lambda word: jaro_winkler_similarity(word, w.text))), 0)
                       ) AS similarity
                FROM tallied c, wanted w
            )
            SELECT value, rows, count(*) OVER () AS matching
            FROM scored
            WHERE contains_text OR similarity >= 0.85
            -- Values containing the text, most common first; then near
            -- misspellings, closest first.
            ORDER BY contains_text DESC,
                     CASE WHEN contains_text THEN rows END DESC,
                     similarity DESC,
                     value
            LIMIT ?
            """,
            [search, limit + 1],
        )
        matching = found[0]["matching"] if found else 0

    return ColumnValues(
        table=table.name,
        column=column.name,
        type=column.type,
        is_list=column.is_list,
        description=column.description,
        rows=totals["rows"],
        rows_without_value=totals["without_value"],
        distinct_values=spread["distinct_values"],
        minimum=to_scalar(spread["minimum"]),
        maximum=to_scalar(spread["maximum"]),
        search=search,
        matching_values=matching,
        values=[
            ValueCount(value=to_scalar(row["value"]), rows=row["rows"])
            for row in found[:limit]
        ],
        values_truncated=len(found) > limit,
    )


# ------------------------------------------------------------------ helpers

def _relations() -> list[tuple[str, Literal["table", "view"], str | None]]:
    rows = db.query(
        """
        SELECT table_name AS name, 'table' AS kind, comment
        FROM duckdb_tables()
        WHERE database_name = current_database() AND schema_name = 'main'
        UNION ALL
        SELECT view_name, 'view', comment
        FROM duckdb_views()
        WHERE database_name = current_database() AND schema_name = 'main'
          AND NOT internal
        """
    )
    return [(row["name"], row["kind"], row["comment"]) for row in rows]


def _count(relation: str) -> int:
    return db.query(f"SELECT count(*) AS n FROM {_quote(relation)}")[0]["n"]


#: Regex class for anything that is not a letter or digit, in any alphabet.
_NOT_LETTERS = r"[^\p{L}\p{N}]"


def _letters_only(expression: str) -> str:
    """SQL lower-casing `expression` and dropping everything but letters and digits."""
    return f"regexp_replace(lower({expression}), '{_NOT_LETTERS}', '', 'g')"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _did_you_mean(name: str, known: object) -> str:
    close = difflib.get_close_matches(name, list(known), n=3, cutoff=0.5)
    return f" Did you mean: {', '.join(close)}?" if close else ""
