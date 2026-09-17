"""TMDB enrichment and the merge into the queryable tables.

A fake TMDB client stands in for the API, so no test needs a token or the
network. The response fixture mirrors a real one closely enough to exercise
the parts that actually bite: crew credited twice, a cast list longer than we
keep, and fields TMDB returns as 0 or "" rather than null.
"""

from __future__ import annotations

import pytest

from projectsummer.core import credits, db
from projectsummer.core.enrich import (
    MissingTokenError,
    as_film_row,
    enrich_all,
    rebuild_diary,
    store_films,
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
        "credits": tmdb_credits(tmdb_id),
    }


def tmdb_credits(tmdb_id=27205):
    """A credits object shaped like TMDB's.

    The same people recur across every film built from it, as real
    collaborators do, but credit ids are unique to the film, as on TMDB.
    """
    def crew(person_id, name, job, department, gender=2):
        return {
            "id": person_id, "credit_id": f"{tmdb_id}-{person_id}-{job}", "name": name,
            "original_name": name, "gender": gender, "known_for_department": department,
            "popularity": 1.0, "profile_path": f"/{person_id}.jpg",
            "job": job, "department": department,
        }

    return {
        "cast": [
            {
                "id": 1000 + i, "credit_id": f"{tmdb_id}-cast-{i}", "name": f"Actor {i}",
                "original_name": f"Actor {i}", "gender": (1, 2, 0)[i % 3],
                "known_for_department": "Acting", "popularity": 2.0, "profile_path": None,
                "character": f"Role {i}", "order": i - 1, "cast_id": i,
            }
            for i in range(1, 16)
        ],
        "crew": [
            crew(525, "Christopher Nolan", "Director", "Directing"),
            # Credited three times -- one person, three credits, named once.
            crew(525, "Christopher Nolan", "Writer", "Writing"),
            crew(525, "Christopher Nolan", "Screenplay", "Writing"),
            crew(601, "Dialogue Writer", "Dialogue", "Writing", gender=0),
            crew(947, "Hans Zimmer", "Original Music Composer", "Sound"),
            crew(948, "A Lyricist", "Lyricist", "Writing", gender=1),
            crew(949, "A Singer", "Playback Singer", "Sound", gender=1),
            crew(3032, "Wally Pfister", "Director of Photography", "Camera"),
            crew(3033, "Lee Smith", "Editor", "Editing"),
            crew(556, "Emma Thomas", "Producer", "Production", gender=1),
            crew(700, "Nobody Important", "Best Boy", "Lighting"),
            crew(701, "An Art Director", "Art Direction", "Art"),
            crew(702, "A Costumer", "Costume Designer", "Costume & Make-Up"),
        ],
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
    assert row["writers"].count("Christopher Nolan") == 1


def test_writers_include_dialogue_and_novel_credits():
    row = as_film_row(27205, tmdb_response())
    assert row["writers"] == ["Christopher Nolan", "Dialogue Writer"]


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
    assert "An Art Director" not in everyone


def test_cast_is_capped_at_the_leading_names():
    row = as_film_row(27205, tmdb_response())
    assert len(row["cast_members"]) == credits.CAST_LIMIT
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


def test_the_row_carries_its_credits_for_storage():
    row = as_film_row(27205, tmdb_response())
    assert isinstance(row["film_credits"], credits.FilmCredits)
    assert row["film_credits"].names("producer") == ["Emma Thomas"]


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


# ------------------------------------------------------- people and credits
#
# Each film's kept credits: 10 cast plus 10 crew credits (Nolan three times,
# then dialogue, composer, lyricist, singer, cinematographer, editor,
# producer). The same 18 people recur in every film, as collaborators do.

CREDITS_PER_FILM = 20
PEOPLE = 18


def test_enrich_stores_each_person_once_and_every_credit(resolved):
    enrich_all(client=FakeClient())

    assert db.query("SELECT count(*) AS n FROM film_credits")[0]["n"] == 4 * CREDITS_PER_FILM
    assert db.query("SELECT count(*) AS n FROM people")[0]["n"] == PEOPLE
    per_film = db.query("SELECT tmdb_id, count(*) AS n FROM film_credits GROUP BY 1")
    assert {row["n"] for row in per_film} == {CREDITS_PER_FILM}
    assert db.query("SELECT * FROM integrity_orphans") == []


def test_credits_are_filed_under_plain_roles(resolved):
    enrich_all(client=FakeClient())
    roles = {
        row["role"]: row["n"]
        for row in db.query(
            "SELECT role, count(*) AS n FROM film_credits WHERE tmdb_id = 694 GROUP BY 1"
        )
    }
    assert roles == {
        "actor": 10, "director": 1, "writer": 3, "composer": 1, "lyricist": 1,
        "playback singer": 1, "cinematographer": 1, "editor": 1, "producer": 1,
    }


def test_a_question_about_people_is_answerable(resolved):
    enrich_all(client=FakeClient())
    women = db.query(
        """
        SELECT DISTINCT p.name
        FROM film_credits c JOIN people p USING (person_id)
        WHERE c.role IN ('producer', 'lyricist', 'playback singer') AND p.gender = 'female'
        ORDER BY 1
        """
    )
    assert [row["name"] for row in women] == ["A Lyricist", "A Singer", "Emma Thomas"]
    unknown = db.query("SELECT gender FROM people WHERE person_id = 601")[0]
    assert unknown["gender"] is None


def test_the_films_name_lists_agree_with_the_credits(resolved):
    enrich_all(client=FakeClient())
    film = db.query("SELECT directors, writers, cast_members FROM films WHERE tmdb_id = 694")[0]
    from_credits = db.query(
        """
        SELECT list(DISTINCT p.name ORDER BY p.name) AS names
        FROM film_credits c JOIN people p USING (person_id)
        WHERE c.tmdb_id = 694 AND c.role = 'writer'
        """
    )[0]["names"]
    assert sorted(film["writers"]) == from_credits
    assert film["directors"] == ["Christopher Nolan"]
    assert len(film["cast_members"]) == 10


def test_every_stored_film_is_recorded_as_having_its_credits(resolved):
    enrich_all(client=FakeClient())
    assert db.query("SELECT count(*) AS n FROM film_credit_fetches")[0]["n"] == 4


def test_a_film_with_no_kept_credits_is_still_recorded(empty_conn):
    store_films([as_film_row(5, {"title": "Silent", "credits": {"cast": [], "crew": []}})])
    assert db.query("SELECT tmdb_id FROM film_credit_fetches") == [{"tmdb_id": 5}]


def test_refetching_a_film_replaces_its_credits(empty_conn):
    store_films([as_film_row(27205, tmdb_response())])

    changed = tmdb_response()
    changed["credits"]["crew"] = [
        c for c in changed["credits"]["crew"] if c["job"] != "Producer"
    ]
    store_films([as_film_row(27205, changed)])

    roles = [row["role"] for row in db.query("SELECT role FROM film_credits")]
    assert "producer" not in roles
    assert len(roles) == CREDITS_PER_FILM - 1


def test_refetching_never_wipes_what_credits_do_not_carry(empty_conn):
    store_films([as_film_row(27205, tmdb_response())])
    empty_conn.execute(
        "UPDATE people SET birthday = DATE '1970-07-30', place_of_birth = 'London', "
        "details_fetched_at = now() WHERE person_id = 525"
    )

    # A later response that no longer knows Nolan's gender.
    forgetful = tmdb_response()
    for member in forgetful["credits"]["crew"]:
        if member["id"] == 525:
            member["gender"] = 0
    store_films([as_film_row(27205, forgetful)])

    nolan = db.query("SELECT * FROM people WHERE person_id = 525")[0]
    assert str(nolan["birthday"]) == "1970-07-30"
    assert nolan["place_of_birth"] == "London"
    assert nolan["details_fetched_at"] is not None
    assert nolan["gender"] == "male"


def test_a_film_and_its_credits_are_stored_together_or_not_at_all(empty_conn, monkeypatch):
    from projectsummer.core import enrich

    def fail(conn, films):
        raise RuntimeError("credits could not be written")

    monkeypatch.setattr(enrich, "store_credits", fail)
    with pytest.raises(RuntimeError):
        store_films([as_film_row(27205, tmdb_response())])

    assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == 0
