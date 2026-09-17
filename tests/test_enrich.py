"""TMDB enrichment and the merge into the queryable tables.

A fake TMDB client stands in for the API, so no test needs a token or the
network. The response fixture mirrors a real one closely enough to exercise
the parts that actually bite: crew credited twice, a cast list longer than we
keep, and fields TMDB returns as 0 or "" rather than null.
"""

from __future__ import annotations

import pytest

from projectsummer.core import db, enrich
from projectsummer.core.enrich import (
    MissingTokenError,
    as_film_row,
    crew_named,
    enrich_all,
    leading_cast,
    rebuild_diary,
)
from projectsummer.core.ingest import ingest_export
from projectsummer.core.resolve import Identity, remember

from export_fixture import ALIEN, GODFATHER, SHINING, UNSEEN


def tmdb_response(tmdb_id=27205, title="Inception", release="2010-07-15"):
    """A TMDB payload shaped like the real thing."""
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": release,
        "runtime": 148,
        "overview": "A thief who steals corporate secrets.",
        "tagline": "Your mind is the scene of the crime.",
        "poster_path": "/poster.jpg",
        "budget": 160000000,
        "revenue": 825532764,
        "status": "Released",
        "original_language": "en",
        "vote_average": 8.4,
        "vote_count": 34000,
        "popularity": 91.2,
        "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
        "spoken_languages": [{"english_name": "English", "name": "English"}],
        "production_companies": [{"name": "Legendary Pictures"}],
        "production_countries": [{"name": "United States of America"}],
        "external_ids": {"imdb_id": "tt1375666", "facebook_id": "inception"},
        "keywords": {"keywords": [{"name": "dream"}, {"name": "heist"}]},
        "credits": {
            "cast": [{"name": f"Actor {i}"} for i in range(1, 16)],
            "crew": [
                {"name": "Christopher Nolan", "job": "Director", "department": "Directing"},
                # Credited twice -- must appear once.
                {"name": "Christopher Nolan", "job": "Writer", "department": "Writing"},
                {"name": "Christopher Nolan", "job": "Screenplay", "department": "Writing"},
                {"name": "Hans Zimmer", "job": "Original Music Composer", "department": "Sound"},
                {"name": "Wally Pfister", "job": "Director of Photography", "department": "Camera"},
                {"name": "Lee Smith", "job": "Editor", "department": "Editing"},
                {"name": "Nobody Important", "job": "Best Boy", "department": "Lighting"},
            ],
        },
    }


class FakeClient:
    """Stands in for TMDBClient, recording what was asked for."""

    def __init__(self, responses=None, missing=()):
        self.responses = responses or {}
        self.missing = set(missing)
        self.requested: list[int] = []

    def fetch_movie(self, tmdb_id, max_retries=3):
        self.requested.append(tmdb_id)
        if tmdb_id in self.missing:
            return None
        payload = self.responses.get(tmdb_id, tmdb_response(tmdb_id, f"Film {tmdb_id}"))
        return as_film_row(tmdb_id, payload)


# --------------------------------------------------------------- extraction

def test_multi_value_fields_become_lists_not_pipe_joined_strings():
    row = as_film_row(27205, tmdb_response())
    assert row["genres"] == ["Action", "Science Fiction"]
    assert row["directors"] == ["Christopher Nolan"]
    assert row["keywords"] == ["dream", "heist"]


def test_a_person_credited_twice_appears_once():
    row = as_film_row(27205, tmdb_response())
    assert row["writers"] == ["Christopher Nolan"]


def test_each_crew_role_is_picked_out():
    row = as_film_row(27205, tmdb_response())
    assert row["composers"] == ["Hans Zimmer"]
    assert row["cinematographers"] == ["Wally Pfister"]
    assert row["editors"] == ["Lee Smith"]


def test_uninteresting_crew_are_left_out():
    row = as_film_row(27205, tmdb_response())
    everyone = sum((row[k] or [] for k in ("directors", "writers", "composers",
                                           "cinematographers", "editors")), [])
    assert "Nobody Important" not in everyone


def test_cast_is_capped_at_the_leading_names():
    row = as_film_row(27205, tmdb_response())
    assert len(row["cast_members"]) == enrich.CAST_LIMIT
    assert row["cast_members"][0] == "Actor 1"


def test_imdb_id_comes_from_external_ids():
    assert as_film_row(27205, tmdb_response())["imdb_id"] == "tt1375666"


def test_absent_lists_are_none_rather_than_empty():
    """So `genres IS NULL` means 'unknown' rather than 'has no genres'."""
    row = as_film_row(1, {"title": "Bare", "credits": {}, "genres": []})
    assert row["genres"] is None
    assert row["directors"] is None
    assert row["cast_members"] is None


def test_zero_budget_is_treated_as_unknown():
    """TMDB returns 0 for 'we don't know', which is not the same as free."""
    row = as_film_row(1, {"title": "Bare", "budget": 0, "revenue": 0})
    assert row["budget"] is None
    assert row["revenue"] is None


def test_a_film_with_no_title_still_gets_one():
    """films.title is NOT NULL, so this must never produce a null."""
    assert as_film_row(99, {})["title"] == "TMDB 99"


def test_crew_helpers_tolerate_missing_sections():
    assert crew_named({}, jobs={"Director"}) is None
    assert leading_cast({}) is None


# ------------------------------------------------------------------ merging

@pytest.fixture
def resolved(empty_conn, export):
    """An imported export whose films have been given TMDB ids."""
    ingest_export(export)
    for uri, tmdb_id in [(SHINING, 694), (ALIEN, 348), (GODFATHER, 238), (UNSEEN, 393)]:
        remember(Identity(letterboxd_uri=uri, slug=f"slug-{tmdb_id}", tmdb_id=tmdb_id))
    return empty_conn


def test_enrich_populates_films(resolved):
    report = enrich_all(client=FakeClient())

    assert report.enriched == 4
    assert report.films_total == 4
    assert db.query("SELECT count(*) AS n FROM films WHERE enriched_at IS NOT NULL")[0]["n"] == 4


def test_year_is_derived_from_the_release_date(resolved):
    enrich_all(client=FakeClient())
    assert db.query("SELECT DISTINCT year FROM films")[0]["year"] == 2010


def test_your_own_data_is_overlaid_onto_films(resolved):
    enrich_all(client=FakeClient())
    shining = db.query("SELECT * FROM films WHERE tmdb_id = 694")[0]

    assert shining["watched"] is True
    assert shining["liked"] is True
    assert shining["my_rating"] == 4.5

    solaris = db.query("SELECT * FROM films WHERE tmdb_id = 393")[0]
    assert solaris["on_watchlist"] is True
    assert solaris["watched"] is False


def test_every_viewing_becomes_a_diary_row(resolved):
    """The Shining was watched twice, at different ratings."""
    enrich_all(client=FakeClient())

    entries = db.query(
        "SELECT watched_date, rating, rewatch FROM diary_entries "
        "WHERE tmdb_id = 694 ORDER BY watched_date"
    )
    assert len(entries) == 2
    assert [e["rating"] for e in entries] == [4.5, 5.0]
    assert [e["rewatch"] for e in entries] == [False, True]


def test_watch_stats_now_derive_correctly(resolved):
    enrich_all(client=FakeClient())
    stats = db.query("SELECT * FROM film_watch_stats WHERE tmdb_id = 694")[0]

    assert stats["watch_count"] == 2
    assert stats["rewatch_count"] == 1
    assert str(stats["first_watched_date"]) == "2024-01-10"


def test_diary_matches_films_by_name_and_year(resolved):
    """Diary URIs are per-viewing, so (name, year) is the only link."""
    report = enrich_all(client=FakeClient())
    assert report.diary_entries == 3
    assert report.unmatched == 0


def test_review_text_survives_into_the_diary(resolved):
    enrich_all(client=FakeClient())
    review = db.query("SELECT review FROM diary_entries WHERE review IS NOT NULL")[0]
    assert review["review"] == "Slow, and\nbetter for it."


def test_the_integrity_view_stays_empty(resolved):
    enrich_all(client=FakeClient())
    assert db.query("SELECT * FROM integrity_orphans") == []


def test_a_film_tmdb_does_not_have_is_reported_not_fatal(resolved):
    report = enrich_all(client=FakeClient(missing={348}))

    assert report.not_on_tmdb == 1
    assert report.enriched == 3
    assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == 3


def test_a_second_run_fetches_nothing(resolved):
    enrich_all(client=FakeClient())

    again = FakeClient()
    report = enrich_all(client=again)
    assert report.attempted == 0
    assert again.requested == []


def test_limit_allows_a_trial_run(resolved):
    client = FakeClient()
    report = enrich_all(limit=2, client=client)

    assert len(client.requested) == 2
    assert report.still_pending == 2


def test_rebuilding_the_diary_is_idempotent(resolved):
    enrich_all(client=FakeClient())
    before = db.query("SELECT count(*) AS n FROM diary_entries")[0]["n"]

    rebuild_diary()
    assert db.query("SELECT count(*) AS n FROM diary_entries")[0]["n"] == before


def test_list_columns_are_queryable_without_string_splitting(resolved):
    enrich_all(client=FakeClient())
    rows = db.query("SELECT title FROM films WHERE list_contains(genres, 'Action')")
    assert len(rows) == 4


def test_missing_token_says_where_to_get_one(resolved, monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    with pytest.raises(MissingTokenError, match="themoviedb.org"):
        enrich_all()


# -------------------------------------------------------- the picker works

def test_the_watchlist_picker_finally_has_something_to_pick(resolved):
    """The end of the pipeline: ingest -> resolve -> enrich -> a real answer."""
    from projectsummer.core.plugins.discovery import random_watchlist_pick

    enrich_all(client=FakeClient())
    pick = random_watchlist_pick()

    assert pick.tmdb_id == 393  # the only unwatched watchlist film
    assert isinstance(pick.genres, list)
    assert pick.runtime == 148
