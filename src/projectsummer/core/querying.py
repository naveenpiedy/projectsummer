"""Running a query someone else wrote: a person at the CLI, or an agent.

Three things make that different from the queries the rest of the package
runs, and each has one home here:

* **Only a single SELECT is accepted**, as judged by DuckDB's own parser.
  Inspecting the text for keywords would miss `SELECT 1; DROP TABLE films` or
  a `DELETE ... RETURNING`; the parser does not. Over MCP this is the second
  line of defence -- the session is read-only, so DuckDB refuses writes
  regardless -- but at the CLI it is the only one.
* **Results are bounded**, in rows, in size and in time. A result that
  floods an agent's context is as unhelpful as one that never arrives, and an
  accidental cross join would otherwise hang the MCP server and the chat
  client with it. Rows alone are not enough: a hundred rows of
  `SELECT * FROM films` is some 50k tokens, a well-formed aggregate a few
  hundred.
* **Every value is JSON-shaped.** DuckDB hands back Decimals, UUIDs,
  intervals and bytes, none of which a tool's output schema can promise.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

from projectsummer.core import db
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.results import Result

#: A value a result cell may hold directly.
Scalar = str | int | float | bool | date | datetime | None

#: A result cell: a value, a list column, or a struct column. Anything nested
#: deeper is rendered as JSON text rather than promised structurally.
Cell = Scalar | list[Scalar] | dict[str, Scalar]

DEFAULT_MAX_ROWS = 100
MAX_ROWS_CEILING = 1000

#: Most characters of JSON a result's rows may take up, about 5k tokens. Not
#: an argument: an agent that could raise it would, and the point is to steer
#: it towards selecting fewer columns instead.
MAX_RESULT_CHARS = 20_000

#: How long a query may run before it is interrupted.
QUERY_TIMEOUT_SECONDS = 30.0


class InvalidQueryError(LetterboxdError):
    """A query is not a single SELECT, or DuckDB could not run it."""


class QueryTimeoutError(LetterboxdError):
    """A query ran past the time limit and was interrupted."""


def require_single_select(sql: str) -> None:
    """Accept exactly one statement, and only a SELECT.

    DuckDB's FROM-first syntax (`FROM films`) and CTEs (`WITH ... SELECT`)
    both parse as SELECT, so they are allowed. `EXPLAIN`, `PRAGMA`, `COPY`
    and everything that writes are not.

    Raises:
        InvalidQueryError: If the text does not parse, holds more than one
            statement, or its statement is not a SELECT.
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
            f"Only a SELECT can be run; this is a {statements[0].type.name}."
        )


class QueryColumn(Result):
    """A column in a query's result."""

    name: str
    """The key this column's values have in each row. A name repeated in the
    query gets a numeric suffix, e.g. title and title_2."""
    type: str
    """DuckDB type, e.g. VARCHAR, DOUBLE, VARCHAR[] for a list."""


class QueryResult(Result):
    """The rows a query returned, up to the row limit."""

    columns: list[QueryColumn]
    """The result's columns, in query order."""
    rows: list[dict[str, Cell]]
    """One object per row, keyed by column name. Lists stay lists, dates are
    ISO 8601, and values with no JSON form (intervals, UUIDs) are text."""
    row_count: int
    """How many rows are included here."""
    truncated: bool
    """True if rows were left out, because the query returned more than
    max_rows or the rows ran past the size limit."""
    note: str | None
    """Why a result was truncated, and what to do about it."""


def run_select(
    sql: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout: float | None = None,
) -> QueryResult:
    """Run a single SELECT, returning at most `max_rows` rows.

    Rows also stop once they would take the result past
    :data:`MAX_RESULT_CHARS` of JSON, though the first row is always kept so a
    result is never empty for being wide.

    One row past the limit is read to learn whether there were more, rather
    than wrapping the query in a LIMIT: wrapping a query in a subquery is not
    guaranteed to keep its ORDER BY.

    Args:
        sql: The query. Checked by :func:`require_single_select` first.
        max_rows: Most rows to return, from 1 to :data:`MAX_ROWS_CEILING`.
        timeout: Seconds before the query is interrupted. Defaults to
            :data:`QUERY_TIMEOUT_SECONDS`.

    Raises:
        InvalidQueryError: If the query is not a single SELECT, `max_rows` is
            out of range, or DuckDB rejects the query.
        QueryTimeoutError: If the query runs past the time limit.
    """
    if not 1 <= max_rows <= MAX_ROWS_CEILING:
        raise InvalidQueryError(
            f"max_rows must be between 1 and {MAX_ROWS_CEILING}, not {max_rows}."
        )
    require_single_select(sql)

    limit = QUERY_TIMEOUT_SECONDS if timeout is None else timeout
    conn = db.get_connection()
    timer = threading.Timer(limit, conn.interrupt)
    timer.daemon = True

    with db.connection_lock():
        timer.start()
        try:
            cursor = conn.execute(sql)
            raw = cursor.fetchmany(max_rows + 1)
            description = cursor.description or []
        except duckdb.InterruptException:
            raise QueryTimeoutError(
                f"The query was stopped after {limit:g} seconds. Narrow it -- "
                f"filter earlier, aggregate, or check for a join missing its "
                f"ON condition."
            ) from None
        except duckdb.Error as error:
            raise InvalidQueryError(f"The query failed: {error}") from None
        finally:
            timer.cancel()

    names = _unique_names([column[0] for column in description])
    rows: list[dict[str, Cell]] = []
    size = 0
    too_large = False
    for row in raw[:max_rows]:
        cells = {name: to_cell(value) for name, value in zip(names, row)}
        size += len(json.dumps(cells, default=str, ensure_ascii=False))
        if rows and size > MAX_RESULT_CHARS:
            too_large = True
            break
        rows.append(cells)
    truncated = too_large or len(raw) > max_rows
    return QueryResult(
        columns=[
            QueryColumn(name=name, type=str(column[1]))
            for name, column in zip(names, description)
        ],
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        note=_truncation_note(len(rows), max_rows, too_large, truncated),
    )


def _truncation_note(
    shown: int, max_rows: int, too_large: bool, truncated: bool
) -> str | None:
    if too_large:
        return (
            f"Only the first {shown} rows are shown: the rest would pass the "
            f"{MAX_RESULT_CHARS:,}-character limit on a result. Select only "
            f"the columns you need rather than *, leave out long text such as "
            f"overview and review, or aggregate to get the answer directly."
        )
    if truncated:
        return (
            f"Only the first {max_rows} rows are shown. Aggregate or filter to "
            f"get the answer directly, rather than reading rows; max_rows goes "
            f"up to {MAX_ROWS_CEILING}."
        )
    return None


def _unique_names(names: list[str]) -> list[str]:
    """Suffix repeated column names, so no value is lost to a dict key clash.

    `SELECT a.title, b.title` returns two columns both called `title`.
    """
    used: set[str] = set()
    unique = []
    for name in names:
        candidate, number = name, 1
        while candidate in used:
            number += 1
            candidate = f"{name}_{number}"
        used.add(candidate)
        unique.append(candidate)
    return unique


def to_cell(value: Any) -> Cell:
    """Shape one DuckDB value into something a JSON schema can describe."""
    if isinstance(value, list):
        return [to_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_scalar(item) for key, item in value.items()}
    return to_scalar(value)


def to_scalar(value: Any) -> Scalar:
    """Shape a value that must not be a container."""
    if value is None or isinstance(value, (str, bool, int, float, date)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str)
    return str(value)
