"""DuckDB connection management and schema bootstrap.

A user's whole library is one DuckDB file. This module owns the connection to
it so that plugins never have to think about paths: they call
:func:`get_connection` or :func:`query`, and get whichever connection the
frontend running them has arranged.

There are two arrangements:

* **The process-wide connection**, opened on first use and held until the
  process exits. Right for the CLI, where a process is one command.
* **A session** (:func:`session`), opened for one unit of work and closed
  straight after. Right for the MCP server, which lives as long as the chat
  client that started it.

The difference matters because of how DuckDB locks the file. While a process
holds a read-write connection, *no other process can open the file at all* --
not even read-only. (Several processes that all open read-only may share it.)
A long-lived server holding the process-wide connection would lock the user
out of `summer sync` for as long as their chat client stayed open. A session
holds the lock only while a tool call is actually running.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import duckdb

from projectsummer import config
from projectsummer.core.errors import (
    DatabaseBusyError,
    DatabaseRecoveryError,
    EmptyDatabaseError,
    LetterboxdError,
    NoDatabaseError,
)

#: Bumped whenever schema.sql changes in a way an existing database cannot
#: simply absorb. The DDL is all CREATE ... IF NOT EXISTS, so an existing
#: table is left exactly as it is -- a changed column would otherwise be
#: silently ignored, and the next statement referencing it would fail with an
#: incomprehensible binder error.
#:
#: 1: initial schema.
#: 2: staging tables + film_identity added; foreign keys removed (DuckDB
#:    rejects LIST-column updates on referenced rows inside a transaction).
SCHEMA_VERSION = "2"


class SchemaVersionError(LetterboxdError):
    """The database was built by an incompatible version of the schema."""


_SCHEMA_SQL = Path(__file__).with_name("schema.sql")

#: How far through the pipeline a database has got. See :func:`library_state`.
LibraryState = Literal["empty", "staged", "ready"]

#: Settings for every read-only connection. `read_only` alone stops writes to
#: the database, but not to the filesystem: `COPY films TO 'x.csv'` still
#: succeeds, and `read_csv` can read any file the user can. Disabling external
#: access closes both, along with ATTACH and extension loading, and locking the
#: configuration stops a query from simply turning it back on with SET.
READ_ONLY_CONFIG: dict[str, Any] = {
    "enable_external_access": False,
    "lock_configuration": True,
}

#: The same restriction for a connection that may write to the database. Used
#: wherever SQL from outside can reach a writable connection -- list_builder's
#: query over MCP, say -- because reading a file is enough to leak it: DuckDB
#: quotes a value it cannot convert in its error message.
NO_EXTERNAL_ACCESS_CONFIG: dict[str, Any] = READ_ONLY_CONFIG

_lock = threading.RLock()
_connection: duckdb.DuckDBPyConnection | None = None
_connection_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _Session:
    connection: duckdb.DuckDBPyConnection
    path: Path


#: The session active in the current context, if any. A ContextVar rather than
#: a global because the MCP server runs tool calls on worker threads: each
#: call must see its own connection, never one opened by a call running
#: alongside it. A thread started from inside a session does not inherit it,
#: and falls back to the process-wide connection.
_session: ContextVar[_Session | None] = ContextVar("projectsummer_db_session", default=None)

#: Serialises sessions within a process. DuckDB refuses to open one file twice
#: in the same process with different settings, so a read-only session and a
#: read-write one cannot overlap. Tool calls against a local library take
#: milliseconds, so taking turns costs nothing noticeable.
_session_lock = threading.Lock()


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create every table, index and view, if schema.sql has changed since last time.

    Safe to call on every open. It records a fingerprint of schema.sql and
    only re-applies the file when that fingerprint differs -- a new database,
    or an upgrade that brings new tables or descriptions -- so an everyday
    open writes nothing at all.

    That matters because of a DuckDB 1.5 bug. A `COMMENT ON COLUMN` left in
    the write-ahead log cannot be replayed: if a process is killed before
    closing -- Ctrl+C on a command, a chat client restarting its MCP server --
    the database refuses to open until the log is moved aside. Re-applying
    schema.sql on every open put a hundred such statements in the log every
    time. Now they are written only when something changed, and checkpointed
    into the main file straight away.

    Raises:
        SchemaVersionError: If the database was built by a different version
            of this schema. There is no migration tooling yet, and applying
            the current DDL over an older layout would half-work: new tables
            would appear while changed ones stayed as they were.
    """
    existing = schema_version(conn)
    if existing is not None:
        _require_current_version(existing)

    text = _SCHEMA_SQL.read_text(encoding="utf-8")
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if existing is not None and _recorded_fingerprint(conn) == fingerprint:
        return

    conn.execute(text)
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES
            ('schema_version', ?), ('schema_fingerprint', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                        updated_at = now()
        """,
        [SCHEMA_VERSION, fingerprint],
    )
    try:
        conn.execute("CHECKPOINT")
    except duckdb.Error:
        # Folding the log into the file is a safeguard, not a requirement:
        # the schema is applied either way, and a failed checkpoint must not
        # stop the library opening.
        pass


def _recorded_fingerprint(conn: duckdb.DuckDBPyConnection) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'schema_fingerprint'"
    ).fetchone()
    return row[0] if row else None


def _require_current_version(existing: str) -> None:
    if existing != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"This database uses schema version {existing}, but this version "
            f"of the tool expects {SCHEMA_VERSION}. There is no automatic "
            f"migration yet. Re-create it by deleting the file and running "
            f"ingestion again, or point LETTERBOXD_DB at a different path."
        )


def schema_version(conn: duckdb.DuckDBPyConnection) -> str | None:
    """Return the schema version a database was built with.

    Returns:
        The recorded version; ``"0"`` for a database that predates version
        tracking but already holds tables; or ``None`` for an empty database,
        which is free to become whatever the current schema says.
    """
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    if not tables:
        return None
    if "sync_state" not in tables:
        return "0"

    recorded = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'schema_version'"
    ).fetchone()
    return recorded[0] if recorded else "0"


def get_connection(
    path: str | Path | None = None,
    *,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Return the connection plugin code should use.

    Inside a :func:`session`, that is the session's connection. Otherwise it
    is the process-wide connection, opened on first use.

    Args:
        path: Database file to open. Defaults to :func:`config.db_path`.
            Pass ``":memory:"`` for an ephemeral database.
        read_only: Open without write access -- see :func:`session` for what
            that enforces. Ignored for in-memory databases, and ignored
            entirely if a connection is already open.

    Returns:
        The connection, with the schema already applied or checked.

    Raises:
        RuntimeError: If `path` names a different database from the one
            already open.
    """
    global _connection, _connection_path

    current = _session.get()
    if current is not None:
        if path is not None and Path(path) != current.path:
            raise RuntimeError(
                f"A session on {current.path} is active; cannot open {path} inside it."
            )
        return current.connection

    with _lock:
        target = Path(path) if path is not None else config.db_path()

        if _connection is not None:
            if path is not None and Path(target) != _connection_path:
                raise RuntimeError(
                    f"A connection to {_connection_path} is already open; "
                    f"call close_connection() before opening {target}."
                )
            return _connection

        in_memory = str(target) == ":memory:"
        _connection = _open(target, read_only=read_only and not in_memory)
        _connection_path = target
        return _connection


@contextmanager
def session(
    path: str | Path | None = None,
    *,
    read_only: bool = False,
    external_access: bool = True,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a connection for one unit of work, and close it afterwards.

    While the block runs, :func:`get_connection` and :func:`query` return this
    connection, so plugin code needs no changes to run inside one. Closing it
    releases DuckDB's file lock, so other processes can use the database
    between sessions.

    A read-only session is enforced by DuckDB rather than trusted to the code
    running in it: any statement that writes to the database fails, and so
    does anything that touches the filesystem -- `COPY ... TO`, `ATTACH`,
    `read_csv`, installing extensions.

        with db.session(read_only=True):
            rows = db.query("SELECT title FROM films")

    Args:
        path: Database file to open. Defaults to :func:`config.db_path`.
        read_only: Open without any ability to write. Implies no external
            access.
        external_access: Allow DuckDB to read and write files outside the
            database. Turn it off wherever the SQL run is not the user's own.

    Yields:
        The session's connection.

    Raises:
        NoDatabaseError: If `read_only` and the file does not exist. A
            read-write session creates it instead.
        EmptyDatabaseError: If `read_only` and nothing has been imported yet,
            so there are no tables to read.
        SchemaVersionError: If the database was built by an incompatible
            version of the schema.
        DatabaseBusyError: If another process is using the database.
        ValueError: If `read_only` is asked of an in-memory database, which
            would have nothing to read. Refused rather than ignored, because
            a silently writable "read-only" session is exactly what this
            function exists to rule out.
        RuntimeError: If a session is already active in this context, or the
            process-wide connection already holds the same file.
    """
    target = Path(path) if path is not None else config.db_path()
    if read_only and str(target) == ":memory:":
        raise ValueError("An in-memory database cannot be opened read-only.")
    if _session.get() is not None:
        raise RuntimeError("A database session is already active; sessions do not nest.")

    with _session_lock:
        with _lock:
            if _connection is not None and _connection_path == target:
                raise RuntimeError(
                    f"The process-wide connection already holds {target}; "
                    f"close it before opening a session on the same file."
                )

        conn = _open(target, read_only=read_only, external_access=external_access)
        token = _session.set(_Session(conn, target))
        try:
            yield conn
        finally:
            _session.reset(token)
            conn.close()


def _open(
    target: Path, *, read_only: bool, external_access: bool = True
) -> duckdb.DuckDBPyConnection:
    """Connect to `target` and make sure its schema is usable.

    A read-write connection applies the schema. A read-only one cannot, so it
    checks the recorded version instead.
    """
    in_memory = str(target) == ":memory:"
    if read_only and not target.exists():
        # DuckDB reports this as an IO error, indistinguishable from a lock
        # conflict, so check first.
        raise NoDatabaseError(
            f"No database at {target}. Import a Letterboxd export to create one."
        )
    if not in_memory and not read_only:
        target.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = duckdb.connect(
            str(target),
            read_only=read_only,
            config=(
                READ_ONLY_CONFIG if read_only
                else {} if external_access
                else NO_EXTERNAL_ACCESS_CONFIG
            ),
        )
    except duckdb.IOException as error:
        if in_memory or not target.exists():
            raise
        raise DatabaseBusyError(
            f"The database at {target} is in use by another process -- most "
            f"likely another `summer` command, or an MCP server in the middle "
            f"of a call. Try again once it has finished. ({error})"
        ) from error
    except duckdb.Error as error:
        if "replaying WAL" not in str(error):
            raise
        wal = target.with_name(target.name + ".wal")
        raise DatabaseRecoveryError(
            f"The database at {target} could not be opened: DuckDB failed to "
            f"replay {wal.name}, its record of changes not yet saved into the "
            f"main file. This happens when a process is stopped part-way "
            f"through writing. Everything saved before then is in the main "
            f"file. To recover, close every program using the library, rename "
            f"{wal} to {wal.name}.unreplayable, and open it again. Keep the "
            f"renamed file until you have checked nothing recent is missing. "
            f"({str(error).splitlines()[0]})"
        ) from error

    try:
        if read_only:
            existing = schema_version(conn)
            if existing is None:
                raise EmptyDatabaseError(
                    "No data in this database yet. Import a Letterboxd export "
                    "first, then run enrichment."
                )
            _require_current_version(existing)
        else:
            init_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def close_connection() -> None:
    """Close the shared connection, if one is open.

    Mostly useful in tests and when switching databases.
    """
    global _connection, _connection_path

    with _lock:
        if _connection is not None:
            _connection.close()
        _connection = None
        _connection_path = None


def database_path() -> Path | None:
    """Where the open connection points, or None if nothing is open yet."""
    current = _session.get()
    if current is not None:
        return current.path
    with _lock:
        return _connection_path


def library_state() -> LibraryState:
    """How far through the pipeline this database has got.

    Returns:
        ``"empty"`` if nothing has been imported, ``"staged"`` if an export
        was ingested but not yet enriched (so `films` is still empty), or
        ``"ready"`` if `films` is populated.
    """
    with _lock:
        conn = get_connection()
        if conn.execute("SELECT count(*) FROM films").fetchone()[0]:
            return "ready"
        if conn.execute("SELECT count(*) FROM staging_films").fetchone()[0]:
            return "staged"
        return "empty"


def require_films() -> None:
    """Fail with an accurate next step if `films` has nothing queryable in it.

    Telling someone to run ingestion when they have just run it successfully
    is worse than saying nothing, so the two unfinished states are reported
    differently.

    Raises:
        EmptyDatabaseError: If no enriched films are available.
    """
    state = library_state()
    if state == "ready":
        return
    if state == "staged":
        raise EmptyDatabaseError(
            "Your export has been imported but not enriched yet, so no film "
            "metadata is available. Run enrichment to resolve films against "
            "TMDB."
        )
    raise EmptyDatabaseError(
        "No data in this database yet. Import a Letterboxd export first, "
        "then run enrichment."
    )


def connection_lock() -> threading.RLock:
    """The lock guarding the process-wide connection.

    For code that needs a cursor for longer than :func:`query` holds one --
    reading a result a few rows at a time, say.
    """
    return _lock


def query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Run a query on the shared connection and return rows as dictionaries.

    Dictionaries, rather than tuples, because these results flow straight out
    to a CLI table or an LLM tool call, where column names carry the meaning.
    """
    with _lock:
        cursor = get_connection().execute(sql, params or [])
        if cursor.description is None:
            return []
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
