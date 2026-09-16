"""Explore and list plugins.

The UI plugin is not started here -- doing so would install a DuckDB
extension over the network and bind a port. What is tested is everything
around it: that it is declared as a serving command, and that the CLI knows
to keep the process alive for one.
"""

from __future__ import annotations

import pytest

from projectsummer.core import db, registry
from projectsummer.core.errors import NoResultError
from projectsummer.core.ingest import ingest_export
from projectsummer.core.plugins.explore import overview
from projectsummer.core.plugins.lists import set_list_ranked, show_lists


# ---------------------------------------------------------------- overview

def test_overview_works_on_an_empty_database(empty_conn):
    """It has to be usable precisely when nothing else is."""
    rows = overview()
    assert {"what": "pipeline state", "count": "empty"} in rows


def test_overview_distinguishes_imported_from_enriched(empty_conn, export):
    ingest_export(export)
    summary = {row["what"]: row["count"] for row in overview()}

    assert summary["films (imported)"] == 4
    assert summary["films (enriched)"] == 0
    assert summary["pipeline state"] == "staged"


def test_overview_reports_the_watch_date_span(empty_conn, export):
    ingest_export(export)
    summary = {row["what"]: row["count"] for row in overview()}

    assert summary["first logged watch"] == "2024-01-10"
    assert summary["most recent watch"] == "2024-03-12"


def test_overview_counts_user_state(empty_conn, export):
    ingest_export(export)
    summary = {row["what"]: row["count"] for row in overview()}

    assert summary["  of which watched"] == 3
    assert summary["  of which watchlist"] == 1
    assert summary["  of which liked"] == 1


# ------------------------------------------------------------------- lists

def test_lists_reports_sizes(empty_conn, export):
    ingest_export(export)
    rows = show_lists()

    assert len(rows) == 1
    assert rows[0]["slug"] == "favourites"
    assert rows[0]["films"] == 2


def test_lists_on_an_empty_database_explains_itself(empty_conn):
    with pytest.raises(NoResultError, match="Import a Letterboxd export"):
        show_lists()


# ------------------------------------------------------------------ ranked
#
# Letterboxd's export gives every list a Position column and no flag saying
# whether the ordering is deliberate -- all list files are structurally
# identical -- so `ranked` cannot be inferred on import. It is set by hand.

def test_lists_are_not_ranked_until_told(empty_conn, export):
    ingest_export(export)
    assert show_lists()[0]["ranked"] is False


def test_marking_a_list_ranked_sticks(empty_conn, export):
    ingest_export(export)

    result = set_list_ranked("favourites")
    assert result["ranked"] is True
    assert show_lists()[0]["ranked"] is True


def test_a_list_can_be_unmarked(empty_conn, export):
    ingest_export(export)
    set_list_ranked("favourites")
    assert set_list_ranked("favourites", ranked=False)["ranked"] is False


def test_ranked_survives_a_reingest(empty_conn, export):
    """Lists are upserted on their slug, so a re-import must not reset this."""
    ingest_export(export)
    set_list_ranked("favourites")

    ingest_export(export)
    assert show_lists()[0]["ranked"] is True


def test_positions_are_kept_whether_or_not_a_list_is_ranked(empty_conn, export):
    """So marking a list ranked later costs nothing."""
    ingest_export(export)
    positions = db.query(
        "SELECT entry_position, name FROM list_entries ORDER BY entry_position"
    )
    assert [row["entry_position"] for row in positions] == [1, 2]


def test_an_unknown_slug_suggests_the_real_ones(empty_conn, export):
    ingest_export(export)
    with pytest.raises(NoResultError, match="favourites"):
        set_list_ranked("favorites")  # the other spelling


# ---------------------------------------------------------------- serving

def test_ui_is_declared_as_a_serving_command():
    """The CLI must know to stay alive, or the server dies on return."""
    registry.discover()
    assert registry.get("ui").serves is True


def test_ordinary_plugins_do_not_serve():
    registry.discover()
    assert registry.get("overview").serves is False
    assert registry.get("random_watchlist_pick").serves is False
