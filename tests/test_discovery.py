"""The random watchlist picker -- the end-to-end proof of the plugin loop."""

from __future__ import annotations

import pytest

from projectsummer.core.errors import EmptyDatabaseError, NoResultError
from projectsummer.core.plugins.discovery import random_watchlist_pick

WATCHLIST_TITLES = {"The Shining", "Shutter Island", "The Others", "Alien"}


def test_picks_something_from_the_watchlist(conn):
    pick = random_watchlist_pick()
    assert pick["title"] in WATCHLIST_TITLES
    assert pick["candidates_considered"] == len(WATCHLIST_TITLES)


def test_never_picks_a_watched_film(conn):
    # The Godfather is on the watchlist but already watched.
    picks = {random_watchlist_pick()["title"] for _ in range(40)}
    assert "The Godfather" not in picks
    assert "Inception" not in picks  # watched, not on the watchlist


def test_genre_filter_is_case_insensitive(conn):
    for spelling in ("Horror", "horror", "HORROR"):
        pick = random_watchlist_pick(genre=spelling)
        assert "Horror" in pick["genres"]
        assert pick["candidates_considered"] == 3


def test_max_runtime_filter(conn):
    pick = random_watchlist_pick(max_runtime=120)
    assert pick["runtime"] <= 120
    assert pick["candidates_considered"] == 2  # The Others (101), Alien (117)


def test_filters_combine(conn):
    pick = random_watchlist_pick(genre="horror", max_runtime=110)
    assert pick["title"] == "The Others"
    assert pick["candidates_considered"] == 1


def test_seed_makes_the_pick_reproducible(conn):
    first = random_watchlist_pick(seed=42)
    assert all(random_watchlist_pick(seed=42)["title"] == first["title"] for _ in range(5))


def test_returns_list_columns_as_lists(conn):
    pick = random_watchlist_pick(genre="horror")
    assert isinstance(pick["genres"], list)
    assert isinstance(pick["directors"], list)


def test_no_match_names_the_offending_filters(conn):
    with pytest.raises(NoResultError, match="genre 'documentary' and runtime <= 90"):
        random_watchlist_pick(genre="documentary", max_runtime=90)


def test_empty_database_is_distinguished_from_no_match(empty_conn):
    with pytest.raises(EmptyDatabaseError, match="Import a Letterboxd export"):
        random_watchlist_pick()


def test_staged_but_unenriched_says_to_enrich_not_to_import(empty_conn):
    """The message must not tell someone to do what they just did."""
    empty_conn.execute(
        "INSERT INTO staging_films (letterboxd_uri, name, year) "
        "VALUES ('https://boxd.it/x', 'Solaris', 1972)"
    )
    with pytest.raises(EmptyDatabaseError, match="not enriched yet"):
        random_watchlist_pick()
