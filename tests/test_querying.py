"""Running queries someone else wrote: one SELECT, bounded, JSON-shaped."""

from __future__ import annotations

import json
import time

import pytest

from conftest import SAMPLE_FILMS
from projectsummer.core import db, querying
from projectsummer.core.plugins.explore import query
from projectsummer.core.querying import (
    InvalidQueryError,
    QueryTimeoutError,
    require_single_select,
    run_select,
)


# --------------------------------------------------------- one SELECT only

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT title FROM films",
        "WITH horror AS (SELECT * FROM films) SELECT count(*) FROM horror",
        "FROM films SELECT title",
        "select 1",
        # DuckDB parses these read-only conveniences as SELECTs too.
        "DESCRIBE films",
        "SUMMARIZE films",
        "PRAGMA database_list",
    ],
)
def test_select_shapes_are_accepted(sql):
    require_single_select(sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT 1; DROP TABLE films", "exactly one"),
        ("DELETE FROM films", "DELETE"),
        ("UPDATE films SET title = 'x'", "UPDATE"),
        ("COPY films TO 'out.csv'", "COPY"),
        ("PRAGMA enable_profiling", "PRAGMA"),
        ("SET threads = 1", "SET"),
        ("CALL start_ui()", "CALL"),
        ("INSTALL ui", "LOAD"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("ATTACH 'other.duckdb'", "ATTACH"),
        ("SELEC title FROM films", "does not parse"),
    ],
)
def test_anything_else_is_refused_before_it_runs(sql, message):
    with pytest.raises(InvalidQueryError, match=message):
        require_single_select(sql)


# ------------------------------------------------------------------ results

def test_rows_come_back_keyed_by_column(conn):
    result = run_select("SELECT title, year FROM films WHERE tmdb_id = 694")

    assert [(c.name, c.type) for c in result.columns] == [("title", "VARCHAR"), ("year", "INTEGER")]
    assert result.rows == [{"title": "The Shining", "year": 1980}]
    assert result.row_count == 1
    assert result.truncated is False
    assert result.note is None


def test_list_columns_stay_lists(conn):
    result = run_select("SELECT genres FROM films WHERE tmdb_id = 694")
    assert result.rows[0]["genres"] == ["Horror", "Thriller"]


def test_an_empty_result_still_describes_its_columns(conn):
    result = run_select("SELECT title FROM films WHERE false")
    assert [c.name for c in result.columns] == ["title"]
    assert result.rows == []


def test_rows_past_the_limit_are_cut_off_and_said_so(conn):
    result = run_select("SELECT title FROM films ORDER BY title", max_rows=2)

    assert result.row_count == 2
    assert result.truncated is True
    assert "first 2 rows" in result.note
    # The query's own ORDER BY is respected, not disturbed by the limit.
    assert [row["title"] for row in result.rows] == ["Alien", "Inception"]


def test_a_result_exactly_at_the_limit_is_not_truncated(conn):
    result = run_select("SELECT title FROM films", max_rows=len(SAMPLE_FILMS))
    assert result.truncated is False
    assert result.row_count == len(SAMPLE_FILMS)


@pytest.mark.parametrize("max_rows", [0, -1, querying.MAX_ROWS_CEILING + 1])
def test_max_rows_is_bounded(conn, max_rows):
    with pytest.raises(InvalidQueryError, match="max_rows"):
        run_select("SELECT 1", max_rows=max_rows)


def test_repeated_column_names_are_kept_apart(conn):
    result = run_select("SELECT 1 AS title, 2 AS title, 3 AS title_2, 4 AS title")
    assert [c.name for c in result.columns] == ["title", "title_2", "title_2_2", "title_3"]
    assert result.rows == [{"title": 1, "title_2": 2, "title_2_2": 3, "title_3": 4}]


def test_values_without_a_json_form_are_converted(conn):
    result = run_select(
        """
        SELECT 1.50::DECIMAL(4, 2)          AS price,
               INTERVAL 3 DAY               AS gap,
               '00000000-0000-0000-0000-000000000001'::UUID AS id,
               TIME '10:30'                 AS at,
               {'k': 1, 'v': 'x'}           AS pair,
               [{'k': 1}]                   AS nested,
               DATE '2024-01-10'            AS day
        """
    )
    row = result.rows[0]

    assert row["price"] == 1.5
    assert isinstance(row["gap"], str)
    assert row["id"] == "00000000-0000-0000-0000-000000000001"
    assert row["at"] == "10:30:00"
    assert row["pair"] == {"k": 1, "v": "x"}
    assert json.loads(row["nested"][0]) == {"k": 1}

    # And the whole result serialises, with dates as ISO text.
    assert json.loads(result.model_dump_json())["rows"][0]["day"] == "2024-01-10"


def test_a_query_duckdb_rejects_says_why(conn):
    with pytest.raises(InvalidQueryError, match="no_such_column"):
        run_select("SELECT no_such_column FROM films")


def test_a_runaway_query_is_stopped(conn):
    started = time.monotonic()
    with pytest.raises(QueryTimeoutError, match="stopped after"):
        run_select(
            "SELECT sum(a.range * b.range) FROM range(1000000) a, range(1000000) b",
            timeout=0.2,
        )
    assert time.monotonic() - started < 10

    # The connection is still usable afterwards.
    assert run_select("SELECT 42 AS answer").rows == [{"answer": 42}]


# ------------------------------------------------- over a read-only session

@pytest.fixture
def library(tmp_path):
    db.close_connection()
    path = tmp_path / "library.duckdb"
    with db.session(path) as connection:
        connection.executemany(
            """
            INSERT INTO films
                (tmdb_id, title, year, runtime, genres, directors, on_watchlist, watched)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            SAMPLE_FILMS,
        )
    yield path
    db.close_connection()


def test_a_query_reads_nothing_outside_the_library_when_read_only(library, tmp_path):
    """The parser allows `SELECT * FROM read_csv(...)` -- it is a SELECT. The
    read-only session is what stops it, which is why the MCP server uses one."""
    secret = tmp_path / "secret.csv"
    secret.write_text("token\nabc123\n", encoding="utf-8")

    with db.session(library, read_only=True):
        assert query("SELECT count(*) AS n FROM films").rows == [{"n": len(SAMPLE_FILMS)}]
        with pytest.raises(InvalidQueryError, match="(?i)permission|access"):
            query(f"SELECT * FROM read_csv('{secret.as_posix()}')")
