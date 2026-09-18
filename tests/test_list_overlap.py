"""Comparing a list with another list, or with what you have watched."""

from __future__ import annotations

import pytest

from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import NoResultError
from projectsummer.core.plugins.lists import list_overlap

# Sample films: Inception 27205, The Shining 694, Shutter Island 11324,
# The Others 1026, Alien 348, The Godfather 238.
FAVOURITES = [27205, 694, 11324]
HORROR = [694, 1026, 348]


@pytest.fixture
def lists(conn):
    conn.execute("UPDATE films SET watched = false, on_watchlist = false")
    conn.execute("UPDATE films SET watched = true, my_rating = 4.0 WHERE tmdb_id IN (27205, 694)")
    conn.execute("UPDATE films SET on_watchlist = true WHERE tmdb_id IN (348, 1026)")
    conn.execute(
        "INSERT INTO lists (list_id, slug, name) VALUES (1, 'faves', 'Favourites'), (2, 'horror', 'Horror'), (3, 'empty', 'Empty')"
    )
    for list_id, films in ((1, FAVOURITES), (2, HORROR)):
        conn.executemany(
            "INSERT INTO list_entries (list_id, entry_position, tmdb_id, name) VALUES (?, ?, ?, 'x')",
            [(list_id, position, tmdb_id) for position, tmdb_id in enumerate(films, 1)],
        )
    return conn


def test_two_lists_are_compared_both_ways(lists):
    result = list_overlap("faves", "horror")

    assert (result.first.name, result.first.films, result.first.only_here) == ("Favourites", 3, 2)
    assert (result.second.name, result.second.films, result.second.only_here) == ("Horror", 3, 2)
    assert result.shared == 1
    assert result.percent_of_first == result.percent_of_second == pytest.approx(33.3)
    assert [film.title for film in result.shared_films] == ["The Shining"]
    assert {film.title for film in result.only_in_first} == {"Inception", "Shutter Island"}


def test_a_list_is_compared_with_what_you_have_watched_by_default(lists):
    result = list_overlap("faves")

    assert result.second.name == "films you have watched"
    assert result.second.slug is None
    assert result.shared == 2
    assert result.percent_of_first == pytest.approx(66.7)
    assert [film.title for film in result.only_in_first] == ["Shutter Island"]


def test_a_list_can_be_compared_with_your_watchlist(lists):
    result = list_overlap("horror", "watchlist")

    assert result.shared == 2
    assert result.first.only_here == 1  # The Shining, watched rather than waiting


def test_films_are_shown_highest_rated_first(lists):
    result = list_overlap("faves", "horror", top=3)
    ratings = [film.your_rating for film in result.only_in_first]
    assert ratings == [4.0, None]


def test_a_sample_is_capped_at_top(lists):
    result = list_overlap("faves", "watchlist", top=1)
    assert len(result.only_in_first) == 1
    assert result.first.only_here == 3


def test_an_unresolved_list_entry_is_not_compared(lists):
    lists.execute(
        "INSERT INTO list_entries (list_id, entry_position, tmdb_id, name) VALUES (1, 9, NULL, 'Unresolved')"
    )
    assert list_overlap("faves", "horror").first.films == 3


def test_an_unknown_list_says_what_else_it_could_be(lists):
    with pytest.raises(NoResultError, match="watched"):
        list_overlap("favs")


def test_a_list_with_nothing_in_the_library_is_explained(lists):
    with pytest.raises(NoResultError, match="no films"):
        list_overlap("empty")


def test_an_empty_set_is_explained(conn):
    conn.execute("UPDATE films SET liked = false")
    with pytest.raises(NoResultError, match="liked"):
        list_overlap("liked", "watched")


def test_comparing_a_side_with_itself_is_refused(lists):
    with pytest.raises(InvalidArgumentError, match="different"):
        list_overlap("faves", "faves")


@pytest.mark.parametrize("top", [0, 51])
def test_top_is_bounded(lists, top):
    with pytest.raises(InvalidArgumentError, match="top"):
        list_overlap("faves", top=top)
