"""Schema and connection behaviour."""

from __future__ import annotations

import pytest

from letterboxd_utility_tools.core import db


def test_schema_is_idempotent(empty_conn):
    db.init_schema(empty_conn)
    db.init_schema(empty_conn)  # would raise if the DDL were not IF NOT EXISTS

    tables = {
        row[0]
        for row in empty_conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert {"films", "diary_entries", "lists", "list_entries", "profile"} <= tables


def test_list_columns_round_trip_as_python_lists(conn):
    rows = db.query("SELECT genres FROM films WHERE tmdb_id = 694")
    assert rows[0]["genres"] == ["Horror", "Thriller"]


def test_list_contains_filters_without_string_splitting(conn):
    rows = db.query(
        "SELECT title FROM films WHERE list_contains(genres, 'Horror') ORDER BY title"
    )
    assert [r["title"] for r in rows] == ["Alien", "The Others", "The Shining"]


def test_watch_stats_derive_from_diary_entries(conn):
    conn.executemany(
        """
        INSERT INTO diary_entries (tmdb_id, watched_date, logged_date, rating, rewatch)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (27205, "2020-01-05", "2020-01-05", 4.5, False),
            (27205, "2023-07-11", "2023-07-12", 5.0, True),
        ],
    )

    stats = db.query("SELECT * FROM film_watch_stats WHERE tmdb_id = 27205")[0]
    assert stats["watch_count"] == 2
    assert stats["rewatch_count"] == 1
    assert str(stats["first_watched_date"]) == "2020-01-05"
    assert str(stats["last_watched_date"]) == "2023-07-11"


def test_diary_entry_for_unknown_film_is_rejected(conn):
    with pytest.raises(Exception, match="Constraint|constraint"):
        conn.execute(
            "INSERT INTO diary_entries (tmdb_id, watched_date, logged_date) "
            "VALUES (999999, '2024-01-01', '2024-01-01')"
        )


def test_duplicate_watch_is_rejected(conn):
    conn.execute(
        "INSERT INTO diary_entries (tmdb_id, watched_date, logged_date) "
        "VALUES (27205, '2024-01-01', '2024-01-02')"
    )
    with pytest.raises(Exception, match="Constraint|constraint"):
        conn.execute(
            "INSERT INTO diary_entries (tmdb_id, watched_date, logged_date) "
            "VALUES (27205, '2024-01-01', '2024-01-02')"
        )


def test_reopening_a_different_database_requires_closing(conn):
    with pytest.raises(RuntimeError, match="already open"):
        db.get_connection("some/other.duckdb")
