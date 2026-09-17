"""Sessions: a connection per unit of work, and read-only enforced by DuckDB.

The MCP server outlives any single tool call, so it cannot hold the process-
wide connection: DuckDB would keep the file locked against every other
process for as long as the user's chat client stayed open. These tests pin
down the two properties the server relies on -- the lock is released between
sessions, and a read-only session cannot write anything, anywhere, no matter
what SQL it is handed.
"""

from __future__ import annotations

import subprocess
import sys
import threading

import duckdb
import pytest

from projectsummer.core import db
from projectsummer.core.errors import (
    DatabaseBusyError,
    EmptyDatabaseError,
    NoDatabaseError,
)


@pytest.fixture(autouse=True)
def no_process_wide_connection():
    db.close_connection()
    yield
    db.close_connection()


@pytest.fixture
def library(tmp_path):
    """A database file with the schema applied and one film in it."""
    path = tmp_path / "library.duckdb"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO films (tmdb_id, title, year) VALUES (27205, 'Inception', 2010)"
        )
    return path


# ------------------------------------------------------------------ lifecycle

def test_plugin_code_inside_a_session_uses_the_session_connection(library):
    with db.session(library, read_only=True) as conn:
        assert db.get_connection() is conn
        assert db.database_path() == library
        assert db.query("SELECT title FROM films") == [{"title": "Inception"}]


def test_the_lock_is_released_when_the_session_ends(library):
    with db.session(library):
        pass

    # Only possible if the session's read-write connection is really closed.
    other = subprocess.run(
        [sys.executable, "-c", f"import duckdb; duckdb.connect(r'{library}').close()"],
        capture_output=True,
        text=True,
    )
    assert other.returncode == 0, other.stderr


def test_the_session_closes_even_when_the_work_fails(library):
    with pytest.raises(ZeroDivisionError):
        with db.session(library) as conn:
            1 / 0

    with pytest.raises(duckdb.ConnectionException):
        conn.execute("SELECT 1")
    assert db.database_path() is None


def test_nothing_leaks_out_of_a_session(library):
    with db.session(library, read_only=True):
        pass
    assert db.database_path() is None


def test_a_read_write_session_creates_the_database_and_schema(tmp_path):
    path = tmp_path / "nested" / "new.duckdb"
    with db.session(path):
        assert db.library_state() == "empty"
    assert path.exists()


def test_sessions_do_not_nest(library):
    with db.session(library, read_only=True):
        with pytest.raises(RuntimeError, match="do not nest"):
            with db.session(library, read_only=True):
                pass


def test_a_session_refuses_a_file_the_process_wide_connection_holds(library):
    db.get_connection(library)
    with pytest.raises(RuntimeError, match="process-wide"):
        with db.session(library):
            pass


def test_another_thread_does_not_see_this_threads_session(library):
    seen = []
    with db.session(library, read_only=True):
        worker = threading.Thread(target=lambda: seen.append(db.database_path()))
        worker.start()
        worker.join()
    assert seen == [None]


# ------------------------------------------------------------ opening failures

def test_read_only_on_a_missing_file_says_so_and_creates_nothing(tmp_path):
    path = tmp_path / "missing.duckdb"
    with pytest.raises(NoDatabaseError, match="No database"):
        with db.session(path, read_only=True):
            pass
    assert not path.exists()


def test_read_only_on_a_database_never_imported_into(tmp_path):
    path = tmp_path / "blank.duckdb"
    duckdb.connect(str(path)).close()  # a valid file with no tables

    with pytest.raises(EmptyDatabaseError, match="Import a Letterboxd export"):
        with db.session(path, read_only=True):
            pass


def test_read_only_still_checks_the_schema_version(tmp_path):
    path = tmp_path / "old.duckdb"
    raw = duckdb.connect(str(path))
    raw.execute("CREATE TABLE films (tmdb_id BIGINT PRIMARY KEY)")
    raw.close()

    with pytest.raises(db.SchemaVersionError):
        with db.session(path, read_only=True):
            pass


def test_read_only_in_memory_is_refused_rather_than_silently_writable():
    with pytest.raises(ValueError, match="in-memory"):
        with db.session(":memory:", read_only=True):
            pass


def test_a_database_held_by_another_process_is_reported_as_busy(library):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time\n"
            f"c = duckdb.connect(r'{library}')\n"
            "print('holding', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "holding"
        for read_only in (True, False):
            with pytest.raises(DatabaseBusyError, match="in use by another process"):
                with db.session(library, read_only=read_only):
                    pass
    finally:
        holder.kill()
        holder.wait()


# ----------------------------------------------------- read-only is enforced
#
# Each of these is SQL a `query` tool could be handed. None may succeed, and
# the refusal has to come from DuckDB itself, not from a check in our code
# that a cleverly written statement might get past.

@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO films (tmdb_id, title) VALUES (1, 'x')",
        "UPDATE films SET title = 'x'",
        "DELETE FROM films",
        "DROP TABLE films",
        "CREATE TABLE sneaky (x INT)",
    ],
)
def test_a_read_only_session_cannot_change_the_database(library, statement):
    with db.session(library, read_only=True):
        with pytest.raises(duckdb.Error):
            db.query(statement)

    with db.session(library, read_only=True):
        assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == 1


def test_a_read_only_session_cannot_write_files(library, tmp_path):
    target = tmp_path / "leak.csv"
    with db.session(library, read_only=True):
        with pytest.raises(duckdb.Error):
            db.query(f"COPY films TO '{target.as_posix()}'")
    assert not target.exists()


def test_a_read_only_session_cannot_read_arbitrary_files(library, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("token\nabc123\n", encoding="utf-8")
    with db.session(library, read_only=True):
        with pytest.raises(duckdb.Error):
            db.query(f"SELECT * FROM read_csv('{secret.as_posix()}')")


def test_a_read_only_session_cannot_attach_another_database(library, tmp_path):
    other = tmp_path / "other.duckdb"
    with db.session(library, read_only=True):
        with pytest.raises(duckdb.Error):
            db.query(f"ATTACH '{other.as_posix()}' AS other")
    assert not other.exists()


def test_a_read_only_session_cannot_switch_its_restrictions_off(library):
    with db.session(library, read_only=True):
        with pytest.raises(duckdb.Error, match="locked"):
            db.query("SET enable_external_access = true")
