"""Head-to-head tournaments, played by a judge with a fixed preference."""

from __future__ import annotations

import random

import pytest

from projectsummer.core.tournament import Tournament, most_choices, round_name


def play(tournament, prefer=lambda a, b: a < b):
    """Decide every matchup by `prefer`, collecting what was asked."""
    asked = []
    while (matchup := tournament.next()) is not None:
        asked.append(matchup)
        tournament.choose(matchup.left if prefer(matchup.left, matchup.right) else matchup.right)
    return asked


def shuffled(n, seed=0):
    entrants = list(range(1, n + 1))
    random.Random(seed).shuffle(entrants)
    return entrants


# -------------------------------------------------------------------- full

@pytest.mark.parametrize("n", [2, 3, 5, 8, 13, 31, 32])
def test_full_ranks_everything_within_the_worst_case(n):
    for seed in range(5):
        tournament = Tournament(shuffled(n, seed))
        asked = play(tournament)

        assert tournament.standings() == [[k] for k in range(1, n + 1)]
        assert len(asked) <= most_choices(n, "full")
        assert [m.number for m in asked] == list(range(1, len(asked) + 1))


def test_no_size_up_to_the_limit_asks_more_than_the_worst_case():
    for n in range(2, 33):
        for seed in range(20):
            assert len(play(Tournament(shuffled(n, seed)))) <= most_choices(n, "full"), n


def test_worst_cases_match_merge_sort():
    assert [most_choices(n, "full") for n in (1, 2, 16, 32)] == [0, 1, 49, 129]


def test_full_rounds_run_from_the_opening_round_to_the_final():
    names = [m.round_name for m in play(Tournament(shuffled(32)))]
    assert names[0] == "Round of 32"
    assert names[-1] == "Final"
    assert list(dict.fromkeys(names)) == [
        "Round of 32", "Round of 16", "Quarter-finals", "Semi-finals", "Final",
    ]


# ----------------------------------------------------------------- bracket

@pytest.mark.parametrize("n", [2, 3, 5, 8, 20, 32])
def test_a_bracket_finds_the_winner_in_one_choice_per_loser(n):
    tournament = Tournament(shuffled(n), mode="bracket")
    asked = play(tournament)

    places = tournament.standings()
    assert places[0] == [1]
    assert len(asked) == n - 1 == most_choices(n, "bracket")
    assert sorted(sum(places, [])) == list(range(1, n + 1))


def test_a_bracket_groups_everyone_out_in_the_same_round():
    tournament = Tournament(shuffled(8), mode="bracket")
    asked = play(tournament)

    assert [len(place) for place in tournament.standings()] == [1, 1, 2, 4]
    assert list(dict.fromkeys(m.round_name for m in asked)) == ["Quarter-finals", "Semi-finals", "Final"]


def test_a_bracket_gives_byes_so_later_rounds_are_even():
    tournament = Tournament(shuffled(5), mode="bracket")
    asked = play(tournament)

    rounds = [m.round_name for m in asked]
    assert rounds.count("Quarter-finals") == 1  # 5 into 4: one match, three byes
    assert rounds.count("Semi-finals") == 2
    assert [len(place) for place in tournament.standings()] == [1, 1, 2, 1]


# ------------------------------------------------------------ undo and edges

def test_undo_forgets_the_last_choice_only():
    tournament = Tournament(shuffled(8))
    first = tournament.next()
    tournament.choose(first.left)
    second = tournament.next()

    assert tournament.undo() is True
    assert tournament.next() == first
    tournament.choose(first.right)
    assert tournament.choices_made == 1
    # A different choice can lead somewhere different, and play carries on.
    play(tournament)
    assert tournament.finished
    assert second.number == 2


def test_undo_with_nothing_to_undo():
    assert Tournament([1, 2]).undo() is False


def test_undo_reopens_a_finished_tournament():
    tournament = Tournament([1, 2])
    play(tournament)
    assert tournament.finished
    tournament.undo()
    assert not tournament.finished
    with pytest.raises(ValueError, match="not finished"):
        tournament.standings()


def test_remaining_counts_down():
    tournament = Tournament(shuffled(8), mode="bracket")
    assert tournament.most_remaining == 7
    tournament.choose(tournament.next().left)
    assert tournament.most_remaining == 6


@pytest.mark.parametrize("mode", ["full", "bracket"])
@pytest.mark.parametrize("entrants", [[], ["only"]])
def test_nothing_to_decide_is_finished_at_once(mode, entrants):
    tournament = Tournament(entrants, mode=mode)
    assert tournament.finished
    assert tournament.standings() == [[e] for e in entrants]


def test_a_choice_must_be_one_of_the_two():
    tournament = Tournament([1, 2, 3])
    with pytest.raises(ValueError, match="not in the current matchup"):
        tournament.choose(99)


def test_entrants_must_be_distinct():
    with pytest.raises(ValueError, match="only once"):
        Tournament([1, 1, 2])


@pytest.mark.parametrize(
    ("field", "name"),
    [(2, "Final"), (3, "Semi-finals"), (4, "Semi-finals"), (5, "Quarter-finals"), (9, "Round of 16"), (32, "Round of 32")],
)
def test_round_names(field, name):
    assert round_name(field) == name
