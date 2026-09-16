"""Shared fixtures.

Every test runs against an in-memory DuckDB seeded with a handful of synthetic
films -- no personal library data is needed to exercise the code.
"""

from __future__ import annotations

import pytest

from export_fixture import write_export
from projectsummer.core import db

# tmdb_id, title, year, runtime, genres, directors, on_watchlist, watched
SAMPLE_FILMS = [
    (27205, "Inception", 2010, 148, ["Action", "Science Fiction"], ["Christopher Nolan"], False, True),
    (694, "The Shining", 1980, 144, ["Horror", "Thriller"], ["Stanley Kubrick"], True, False),
    (11324, "Shutter Island", 2010, 138, ["Thriller", "Mystery"], ["Martin Scorsese"], True, False),
    (1026, "The Others", 2001, 101, ["Horror", "Mystery"], ["Alejandro Amenabar"], True, False),
    (348, "Alien", 1979, 117, ["Horror", "Science Fiction"], ["Ridley Scott"], True, False),
    (238, "The Godfather", 1972, 175, ["Drama", "Crime"], ["Francis Ford Coppola"], True, True),
]


@pytest.fixture
def conn():
    """An in-memory database with the schema applied and sample films loaded."""
    db.close_connection()
    connection = db.get_connection(":memory:")
    connection.executemany(
        """
        INSERT INTO films
            (tmdb_id, title, year, runtime, genres, directors, on_watchlist, watched)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        SAMPLE_FILMS,
    )
    yield connection
    db.close_connection()


@pytest.fixture
def empty_conn():
    """An in-memory database with the schema applied but no rows."""
    db.close_connection()
    connection = db.get_connection(":memory:")
    yield connection
    db.close_connection()


@pytest.fixture
def export(tmp_path):
    """A miniature Letterboxd export on disk. See `export_fixture`."""
    return write_export(tmp_path / "export")
