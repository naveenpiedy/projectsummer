"""Ranking films head to head, and the asking it relies on."""

from __future__ import annotations

import csv

import pytest
from typer.testing import CliRunner

from projectsummer import cli
from projectsummer.core import asking
from projectsummer.core.asking import NoOneToAskError
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import NoResultError
from projectsummer.core.plugins.ranking import rank

runner = CliRunner()

# Best first, by the judge's taste. Ratings are set so The Godfather is rated
# highest while the judge likes it least, for upsets.
TASTE = ["Alien", "The Shining", "Inception", "The Others", "Shutter Island", "The Godfather"]
RATINGS = {"Alien": 4.0, "The Shining": 4.5, "Inception": 3.5, "The Others": 3.0,
           "Shutter Island": 3.0, "The Godfather": 5.0}


class Judge:
    """Answers every matchup by a fixed taste, and remembers what it was told."""

    def __init__(self, script=()):
        self.script = list(script)  # answers to give first, e.g. "u" or "q"
        self.told: list[str] = []
        self.asked = 0

    def tell(self, message):
        self.told.append(message)

    def choose(self, question, options):
        self.asked += 1
        if self.script:
            return self.script.pop(0)
        left, right = options["1"], options["2"]
        return "1" if _taste(left) < _taste(right) else "2"


def _taste(label):
    return next(index for index, title in enumerate(TASTE) if label.startswith(title))


@pytest.fixture
def library(conn):
    for title, rating in RATINGS.items():
        conn.execute("UPDATE films SET my_rating = ? WHERE title = ?", [rating, title])
    conn.execute("INSERT INTO lists (list_id, slug, name) VALUES (1, 'faves', 'Faves'), (2, 'empty', 'Empty')")
    conn.execute(
        """
        INSERT INTO list_entries (list_id, entry_position, tmdb_id, name)
        SELECT 1, row_number() OVER (), tmdb_id, title FROM films
        """
    )
    return conn


def _run(judge, **arguments):
    with asking.answering_with(judge):
        return rank(**arguments)


# ------------------------------------------------------------------- asking

def test_asking_with_nobody_there_is_refused():
    with pytest.raises(NoOneToAskError):
        asking.choose("Which?", {"1": "a", "2": "b"})
    asking.tell("nobody hears this")  # and saying something is harmless


def test_an_asker_is_only_installed_for_its_block():
    judge = Judge(script=["1"])
    with asking.answering_with(judge):
        assert asking.choose("Which?", {"1": "a", "2": "b"}) == "1"
    assert not asking.can_ask()


def test_an_answer_that_is_not_an_option_is_a_bug():
    with pytest.raises(ValueError, match="not an option"):
        with asking.answering_with(Judge(script=["x"])):
            asking.choose("Which?", {"1": "a", "2": "b"})


# --------------------------------------------------------------------- full

def test_a_full_ranking_puts_every_film_in_order(library):
    result = _run(Judge(), from_list="faves", seed=1)

    assert result.finished
    assert [film.title for film in result.ranking] == TASTE
    assert [film.place for film in result.ranking] == [1, 2, 3, 4, 5, 6]
    assert result.ranking[0].your_rating == 4.0


def test_the_order_drawn_depends_on_the_seed_only(library):
    first, second = Judge(), Judge()
    _run(first, from_list="faves", seed=7)
    _run(second, from_list="faves", seed=7)
    assert first.told == second.told


def test_rounds_are_announced_and_the_reveal_counts_down(library):
    judge = Judge()
    _run(judge, from_list="faves", seed=1)

    assert judge.told[0] == "Quarter-finals"
    assert "Final" in judge.told
    reveal = judge.told[judge.told.index("The results") + 1:]
    assert reveal[0].startswith("6. The Godfather")
    assert reveal[-1].startswith("1. Alien")


def test_undo_takes_back_a_choice(library):
    # Deliberately wrong first, then undone: the ranking is unaffected.
    plain = Judge()
    _run(plain, from_list="faves", seed=3)
    corrected = Judge(script=["2", "u"])
    result = _run(corrected, from_list="faves", seed=3)

    assert [film.title for film in result.ranking] == TASTE
    assert corrected.asked == plain.asked + 2


def test_undo_before_any_choice_says_so(library):
    judge = Judge(script=["u"])
    _run(judge, from_list="faves", seed=1)
    assert "Nothing to undo yet." in judge.told


def test_stopping_ranks_and_saves_nothing(library, tmp_path):
    target = tmp_path / "ranked.csv"
    result = _run(Judge(script=["1", "q"]), from_list="faves", save=target)

    assert (result.finished, result.choices, result.ranking, result.saved_to) == (False, 1, [], [])
    assert not target.exists()


def test_a_finished_ranking_can_be_saved_in_order(library, tmp_path):
    target = tmp_path / "ranked.csv"
    result = _run(Judge(), from_list="faves", save=target, seed=2)

    assert result.saved_to == [str(target)]
    with target.open(encoding="utf-8") as handle:
        titles = [row["Title"] for row in csv.DictReader(handle, escapechar="\\")]
    assert titles == TASTE


# ------------------------------------------------------------------ bracket

def test_a_bracket_crowns_a_champion_and_groups_the_rest(library):
    judge = Judge()
    result = _run(judge, from_list="faves", mode="bracket", seed=4)

    assert result.ranking[0].title == "Alien"
    assert result.ranking[0].place == 1
    assert result.choices == 5
    assert judge.told[-1] == "Champion: Alien (1979)"
    # Six entrants: a bracket of eight with two byes, so places run 1, 2, 3.
    assert sorted({film.place for film in result.ranking}) == [1, 2, 3, 5]


def test_a_bracket_points_out_an_upset(library):
    judge = Judge()
    _run(judge, sql="SELECT tmdb_id FROM films WHERE title IN ('Alien', 'The Godfather')", mode="bracket")
    assert any(message.startswith("Upset! Alien knocks out The Godfather") for message in judge.told)


# ---------------------------------------------------------- choosing films

def test_films_can_come_from_a_query(library):
    result = _run(Judge(), sql="SELECT tmdb_id FROM films WHERE 'Horror' = ANY(genres)")
    assert [film.title for film in result.ranking] == ["Alien", "The Shining", "The Others"]


@pytest.mark.parametrize("arguments", [{}, {"from_list": "faves", "sql": "SELECT 1 AS tmdb_id"}])
def test_exactly_one_source_of_films(library, arguments):
    with pytest.raises(InvalidArgumentError, match="either"):
        _run(Judge(), **arguments)


def test_more_than_32_films_is_refused(library):
    with pytest.raises(InvalidArgumentError, match="at most 32"):
        _run(Judge(), sql="SELECT range AS tmdb_id FROM range(1, 41)")


def test_one_film_is_not_a_ranking(library):
    with pytest.raises(InvalidArgumentError, match="at least 2"):
        _run(Judge(), sql="SELECT 694 AS tmdb_id")


def test_an_unknown_list_suggests_real_ones(library):
    with pytest.raises(NoResultError, match="faves"):
        _run(Judge(), from_list="favs")


def test_an_empty_list_is_explained(library):
    with pytest.raises(NoResultError, match="no films"):
        _run(Judge(), from_list="empty")


def test_nobody_to_answer_is_refused_before_anything_happens(library):
    with pytest.raises(NoOneToAskError):
        rank(from_list="faves")


# ---------------------------------------------------------------------- CLI

def test_the_cli_plays_with_keypresses(library, monkeypatch):
    monkeypatch.setattr(cli, "_can_ask", lambda: True)
    # Keys the judge would not press are ignored until a valid one arrives.
    result = runner.invoke(
        cli.build_app(),
        ["rank", "--sql", "SELECT tmdb_id FROM films WHERE title IN ('Alien', 'Inception')", "--mode", "bracket"],
        input="x1",
    )

    assert result.exit_code == 0, result.output
    assert "Final" in result.output
    assert "Champion:" in result.output


def test_the_cli_stops_when_input_runs_out(library, monkeypatch):
    monkeypatch.setattr(cli, "_can_ask", lambda: True)
    result = runner.invoke(cli.build_app(), ["rank", "--from-list", "faves"], input="")
    assert result.exit_code != 0


def test_the_cli_refuses_without_a_terminal(library, monkeypatch):
    monkeypatch.setattr(cli, "_can_ask", lambda: False)
    result = runner.invoke(cli.build_app(), ["rank", "--from-list", "faves"])
    assert result.exit_code == 1
    assert "terminal" in result.output
