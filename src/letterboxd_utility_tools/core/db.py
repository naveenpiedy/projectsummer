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

#: Bumped whenever schema.sql changes in a way that needs a migration.
SCHEMA_VERSION = "1"

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")

_lock = threading.RLock()
_connection: duckdb.DuckDBPyConnection | None = None
_connection_path: Path | None = None


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create every table, index and view if it does not already exist.

    Idempotent: safe to run against a fresh file or a fully populated one.
    """
    conn.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES ('schema_version', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                        updated_at = now()
        """,
        [SCHEMA_VERSION],
    )


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
