"""Which credits are kept from a TMDB response, and the role each is given."""

from __future__ import annotations

import pytest

from projectsummer.core import credits
from projectsummer.core.credits import ACTOR, CAST_LIMIT, extract

from test_enrich import tmdb_credits


def test_leading_cast_is_kept_in_billing_order():
    kept = extract(tmdb_credits())
    actors = [c for c in kept.credits if c.role == ACTOR]

    assert len(actors) == CAST_LIMIT
    assert [c.billing_order for c in actors] == list(range(CAST_LIMIT))
    assert actors[0].character == "Role 1"
    assert actors[0].job == "Actor" and actors[0].department == "Acting"


def test_cast_out_of_order_is_sorted_by_billing():
    payload = tmdb_credits()
    payload["cast"] = list(reversed(payload["cast"]))
    names = extract(payload).names(ACTOR)
    assert names[:2] == ["Actor 1", "Actor 2"]


@pytest.mark.parametrize(
    ("job", "role"),
    [
        ("Director", "director"),
        ("Co-Director", "co-director"),
        ("Screenplay", "writer"),
        ("Writer", "writer"),
        ("Story", "writer"),
        ("Novel", "writer"),
        ("Dialogue", "writer"),
        ("Lyricist", "lyricist"),
        ("Director of Photography", "cinematographer"),
        ("Editor", "editor"),
        ("Original Music Composer", "composer"),
        ("Music", "composer"),
        ("Playback Singer", "playback singer"),
        ("Production Design", "production designer"),
        ("Costume Design", "costume designer"),
        ("Producer", "producer"),
    ],
)
def test_each_kept_job_gets_its_role(job, role):
    kept = extract({"crew": [{"id": 1, "credit_id": "c", "name": "Someone", "job": job, "department": "X"}]})
    assert [(c.job, c.role) for c in kept.credits] == [(job, role)]


@pytest.mark.parametrize(
    "job", ["Art Direction", "Costume Designer", "Executive Producer", "Best Boy", "Makeup Artist"]
)
def test_other_jobs_are_dropped(job):
    kept = extract({"crew": [{"id": 1, "credit_id": "c", "name": "Someone", "job": job, "department": "X"}]})
    assert kept.credits == ()
    assert kept.people == ()


def test_one_person_with_several_credits_is_one_person():
    kept = extract(tmdb_credits())
    nolan = [c for c in kept.credits if c.person_id == 525]

    assert sorted(c.job for c in nolan) == ["Director", "Screenplay", "Writer"]
    assert len({c.credit_id for c in nolan}) == 3
    assert [p.person_id for p in kept.people].count(525) == 1
    assert kept.names("writer").count("Christopher Nolan") == 1


@pytest.mark.parametrize(("code", "gender"), [(0, None), (1, "female"), (2, "male"), (3, "non-binary"), (None, None)])
def test_gender_codes_become_words_and_unknown_becomes_null(code, gender):
    kept = extract({"cast": [{"id": 1, "credit_id": "c", "name": "Someone", "gender": code}]})
    assert kept.people[0].gender == gender


def test_crew_have_no_character_or_billing():
    director = next(c for c in extract(tmdb_credits()).credits if c.role == "director")
    assert director.character is None
    assert director.billing_order is None


def test_a_credit_without_ids_still_names_someone_but_is_not_stored():
    kept = extract({"crew": [{"name": "Anonymous", "job": "Director"}]})
    assert kept.names("director") == ["Anonymous"]
    assert kept.storable_credits() == []
    assert kept.people == ()


def test_nothing_to_extract_is_empty_not_an_error():
    for payload in (None, {}, {"cast": [], "crew": []}):
        kept = extract(payload)
        assert kept.credits == () and kept.names("director") is None


def test_roles_lists_every_role_once_actor_first():
    roles = credits.roles()
    assert roles[0] == ACTOR
    assert len(roles) == len(set(roles))
    assert set(roles) == {ACTOR, *credits.JOB_ROLES.values()}


def test_a_cast_member_with_no_billing_order_does_not_break_sorting():
    kept = extract({"cast": [
        {"id": 1, "credit_id": "a", "name": "Second", "order": 1},
        {"id": 2, "credit_id": "b", "name": "Unordered", "order": None},
        {"id": 3, "credit_id": "c", "name": "First", "order": 0},
    ]})
    # No crash; the member without an order keeps its place in the list.
    names = kept.names(ACTOR)
    assert names[0] == "First"
    assert sorted(names) == ["First", "Second", "Unordered"]
