"""Schema and connection behaviour."""

from __future__ import annotations

import duckdb
import pytest

from projectsummer.core import db
from projectsummer.core.errors import EmptyDatabaseError


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


def test_orphan_diary_entry_is_reported_by_the_integrity_view(conn):
    """Foreign keys would have blocked this, but DuckDB cannot have them here.

    Updating a LIST column on a referenced row fails inside a transaction, and
    `films` is both the parent of everything and made of LIST columns. So the
    check is a view that surfaces dangling references instead.
    """
    assert db.query("SELECT * FROM integrity_orphans") == []

    conn.execute(
        "INSERT INTO diary_entries (tmdb_id, watched_date, logged_date) "
        "VALUES (999999, '2024-01-01', '2024-01-01')"
    )

    orphans = db.query("SELECT * FROM integrity_orphans")
    assert len(orphans) == 1
    assert orphans[0]["source_table"] == "diary_entries"
    assert orphans[0]["missing_value"] == 999999


def test_updating_a_list_column_works_without_foreign_keys(conn):
    """The exact operation that forced the foreign keys out: enrichment
    refreshing a film's genres while diary entries reference it."""
    conn.execute(
        "INSERT INTO diary_entries (tmdb_id, watched_date, logged_date) "
        "VALUES (27205, '2024-01-01', '2024-01-01')"
    )
    conn.execute("BEGIN TRANSACTION")
    conn.execute("UPDATE films SET genres = ['Action', 'Thriller'] WHERE tmdb_id = 27205")
    conn.execute("COMMIT")

    assert db.query("SELECT genres FROM films WHERE tmdb_id = 27205")[0]["genres"] == [
        "Action",
        "Thriller",
    ]


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


# ------------------------------------------------------------ schema version
#
# Every DDL statement is CREATE ... IF NOT EXISTS, so an existing table is
# left untouched. Applying a newer schema over an older database would half
# work: new tables appear, changed ones silently do not, and the failure
# surfaces later as an unintelligible binder error about a missing column.

def test_a_fresh_database_records_the_current_version(empty_conn):
    assert db.schema_version(empty_conn) == db.SCHEMA_VERSION


def test_an_empty_database_has_no_version_yet():
    raw = duckdb.connect(":memory:")
    assert db.schema_version(raw) is None


def test_a_database_predating_version_tracking_reports_zero():
    raw = duckdb.connect(":memory:")
    raw.execute("CREATE TABLE films (tmdb_id BIGINT PRIMARY KEY)")
    assert db.schema_version(raw) == "0"


def test_opening_an_older_database_is_refused_with_a_clear_message():
    raw = duckdb.connect(":memory:")
    raw.execute("CREATE TABLE films (tmdb_id BIGINT PRIMARY KEY)")

    with pytest.raises(db.SchemaVersionError) as error:
        db.init_schema(raw)

    message = str(error.value)
    assert "schema version 0" in message
    assert db.SCHEMA_VERSION in message
    assert "delet" in message.lower()  # tells the user what to actually do


def test_a_database_at_the_current_version_reinitialises_cleanly(empty_conn):
    db.init_schema(empty_conn)
    db.init_schema(empty_conn)
    assert db.schema_version(empty_conn) == db.SCHEMA_VERSION


# -------------------------------------------------------------- library state

def test_library_state_is_empty_before_anything_is_imported(empty_conn):
    assert db.library_state() == "empty"


def test_library_state_is_staged_after_ingestion_before_enrichment(empty_conn):
    empty_conn.execute(
        "INSERT INTO staging_films (letterboxd_uri, name, year) "
        "VALUES ('https://boxd.it/x', 'Solaris', 1972)"
    )
    assert db.library_state() == "staged"


def test_library_state_is_ready_once_films_are_populated(conn):
    assert db.library_state() == "ready"
    db.require_films()  # must not raise


def test_require_films_names_the_right_next_step(empty_conn):
    with pytest.raises(EmptyDatabaseError, match="Import a Letterboxd export"):
        db.require_films()

    empty_conn.execute(
        "INSERT INTO staging_films (letterboxd_uri, name, year) "
        "VALUES ('https://boxd.it/x', 'Solaris', 1972)"
    )
    with pytest.raises(EmptyDatabaseError, match="not enriched yet"):
        db.require_films()
