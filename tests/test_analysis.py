"""Analysis plugins: fixed definitions of what a library's figures mean."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from projectsummer import cli
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import MissingArgumentError
from projectsummer.core.plugins.analysis import trends

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
