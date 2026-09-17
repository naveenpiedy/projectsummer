"""Head-to-head tournaments: rank things by choosing between two at a time.

Two ways to play:

* **full** ranks everything. It is a merge sort played in rounds:
  the first round pairs entrants off, and each later round merges the
  winners' groups, one choice at a time. That asks close to the fewest
  questions any complete ranking can -- 129 at most for 32 entrants.
* **bracket** is a knockout. It needs one choice fewer than there are
  entrants and finds a winner, but everyone knocked out in the same round
  shares a place: a knockout never compares them with each other.

A tournament holds only the entrants and the choices made so far. The next
matchup is found by replaying those choices from the start, so undoing one is
just forgetting it -- no state to unwind. Replaying is a few hundred steps at
most, which is nothing next to a person deciding.

Nothing here touches the database or the terminal; the `rank` plugin does.
"""

from __future__ import annotations

from collections.abc import Generator, Hashable, Sequence
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T", bound=Hashable)

Mode = Literal["full", "bracket"]

#: A game yields (left, right, round name), is sent True if left won, and
#: returns the standings: places, best first, each a group of entrants.
_Game = Generator[tuple[T, T, str], bool, list[list[T]]]


@dataclass(frozen=True)
class Matchup(Generic[T]):
    """Two entrants to choose between."""

    left: T
    right: T
    round_name: str
    """E.g. "Round of 32", "Quarter-finals", "Final"."""
    number: int
    """Which matchup this is, counting from 1."""


class Tournament(Generic[T]):
    """A tournament in progress."""

    def __init__(self, entrants: Sequence[T], mode: Mode = "full"):
        if len(set(entrants)) != len(entrants):
            raise ValueError("Each entrant can appear only once.")
        if mode not in ("full", "bracket"):
            raise ValueError(f"mode must be 'full' or 'bracket', not {mode!r}.")
        self._entrants = list(entrants)
        self._mode: Mode = mode
        self._choices: list[bool] = []
        self._current, self._standings = self._replay()

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def finished(self) -> bool:
        return self._current is None

    @property
    def choices_made(self) -> int:
        return len(self._choices)

    @property
    def most_remaining(self) -> int:
        """The most choices still to make. Exact for a bracket; for a full
        ranking the real number is often lower."""
        return max(0, most_choices(len(self._entrants), self._mode) - len(self._choices))

    def next(self) -> Matchup[T] | None:
        """The matchup waiting to be decided, or None once finished."""
        return self._current

    def choose(self, winner: T) -> None:
        """Record which of the current matchup's two entrants won."""
        if self._current is None:
            raise ValueError("The tournament is finished; there is nothing to choose.")
        if winner not in (self._current.left, self._current.right):
            raise ValueError(f"{winner!r} is not in the current matchup.")
        self._choices.append(winner == self._current.left)
        self._current, self._standings = self._replay()

    def undo(self) -> bool:
        """Forget the last choice. Returns False if there was none to forget."""
        if not self._choices:
            return False
        self._choices.pop()
        self._current, self._standings = self._replay()
        return True

    def standings(self) -> list[list[T]]:
        """Places, best first, once finished. Each place is a group: always
        one entrant in a full ranking, and everyone knocked out in the same
        round of a bracket."""
        if self._standings is None:
            raise ValueError("The tournament is not finished yet.")
        return [list(place) for place in self._standings]

    def _replay(self) -> tuple[Matchup[T] | None, list[list[T]] | None]:
        game = _full(self._entrants) if self._mode == "full" else _bracket(self._entrants)
        try:
            left, right, round_name = next(game)
            for left_won in self._choices:
                left, right, round_name = game.send(left_won)
        except StopIteration as done:
            return None, done.value
        return Matchup(left, right, round_name, len(self._choices) + 1), None


def most_choices(entrants: int, mode: Mode) -> int:
    """The most choices a tournament of this size can ask for."""
    if entrants < 2:
        return 0
    if mode == "bracket":
        return entrants - 1
    # Merge sort's worst case: n * ceil(log2 n) - 2 ** ceil(log2 n) + 1.
    depth = (entrants - 1).bit_length()
    return entrants * depth - (1 << depth) + 1


def round_name(field: int) -> str:
    """Name a round by how many are left in it, rounded up the way a bracket
    would be: 5 to 8 entrants play quarter-finals."""
    size = 1 << max(0, (field - 1).bit_length())
    return {2: "Final", 4: "Semi-finals", 8: "Quarter-finals"}.get(size, f"Round of {size}")


def _full(entrants: list[T]) -> _Game:
    # Halving from the top keeps merge sort's worst case; a bottom-up pass
    # that carries an odd group forward can ask more (9 rather than 8 for 5
    # entrants). The merges are then played lowest first, level by level, so
    # the game still runs in rounds.
    levels: list[list[tuple[int, int, int]]] = []

    def split(start: int, end: int) -> int:
        if end - start < 2:
            return 0
        middle = (start + end) // 2
        height = 1 + max(split(start, middle), split(middle, end))
        while len(levels) < height:
            levels.append([])
        levels[height - 1].append((start, middle, end))
        return height

    split(0, len(entrants))
    order = list(entrants)
    groups = len(entrants)
    for merges in levels:
        name = round_name(groups)
        for start, middle, end in merges:
            left, right = order[start:middle], order[middle:end]
            out: list[T] = []
            while left and right:
                left_won = yield left[0], right[0], name
                out.append((left if left_won else right).pop(0))
            order[start:end] = out + left + right
        groups -= len(merges)
    return [[entrant] for entrant in order]


def _bracket(entrants: list[T]) -> _Game:
    if len(entrants) < 2:
        return [list(entrants)] if entrants else []
    size = 1 << (len(entrants) - 1).bit_length()
    byes = size - len(entrants)
    # The first entrants sit out the opening round, so that every later round
    # is a clean power of two. Entrants arrive shuffled, so that is fair.
    field, playing = entrants[:byes], entrants[byes:]
    knocked_out: list[list[T]] = []
    name = round_name(size)
    while True:
        losers = []
        winners = []
        for left, right in zip(playing[0::2], playing[1::2]):
            left_won = yield left, right, name
            winners.append(left if left_won else right)
            losers.append(right if left_won else left)
        knocked_out.append(losers)
        field = field + winners
        if len(field) == 1:
            return [field, *reversed(knocked_out)]
        playing, field = field, []
        name = round_name(len(playing))
