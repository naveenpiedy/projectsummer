"""Ranking plugin: put films in order by choosing between two at a time.

For fun rather than analysis. It runs as a conversation at the terminal, so
it is not offered over MCP: a choice per chat message would be slow, and the
assistant would add nothing to "Heat or Collateral?". Nothing is stored in
the library; a finished ranking can be written out as a list to import.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Literal

from projectsummer.core import asking, db
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import NoResultError
from projectsummer.core.list_builder import build_from_sql, write_import_files
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result
from projectsummer.core.tournament import Tournament, round_name

#: Most films one ranking may hold. A full ranking of 32 asks at most 129
#: questions, which is about as long as choosing stays fun.
MAX_FILMS = 32


class RankedFilm(Result):
    """A film's place in a ranking."""

    place: int
    """1 for the best. Films knocked out in the same round of a bracket
    share a place."""
    tmdb_id: int
    """TMDB's id for the film."""
    title: str
    """The film's title."""
    year: int | None
    """Release year."""
    your_rating: float | None
    """Your current rating, for comparison."""


class Ranking(Result):
    """How a ranking went."""

    mode: Literal["full", "bracket"]
    """`full` put every film in order; `bracket` was a knockout."""
    films: int
    """Films in the ranking."""
    choices: int
    """Choices made."""
    finished: bool
    """False if it was stopped before the end, in which case nothing is
    ranked or saved."""
    ranking: list[RankedFilm]
    """The films, best first. Empty if unfinished."""
    saved_to: list[str]
    """Files the ranking was written to, if saved."""


@plugin(category="lists", access="write", destructive=True)
def rank(
    from_list: str | None = None,
    sql: str | None = None,
    mode: Literal["full", "bracket"] = "full",
    save: Path | None = None,
    seed: int | None = None,
) -> Ranking:
    """Rank films head to head: pick the better of two until all are in order.

    Choose the films from one of your lists or with a SQL query, up to 32 of
    them. They are drawn in a random order and played in rounds, from the
    opening round to the final. Press 1 or 2 to pick, u to undo the last
    choice, or q to stop.

    Full mode ranks every film, in at most 129 choices for 32 films and
    usually fewer. Bracket mode is a knockout: one choice per film knocked
    out, finding a champion, with everyone out in the same round sharing a
    place.

    Args:
        from_list: The slug of one of your lists, as shown by `lists`.
        sql: A single SELECT returning a tmdb_id column, instead of a list.
        mode: "full" to rank every film, or "bracket" for a knockout.
        save: Also write the finished ranking to this .csv, in order, for
            Letterboxd's list importer.
        seed: Draw the films in the same order every time with this number.

    Returns:
        The ranking, best first.

    Raises:
        InvalidArgumentError: If neither or both of from_list and sql are
            given, or there are fewer than 2 or more than 32 films.
        NoResultError: If the list does not exist or holds no films.
        NoOneToAskError: If not run at a terminal.
    """
    if (from_list is None) == (sql is None):
        raise InvalidArgumentError(
            "Choose the films with either --from-list (a list's slug, as shown "
            "by `summer lists`) or --sql, but not both."
        )
    asking.require_someone()
    films = _films_from_list(from_list) if from_list is not None else _films_from_sql(sql)
    if len(films) < 2:
        raise InvalidArgumentError("A ranking needs at least 2 films from your library.")

    by_id = {film["tmdb_id"]: film for film in films}
    # Sorted first: the query's row order is not fixed, and a seed must draw
    # the same order every time.
    order = sorted(by_id)
    random.Random(seed).shuffle(order)
    tournament: Tournament[int] = Tournament(order, mode)

    if not _play(tournament, by_id):
        return Ranking(
            mode=mode, films=len(order), choices=tournament.choices_made,
            finished=False, ranking=[], saved_to=[],
        )

    ranking = _placed(tournament.standings(), by_id)
    _reveal(tournament, ranking)
    saved = []
    if save is not None:
        saved = [
            str(path)
            for path in write_import_files([by_id[film.tmdb_id] for film in ranking], save)
        ]
    return Ranking(
        mode=mode, films=len(order), choices=tournament.choices_made,
        finished=True, ranking=ranking, saved_to=saved,
    )


_FILM_FIELDS = "f.tmdb_id, f.imdb_id, f.title, f.year, f.directors, f.my_rating"


def _films_from_list(slug: str) -> list[dict[str, Any]]:
    found = db.query("SELECT list_id FROM lists WHERE slug = ?", [slug])
    if not found:
        known = db.query("SELECT slug FROM lists ORDER BY slug LIMIT 10")
        suggestions = ", ".join(row["slug"] for row in known) or "none"
        raise NoResultError(f"No list with slug {slug!r}. Known slugs include: {suggestions}.")
    films = db.query(
        f"""
        SELECT DISTINCT ON (f.tmdb_id) {_FILM_FIELDS}
        FROM list_entries e
        JOIN films f USING (tmdb_id)
        WHERE e.list_id = ?
        """,
        [found[0]["list_id"]],
    )
    if not films:
        raise NoResultError(f"The list {slug!r} holds no films that are in your library yet.")
    _check_size(len(films))
    return films


def _films_from_sql(sql: str) -> list[dict[str, Any]]:
    # Reuses the list builder's checks: one SELECT, with a tmdb_id column.
    ids = [film["tmdb_id"] for film in build_from_sql(sql).films]
    _check_size(len(ids))
    return db.query(
        f"SELECT {_FILM_FIELDS} FROM films f WHERE f.tmdb_id IN (SELECT unnest(?::BIGINT[]))",
        [ids],
    )


def _check_size(films: int) -> None:
    if films > MAX_FILMS:
        raise InvalidArgumentError(
            f"That is {films} films; a ranking holds at most {MAX_FILMS}. "
            f"Narrow it down, for example with LIMIT in the query."
        )


def _play(tournament: Tournament[int], films: dict[int, dict[str, Any]]) -> bool:
    """Ask every matchup in turn. Returns False if the person stopped."""
    shown_round = None
    while (matchup := tournament.next()) is not None:
        if matchup.round_name != shown_round:
            shown_round = matchup.round_name
            asking.tell(shown_round)
        # A bracket's count is exact; a full ranking often finishes early.
        to_go = (
            f"{tournament.most_remaining} to go"
            if tournament.mode == "bracket"
            else f"at most {tournament.most_remaining} to go"
        )
        answer = asking.choose(
            f"Matchup {matchup.number}, {to_go}",
            {
                "1": _label(films[matchup.left]),
                "2": _label(films[matchup.right]),
                "u": "undo the last choice",
                "q": "stop without saving",
            },
        )
        if answer == "q":
            return False
        if answer == "u":
            if not tournament.undo():
                asking.tell("Nothing to undo yet.")
            continue
        winner, loser = (
            (matchup.left, matchup.right) if answer == "1" else (matchup.right, matchup.left)
        )
        tournament.choose(winner)
        if tournament.mode == "bracket":
            _mention_upset(films[winner], films[loser])
    return True


def _mention_upset(winner: dict[str, Any], loser: dict[str, Any]) -> None:
    if (
        winner["my_rating"] is not None
        and loser["my_rating"] is not None
        and winner["my_rating"] < loser["my_rating"]
    ):
        asking.tell(
            f"Upset! {winner['title']} knocks out {loser['title']}, which you "
            f"rated higher."
        )


def _label(film: dict[str, Any]) -> str:
    label = film["title"]
    if film["year"] is not None:
        label += f" ({film['year']})"
    if film["directors"]:
        label += f", {', '.join(film['directors'])}"
    return label


def _placed(standings: list[list[int]], films: dict[int, dict[str, Any]]) -> list[RankedFilm]:
    """Number the places. A shared place is ordered by your rating, then title,
    only so the list reads sensibly; the knockout did not decide it."""
    ranking = []
    for group in standings:
        place = len(ranking) + 1
        group = sorted(
            group,
            key=lambda tmdb_id: (-(films[tmdb_id]["my_rating"] or 0), films[tmdb_id]["title"]),
        )
        for tmdb_id in group:
            film = films[tmdb_id]
            ranking.append(RankedFilm(
                place=place, tmdb_id=tmdb_id, title=film["title"], year=film["year"],
                your_rating=film["my_rating"],
            ))
    return ranking


def _reveal(tournament: Tournament[int], ranking: list[RankedFilm]) -> None:
    """Count down from last place to the winner."""
    asking.tell("The results")
    if tournament.mode == "full":
        for film in reversed(ranking):
            asking.tell(f"{film.place}. {_short(film)}")
        return

    places = sorted({film.place for film in ranking}, reverse=True)
    for depth, place in enumerate(places):
        group = [_short(film) for film in ranking if film.place == place]
        rounds_from_final = len(places) - 1 - depth
        if rounds_from_final == 0:
            asking.tell(f"Champion: {group[0]}")
        elif rounds_from_final == 1:
            asking.tell(f"Runner-up: {group[0]}")
        else:
            out_in = round_name(2 ** rounds_from_final)
            asking.tell(f"Out in the {out_in.lower()}: {'; '.join(group)}")


def _short(film: RankedFilm) -> str:
    return f"{film.title} ({film.year})" if film.year is not None else film.title
