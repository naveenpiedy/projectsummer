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
    as_person_details,
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


def person_response(person_id):
    """A TMDB person record shaped like the real thing."""
    return {
        "id": person_id,
        "name": f"Person {person_id}",
        "birthday": "1970-07-30",
        "deathday": None,
        "place_of_birth": "Chennai, Tamil Nadu, India",
        "also_known_as": [f"Alias {person_id}", ""],
        "imdb_id": f"nm{person_id}",
        # Credits leave the dialogue writer's gender unset; their own record
        # has it. Everyone else's record leaves it to what credits said.
        "gender": 1 if person_id == 601 else 0,
        "known_for_department": "Writing",
        "biography": "Not kept.",
    }


class FakeClient:
    """Stands in for TMDBClient, recording what was asked for."""

    def __init__(self, responses=None, missing=(), missing_people=(), fail_person_after=None):
        self.responses = responses or {}
        self.missing = set(missing)
        self.missing_people = set(missing_people)
        self.fail_person_after = fail_person_after
        self.requested: list[int] = []
        self.people_requested: list[int] = []

    def fetch_person(self, person_id, max_retries=3):
        if self.fail_person_after is not None and len(self.people_requested) >= self.fail_person_after:
            raise KeyboardInterrupt  # someone pressing Ctrl+C mid-run
        self.people_requested.append(person_id)
        if person_id in self.missing_people:
            return None
        return as_person_details(person_id, person_response(person_id))

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


def test_a_gender_credits_do_not_record_is_stored_as_unknown(empty_conn):
    store_films([as_film_row(27205, tmdb_response())])
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


# ------------------------------------------------------------ person details

def test_enrich_fetches_each_credited_person_once(resolved):
    client = FakeClient()
    report = enrich_all(client=client)

    assert sorted(client.people_requested) == sorted(set(client.people_requested))
    assert len(client.people_requested) == PEOPLE
    assert report.people_fetched == PEOPLE
    assert report.people_pending == 0
    assert db.query("SELECT count(*) AS n FROM people WHERE details_fetched_at IS NULL")[0]["n"] == 0


def test_details_are_stored_with_their_types(resolved):
    enrich_all(client=FakeClient())
    nolan = db.query("SELECT * FROM people WHERE person_id = 525")[0]

    assert str(nolan["birthday"]) == "1970-07-30"
    assert nolan["deathday"] is None
    assert nolan["place_of_birth"] == "Chennai, Tamil Nadu, India"
    assert nolan["also_known_as"] == ["Alias 525"]  # the empty alias is dropped
    assert nolan["imdb_id"] == "nm525"
    assert db.query("SELECT count(*) AS n FROM people WHERE person_id IN "
                    "(SELECT person_id FROM people WHERE contains(place_of_birth, 'Chennai'))")[0]["n"] == PEOPLE


def test_a_persons_own_record_fills_a_gender_their_credits_lacked(resolved):
    enrich_all(client=FakeClient())
    assert db.query("SELECT gender FROM people WHERE person_id = 601")[0]["gender"] == "female"


def test_a_second_run_fetches_no_one_again(resolved):
    enrich_all(client=FakeClient())
    again = FakeClient()
    report = enrich_all(client=again)

    assert again.people_requested == []
    assert report.people_fetched == 0


def test_a_person_tmdb_no_longer_has_is_not_asked_for_forever(resolved):
    report = enrich_all(client=FakeClient(missing_people={556}))
    assert report.people_not_on_tmdb == 1

    emma = db.query("SELECT * FROM people WHERE person_id = 556")[0]
    assert emma["details_fetched_at"] is not None
    assert emma["birthday"] is None
    assert emma["gender"] == "female"  # what credits said is kept

    again = FakeClient()
    enrich_all(client=again)
    assert 556 not in again.people_requested


def test_an_interrupted_run_keeps_what_it_fetched(resolved, monkeypatch):
    from projectsummer.core import enrich

    # One worker, so exactly which batches were saved is predictable. The
    # concurrent version of this is tested below, by what must hold anyway.
    monkeypatch.setattr(enrich, "WORKERS", 1)
    monkeypatch.setattr(enrich, "PEOPLE_BATCH", 5)
    with pytest.raises(KeyboardInterrupt):
        enrich_all(client=FakeClient(fail_person_after=12))

    fetched = db.query("SELECT count(*) AS n FROM people WHERE details_fetched_at IS NOT NULL")[0]["n"]
    assert fetched == 10  # two full batches of five; the partial third is lost

    resumed = FakeClient()
    report = enrich_all(client=resumed)
    assert len(resumed.people_requested) == PEOPLE - 10
    assert report.people_pending == 0


def test_interrupting_the_people_phase_leaves_the_rest_complete(resolved):
    with pytest.raises(KeyboardInterrupt):
        enrich_all(client=FakeClient(fail_person_after=0))

    assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == 4
    assert db.query("SELECT count(*) AS n FROM diary_entries")[0]["n"] == 3
    assert db.query("SELECT watched FROM films WHERE tmdb_id = 694")[0]["watched"] is True


def test_limit_caps_people_too(resolved):
    client = FakeClient()
    report = enrich_all(limit=2, client=client)

    assert len(client.people_requested) == 2
    assert report.people_pending == PEOPLE - 2


# ------------------------------------------------------------ the TMDB client
#
# The client's retry rules decide how a long run behaves when TMDB is busy or
# the network blips, so they are tested against a scripted session rather
# than left to be discovered twenty minutes into a real run.

class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        import requests
        raise requests.HTTPError(f"{self.status_code}")


class _Session:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.headers = {}
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch):
    from projectsummer.core import enrich

    recorded = []
    monkeypatch.setattr(enrich.time, "sleep", recorded.append)
    return recorded


def _client(*outcomes):
    from projectsummer.core.enrich import TMDBClient

    return TMDBClient("token", session=_Session(*outcomes))


def test_fetch_person_reads_a_person_record(sleeps):
    client = _client(_Response(200, person_response(525)))
    details = client.fetch_person(525)

    assert details["imdb_id"] == "nm525"
    assert client.session.urls == ["https://api.themoviedb.org/3/person/525"]


def test_a_missing_person_is_none_without_retrying(sleeps):
    client = _client(_Response(404))
    assert client.fetch_person(1) is None
    assert len(client.session.urls) == 1


def test_being_rate_limited_waits_as_told_then_retries(sleeps):
    client = _client(_Response(429, headers={"Retry-After": "3"}), _Response(200, person_response(7)))
    assert client.fetch_person(7)["person_id"] == 7
    # Measured against a clock, so a hair under the three seconds asked for.
    assert any(2.9 < slept <= 3 for slept in sleeps)


def test_a_network_blip_is_retried(sleeps):
    import requests

    client = _client(requests.ConnectionError("blip"), _Response(200, person_response(7)))
    assert client.fetch_person(7)["person_id"] == 7


def test_a_persistent_server_error_is_raised_not_swallowed(sleeps):
    import requests

    client = _client(_Response(500), _Response(500), _Response(500))
    with pytest.raises(requests.HTTPError):
        client.fetch_person(7)


def test_films_go_through_the_same_client_rules(sleeps):
    client = _client(_Response(429, headers={"Retry-After": "1"}), _Response(200, tmdb_response(27205)))
    row = client.fetch_movie(27205)
    assert row["directors"] == ["Christopher Nolan"]
    assert "append_to_response" not in client.session.urls[0]  # sent as params


# ---------------------------------------------------------------- catching up
#
# A library enriched before credits were kept has films and no people. The
# next enrich must notice, fetch each film again for its credits, and then
# fetch the people on them -- with no flag for the user to know about.

def _forget_credits():
    conn = db.get_connection()
    for table in ("film_credits", "film_credit_fetches", "people"):
        conn.execute(f"DELETE FROM {table}")


def test_films_stored_before_credits_existed_are_fetched_again(resolved):
    enrich_all(client=FakeClient())
    _forget_credits()

    client = FakeClient()
    report = enrich_all(client=client)

    assert sorted(client.requested) == [238, 348, 393, 694]
    assert report.attempted == 4
    assert db.query("SELECT count(*) AS n FROM film_credits")[0]["n"] == 4 * CREDITS_PER_FILM
    assert report.people_fetched == PEOPLE
    # User state survives being fetched again.
    assert db.query("SELECT watched FROM films WHERE tmdb_id = 694")[0]["watched"] is True

    settled = FakeClient()
    enrich_all(client=settled)
    assert settled.requested == [] and settled.people_requested == []


def test_a_film_that_only_came_from_the_feed_catches_up_too(resolved):
    enrich_all(client=FakeClient())
    # Stored by an older sync: in films, never resolved, no credits recorded.
    store_films([as_film_row(5555, tmdb_response(5555, "From the feed"))])
    db.get_connection().execute("DELETE FROM film_credit_fetches WHERE tmdb_id = 5555")

    client = FakeClient()
    enrich_all(client=client)
    assert client.requested == [5555]


def test_a_stored_film_tmdb_no_longer_has_is_not_asked_for_forever(resolved):
    enrich_all(client=FakeClient())
    db.get_connection().execute("DELETE FROM film_credit_fetches WHERE tmdb_id = 348")

    report = enrich_all(client=FakeClient(missing={348}))
    assert report.not_on_tmdb == 1
    # The film itself stays in the library.
    assert db.query("SELECT count(*) AS n FROM films WHERE tmdb_id = 348")[0]["n"] == 1

    again = FakeClient()
    enrich_all(client=again)
    assert 348 not in again.requested


def test_a_film_tmdb_never_had_is_not_asked_for_forever(resolved):
    """It is never stored in `films`, so only films_not_on_tmdb remembers it."""
    report = enrich_all(client=FakeClient(missing={348}))
    assert report.not_on_tmdb == 1
    assert report.still_pending == 0

    again = FakeClient(missing={348})
    second = enrich_all(client=again)
    assert 348 not in again.requested
    assert second.attempted == 0


def test_a_film_tmdb_never_had_can_be_asked_for_again(resolved):
    enrich_all(client=FakeClient(missing={348}))

    client = FakeClient()
    report = enrich_all(client=client, retry_missing=True)

    assert 348 in client.requested
    assert report.enriched == 1
    assert db.query("SELECT count(*) AS n FROM films_not_on_tmdb")[0]["n"] == 0


def test_list_entries_are_given_their_films(resolved):
    """A Letterboxd export identifies list films by link alone, so without
    this a list cannot be compared with anything in the library."""
    before = db.query("SELECT count(tmdb_id) AS n FROM list_entries")[0]["n"]
    enrich_all(client=FakeClient())

    entries = db.query(
        "SELECT e.name, e.tmdb_id FROM list_entries e ORDER BY e.entry_position"
    )
    assert before == 0
    assert [(row["name"], row["tmdb_id"]) for row in entries] == [
        ("The Shining", 694), ("Alien", 348)
    ]


def test_a_list_film_that_was_never_resolved_keeps_no_id(resolved):
    db.get_connection().execute(
        """
        INSERT INTO list_entries (list_id, entry_position, name, letterboxd_uri)
        SELECT list_id, 99, 'Unresolved', 'https://boxd.it/film99' FROM lists LIMIT 1
        """
    )
    enrich_all(client=FakeClient())

    assert db.query(
        "SELECT tmdb_id FROM list_entries WHERE name = 'Unresolved'"
    )[0]["tmdb_id"] is None


def test_overview_shows_what_is_still_waiting(resolved):
    from projectsummer.core.plugins.explore import overview

    enrich_all(client=FakeClient())
    done = overview()
    assert (done.people, done.films_without_credits, done.people_without_details) == (PEOPLE, 0, 0)

    _forget_credits()
    assert overview().films_without_credits == 4

    with pytest.raises(KeyboardInterrupt):
        enrich_all(client=FakeClient(fail_person_after=0))
    waiting = overview()
    assert waiting.films_without_credits == 0
    assert waiting.people_without_details == PEOPLE


def test_being_rate_limited_throughout_raises_rather_than_calling_it_gone(sleeps):
    """None means "TMDB no longer has this", which is recorded for good. A
    busy TMDB must stop the run instead, so the next run can carry on."""
    from projectsummer.core.enrich import RateLimitedError

    client = _client(*[_Response(429, headers={"Retry-After": "1"})] * 3)
    with pytest.raises(RateLimitedError, match="everything fetched so far is kept"):
        client.fetch_person(7)


def test_one_worker_being_told_to_slow_down_holds_them_all(sleeps):
    import threading

    client = _client(_Response(200, person_response(7)))
    client.pause(5)

    worker = threading.Thread(target=client.fetch_person, args=(7,))
    worker.start()
    worker.join()
    assert any(4.9 < slept <= 5 for slept in sleeps)


def test_each_worker_thread_gets_its_own_http_session():
    import threading

    from projectsummer.core.enrich import TMDBClient

    client = TMDBClient("token")
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(client.session)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(session) for session in seen}) == 3
    assert all(session.headers["Authorization"] == "Bearer token" for session in seen)


# ------------------------------------------------------ several at a time

class _SlowClient(FakeClient):
    """Takes a moment per request and records how many ran at once."""

    def __init__(self, **options):
        super().__init__(**options)
        import threading

        self._lock = threading.Lock()
        self.in_flight = 0
        self.most_at_once = 0

    def _enter(self):
        import time

        with self._lock:
            self.in_flight += 1
            self.most_at_once = max(self.most_at_once, self.in_flight)
        time.sleep(0.02)
        with self._lock:
            self.in_flight -= 1

    def fetch_movie(self, tmdb_id, max_retries=3):
        self._enter()
        return super().fetch_movie(tmdb_id, max_retries)

    def fetch_person(self, person_id, max_retries=3):
        self._enter()
        return super().fetch_person(person_id, max_retries)


def test_requests_run_several_at_once_but_no_more_than_the_limit(resolved, monkeypatch):
    from projectsummer.core import enrich

    monkeypatch.setattr(enrich, "WORKERS", 4)
    client = _SlowClient()
    report = enrich_all(client=client)

    assert 1 < client.most_at_once <= 4
    # Nothing fetched twice, nothing missed, everything stored.
    assert sorted(client.requested) == [238, 348, 393, 694]
    assert sorted(client.people_requested) == sorted(set(client.people_requested))
    assert report.people_fetched == PEOPLE and report.people_pending == 0
    assert db.query("SELECT count(*) AS n FROM film_credits")[0]["n"] == 4 * CREDITS_PER_FILM


def test_an_interruption_mid_flight_still_resumes_exactly(resolved, monkeypatch):
    """With workers running, which batches were saved depends on timing. What
    must hold regardless: saved people are complete, and the next run fetches
    exactly the rest."""
    from projectsummer.core import enrich

    monkeypatch.setattr(enrich, "WORKERS", 4)
    monkeypatch.setattr(enrich, "PEOPLE_BATCH", 5)
    with pytest.raises(KeyboardInterrupt):
        enrich_all(client=_SlowClient(fail_person_after=9))

    saved = db.query("SELECT count(*) AS n FROM people WHERE details_fetched_at IS NOT NULL")[0]["n"]
    assert saved % 5 == 0 and saved < PEOPLE
    assert db.query(
        "SELECT count(*) AS n FROM people WHERE details_fetched_at IS NOT NULL AND imdb_id IS NULL"
    )[0]["n"] == 0

    resumed = FakeClient()
    enrich_all(client=resumed)
    assert len(resumed.people_requested) == PEOPLE - saved


def test_a_failing_request_cancels_the_ones_not_yet_started(resolved, monkeypatch):
    from projectsummer.core import enrich

    monkeypatch.setattr(enrich, "WORKERS", 2)
    client = _SlowClient(fail_person_after=0)
    with pytest.raises(KeyboardInterrupt):
        enrich_all(client=client)
    # 18 people were queued; the rest were cancelled rather than sent.
    assert len(client.people_requested) < PEOPLE

