"""Analysis plugins: fixed definitions of what a library's figures mean."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from projectsummer import cli
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import EmptyDatabaseError, MissingArgumentError
from projectsummer.core.plugins.analysis import taste, trends

runner = CliRunner()

# tmdb_id, watched_date, rating, rewatch. Sample films: Inception (27205, en,
# 2010), The Shining (694, en, 1980), Alien (348, en, 1979), The Godfather
# (238, en, 1972); The Others (1026) is given Spanish below.
VIEWINGS = [
    (27205, "2023-01-05", 4.5, False),
    (694,   "2023-01-20", 4.0, False),
    (694,   "2023-03-02", None, True),
    (1026,  "2023-03-15", 3.0, False),
    (348,   "2025-07-04", 5.0, False),
    (238,   None,         4.0, False),  # no watched date: not counted
]


@pytest.fixture
def diary(conn):
    conn.execute("UPDATE films SET original_language = 'en', production_countries = ['United States of America']")
    conn.execute("UPDATE films SET original_language = 'es', production_countries = ['Spain', 'United States of America'] WHERE tmdb_id = 1026")
    conn.execute("UPDATE films SET runtime = NULL WHERE tmdb_id = 348")
    conn.executemany(
        "INSERT INTO diary_entries (tmdb_id, watched_date, rating, rewatch) VALUES (?, ?, ?, ?)",
        VIEWINGS,
    )
    return conn


def _period(result, name):
    return next(period for period in result.periods if period.period == name)


# ------------------------------------------------------------------ by year

def test_by_year_counts_each_year_and_keeps_empty_ones(diary):
    result = trends()

    assert [p.period for p in result.periods] == ["2023", "2024", "2025"]
    y2023 = _period(result, "2023")
    assert (y2023.viewings, y2023.films, y2023.first_watches, y2023.rewatches) == (4, 3, 3, 1)
    assert y2023.rated == 3
    assert y2023.average_rating == pytest.approx((4.5 + 4.0 + 3.0) / 3, abs=0.01)
    # Inception 148 + The Shining 144 twice + The Others 101 = 537 minutes.
    assert y2023.hours == 9.0

    empty = _period(result, "2024")
    assert (empty.viewings, empty.average_rating, empty.hours, empty.genres) == (0, None, 0.0, [])


def test_breakdowns_list_the_most_watched_first(diary):
    y2023 = _period(trends(top=2), "2023")

    assert [(s.name, s.viewings) for s in y2023.genres] == [("Horror", 3), ("Thriller", 2)]
    assert [(s.name, s.viewings) for s in y2023.languages] == [("en", 3), ("es", 1)]
    assert [(s.name, s.viewings) for s in y2023.countries] == [("United States of America", 4), ("Spain", 1)]
    assert [(s.name, s.viewings) for s in y2023.decades] == [("1980s", 2), ("2000s", 1)]


def test_a_film_without_a_runtime_adds_no_hours(diary):
    assert _period(trends(), "2025").hours == 0.0


def test_by_year_can_narrow_to_one_year(diary):
    result = trends(year=2025)
    assert [p.period for p in result.periods] == ["2025"]
    assert result.periods[0].viewings == 1


def test_an_empty_diary_has_no_periods(conn):
    assert trends().periods == []


# ----------------------------------------------------------------- by month

def test_by_month_shows_all_twelve_months_of_the_year(diary):
    result = trends(by="month", year=2023)

    assert [p.period for p in result.periods] == [f"2023-{m:02d}" for m in range(1, 13)]
    assert [p.viewings for p in result.periods][:4] == [2, 0, 2, 0]
    assert _period(result, "2023-03").rewatches == 1


def test_by_month_needs_a_year(diary):
    with pytest.raises(MissingArgumentError) as error:
        trends(by="month")
    assert error.value.argument == "year"
    assert "year" in str(error.value)


@pytest.mark.parametrize("top", [0, 21])
def test_top_is_bounded(diary, top):
    with pytest.raises(InvalidArgumentError, match="top"):
        trends(top=top)


# ---------------------------------------------------------------------- CLI

def test_the_cli_asks_for_a_missing_year_at_a_terminal(diary, monkeypatch):
    monkeypatch.setattr(cli, "_can_ask", lambda: True)
    result = runner.invoke(cli.build_app(), ["trends", "--by", "month", "--json"], input="2023\n")

    assert result.exit_code == 0, result.output
    # The test runner echoes the typed answer; the prompt itself is on stderr.
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["year"] == 2023
    assert len(payload["periods"]) == 12


def test_without_a_terminal_the_cli_explains_instead_of_asking(diary, monkeypatch):
    monkeypatch.setattr(cli, "_can_ask", lambda: False)
    result = runner.invoke(cli.build_app(), ["trends", "--by", "month"])

    assert result.exit_code == 1
    assert "give the year" in result.output


def test_the_cli_table_reads_breakdowns_as_names_and_counts(diary):
    result = runner.invoke(cli.build_app(), ["trends", "--year", "2023"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "Horror (3)" in result.output


# ---------------------------------------------------------------------- taste
#
# Ratings are set so the answers are arithmetic rather than a matter of taste:
# the two Kubrick films average 4.5, the two Scott films 2.0.

RATED = {
    # tmdb_id: (my_rating, tmdb_rating out of 10, votes)
    27205: (4.0, 8.4, 30_000),   # Inception, en, 2010s
    694:   (5.0, 8.2, 10_000),   # The Shining, en, 1980s
    11324: (3.0, 8.2, 20_000),   # Shutter Island, en, 2010s
    1026:  (1.0, 7.0, 5_000),    # The Others, es, 2000s
    348:   (2.5, 8.1, 14_000),   # Alien, en, 1970s
    238:   (0.5, 8.7, 50),       # The Godfather, en, 1970s: too few votes
}
CREDITS = [
    # tmdb_id, person_id, name, role
    (694, 1, "Stanley Kubrick", "director"),
    (27205, 2, "Christopher Nolan", "director"),
    (348, 3, "Ridley Scott", "director"),
    (1026, 3, "Ridley Scott", "director"),  # not really, but two films to average
    (11324, 4, "Leonardo DiCaprio", "actor"),
    (27205, 4, "Leonardo DiCaprio", "actor"),
]


@pytest.fixture
def rated(conn):
    conn.execute("UPDATE films SET watched = true, original_language = 'en', production_countries = ['United States of America']")
    conn.execute("UPDATE films SET original_language = 'es', production_countries = ['Spain'] WHERE tmdb_id = 1026")
    conn.executemany(
        "UPDATE films SET my_rating = ?, tmdb_rating = ?, tmdb_vote_count = ? WHERE tmdb_id = ?",
        [(rating, tmdb, votes, tmdb_id) for tmdb_id, (rating, tmdb, votes) in RATED.items()],
    )
    conn.executemany(
        "INSERT INTO people (person_id, name) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [(person_id, name) for _, person_id, name, _ in CREDITS],
    )
    conn.executemany(
        "INSERT INTO film_credits (credit_id, tmdb_id, person_id, role, job, department) VALUES (?, ?, ?, ?, '', '')",
        [(f"c{index}", *credit[:2], credit[3]) for index, credit in enumerate(CREDITS)],
    )
    return conn


def _rows(result, facet, standing=None):
    return [
        (row.name, row.films, row.average_rating)
        for row in result.rows
        if row.facet == facet and (standing is None or row.standing == standing)
    ]


def test_taste_summarises_how_you_rate_against_the_crowd(rated):
    result = taste()

    assert result.rated_films == 6
    assert result.average_rating == pytest.approx(2.67, abs=0.01)
    # TMDB's 0-10 average halved: (8.4 + 8.2 + 8.2 + 7.0 + 8.1 + 8.7) / 2 / 6.
    assert result.tmdb_average == pytest.approx(4.05, abs=0.01)
    assert result.generosity == pytest.approx(-1.38, abs=0.01)


def test_directors_are_grouped_by_person_with_a_minimum(rated):
    result = taste(facet="directors", min_films=2)

    # Kubrick and Nolan have one film each, so only Ridley Scott qualifies.
    assert _rows(result, "directors", "highest") == [("Ridley Scott", 2, 1.75)]
    assert _rows(result, "directors", "lowest") == []


def test_actors_have_a_minimum_of_their_own(rated):
    assert _rows(taste(facet="actors", min_films=1), "actors") == []
    assert _rows(taste(facet="actors", min_films=1, actor_min_films=2), "actors") == [
        ("Leonardo DiCaprio", 2, 3.5)
    ]


def test_highest_and_lowest_never_hold_the_same_row(rated):
    result = taste(facet="languages", top=3, min_films=1)

    assert _rows(result, "languages", "highest") == [("en", 5, 3.0), ("es", 1, 1.0)]
    assert _rows(result, "languages", "lowest") == []


def test_each_facet_is_cut_its_own_way(rated):
    result = taste(top=1, min_films=1, actor_min_films=1)

    assert _rows(result, "decades", "highest") == [("1980s", 1, 5.0)]
    assert _rows(result, "countries", "highest")[0][0] == "United States of America"
    assert _rows(result, "genres", "highest")[0][1] >= 1
    assert {row.facet for row in result.rows} == {
        "directors", "actors", "genres", "decades", "languages", "countries"
    }


def test_one_facet_can_be_asked_for_alone(rated):
    assert {row.facet for row in taste(facet="genres").rows} == {"genres"}


def test_a_row_carries_the_crowd_average_for_the_same_films(rated):
    row = next(row for row in taste(facet="languages", min_films=1).rows if row.name == "es")

    assert (row.films, row.average_rating, row.tmdb_average) == (1, 1.0, 3.5)
    assert row.difference == -2.5


def test_contrarian_films_need_enough_votes_behind_them(rated):
    result = taste(top=2)

    # The Godfather is the biggest disagreement, but only 50 people voted.
    assert [film.title for film in result.liked_less_than_most] == ["The Others", "Alien"]
    assert [film.title for film in result.loved_more_than_most] == ["The Shining", "Inception"]
    assert result.liked_less_than_most[0].difference == -2.5


@pytest.mark.parametrize(
    ("arguments", "message"),
    [({"top": 0}, "top"), ({"top": 21}, "top"), ({"min_films": 0}, "at least 1")],
)
def test_taste_arguments_are_bounded(rated, arguments, message):
    with pytest.raises(InvalidArgumentError, match=message):
        taste(**arguments)


def test_taste_needs_a_library(empty_conn):
    with pytest.raises(EmptyDatabaseError):
        taste()


def test_taste_runs_through_the_cli(rated):
    result = runner.invoke(cli.build_app(), ["taste", "--facet", "genres", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["rated_films"] == 6
    assert payload["rows"][0]["facet"] == "genres"
