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
    DatabaseRecoveryError,
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


def test_a_writable_session_can_refuse_external_access(library, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("token\nabc123\n", encoding="utf-8")
    target = tmp_path / "leak.csv"

    with db.session(library, external_access=False):
        db.query("UPDATE films SET title = 'Still writable'")
        with pytest.raises(duckdb.Error):
            db.query(f"SELECT * FROM read_csv('{secret.as_posix()}')")
        with pytest.raises(duckdb.Error):
            db.query(f"COPY films TO '{target.as_posix()}'")
    assert not target.exists()


# ------------------------------------------------- surviving a killed process
#
# DuckDB 1.5 cannot replay a `COMMENT ON COLUMN` from its write-ahead log. A
# process killed before closing -- Ctrl+C, or a chat client restarting its MCP
# server -- used to leave a hundred of them there, because every read-write
# open re-applied schema.sql, and the library then refused to open at all.
# That happened to a real library; these tests reproduce it with a subprocess.

def _killed_mid_session(path, setup=""):
    """Run a child that opens a read-write session and dies without closing."""
    child = (
        "import os\n"
        "from projectsummer.core import db\n"
        f"with db.session(r'{path}', external_access=False) as conn:\n"
        f"    {setup or 'pass'}\n"
        "    os._exit(0)\n"
    )
    result = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _wal(path):
    return path.with_name(path.name + ".wal")


def test_an_everyday_open_writes_nothing_so_a_kill_leaves_nothing(library):
    _killed_mid_session(library)

    assert not _wal(library).exists()
    with db.session(library, read_only=True):
        assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == 1


def test_a_schema_refresh_is_checkpointed_before_a_kill_can_matter(library):
    raw = duckdb.connect(str(library))
    raw.execute("UPDATE sync_state SET value = 'older' WHERE key = 'schema_fingerprint'")
    raw.close()

    _killed_mid_session(library)  # re-applies schema.sql, then dies

    with db.session(library, read_only=True):
        recorded = db.query("SELECT value FROM sync_state WHERE key = 'schema_fingerprint'")
    assert recorded[0]["value"] != "older"


def test_schema_sql_is_only_reapplied_when_it_changes(library):
    raw = duckdb.connect(str(library))
    raw.execute("COMMENT ON TABLE films IS NULL")
    raw.close()

    with db.session(library):
        pass  # unchanged schema.sql: nothing re-applied
    with db.session(library, read_only=True):
        assert db.query("SELECT comment FROM duckdb_tables() WHERE table_name = 'films'")[0]["comment"] is None

    raw = duckdb.connect(str(library))
    raw.execute("UPDATE sync_state SET value = 'older' WHERE key = 'schema_fingerprint'")
    raw.close()
    with db.session(library):
        pass  # changed: re-applied
    with db.session(library, read_only=True):
        assert db.query("SELECT comment FROM duckdb_tables() WHERE table_name = 'films'")[0]["comment"]


def test_an_unreplayable_log_is_explained_with_how_to_recover(library):
    child = (
        "import duckdb, os\n"
        f"c = duckdb.connect(r'{library}')\n"
        "c.execute(\"COMMENT ON COLUMN films.title IS 'left in the log'\")\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", child], check=True)
    try:
        duckdb.connect(str(library)).close()
        pytest.skip("this DuckDB replays column comments; the bug these tests guard against is fixed")
    except duckdb.Error:
        pass

    for read_only in (False, True):
        with pytest.raises(DatabaseRecoveryError) as error:
            with db.session(library, read_only=read_only):
                pass
        message = str(error.value)
        assert f"{library.name}.wal" in message
        assert ".unreplayable" in message
