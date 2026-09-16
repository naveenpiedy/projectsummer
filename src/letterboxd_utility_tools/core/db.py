"""DuckDB connection management and schema bootstrap.

A user's whole library is one DuckDB file. This module owns the single
connection to it so that plugins never have to think about paths, and so the
CLI and the MCP server share exactly the same handle.

DuckDB allows only one read-write process per database file. Opening a second
writer (e.g. running the MCP server while the CLI ingests) raises an IO error;
read-only connections may be shared freely. `read_only=True` is the right
choice for anything that only queries.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import duckdb

from letterboxd_utility_tools import config
from letterboxd_utility_tools.core.errors import EmptyDatabaseError, LetterboxdError

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

_lock = threading.RLock()
_connection: duckdb.DuckDBPyConnection | None = None
_connection_path: Path | None = None


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create every table, index and view if it does not already exist.

    Idempotent for a database already at :data:`SCHEMA_VERSION`, so it is safe
    to run on every open.

    Raises:
        SchemaVersionError: If the database was built by a different version
            of this schema. There is no migration tooling yet, and applying
            the current DDL over an older layout would half-work: new tables
            would appear while changed ones stayed as they were.
    """
    existing = schema_version(conn)
    if existing is not None and existing != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"This database uses schema version {existing}, but this version "
            f"of the tool expects {SCHEMA_VERSION}. There is no automatic "
            f"migration yet. Re-create it by deleting the file and running "
            f"ingestion again, or point LETTERBOXD_DB at a different path."
        )

    conn.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES ('schema_version', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                        updated_at = now()
        """,
        [SCHEMA_VERSION],
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
    """Return the process-wide DuckDB connection, opening it on first use.

    Args:
        path: Database file to open. Defaults to :func:`config.db_path`.
            Pass ``":memory:"`` for an ephemeral database.
        read_only: Open without write access. Lets several processes read the
            same file concurrently. Ignored for in-memory databases, and
            ignored entirely if a connection is already open.

    Returns:
        The shared connection, with the schema already applied.
    """
    global _connection, _connection_path

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
        if not in_memory:
            target.parent.mkdir(parents=True, exist_ok=True)

        conn = duckdb.connect(str(target), read_only=read_only and not in_memory)
        if not read_only or in_memory:
            init_schema(conn)

        _connection = conn
        _connection_path = target
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
    with _lock:
        return _connection_path


def library_state() -> str:
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
