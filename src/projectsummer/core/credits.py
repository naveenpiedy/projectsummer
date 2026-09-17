"""Who made a film: which credits are kept, and the role each is filed under.

TMDB lists everyone -- a big film credits hundreds of crew -- under its own
job names, several of which mean the same thing to a reader ("Screenplay",
"Writer", "Story"). This module decides, in one place, which of those are
worth keeping and what plain role each one is. Both places that describe a
film's people are built from it:

* the name lists on `films` (`directors`, `writers`, ...), the quick path for
  simple filters, and
* the `people` and `film_credits` tables, where a person has an identity and
  attributes of their own.

Because both come from the same extraction of the same response, they cannot
disagree about who wrote a film.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Leading cast kept per film, by billing order. TMDB's gender data stays
#: near-complete through the top ten and thins out after it.
CAST_LIMIT = 10

#: The role given to every kept cast member.
ACTOR = "actor"

#: Crew jobs kept, and the role each is filed under. Anything not listed is
#: dropped -- including "Art Direction" and "Costume Designer", which TMDB
#: sometimes uses where a film has no "Production Design" or "Costume
#: Design" credit.
JOB_ROLES: dict[str, str] = {
    "Director": "director",
    "Co-Director": "co-director",
    "Screenplay": "writer",
    "Writer": "writer",
    "Story": "writer",
    "Novel": "writer",
    "Dialogue": "writer",
    "Lyricist": "lyricist",
    "Director of Photography": "cinematographer",
    "Editor": "editor",
    "Original Music Composer": "composer",
    "Music": "composer",
    "Playback Singer": "playback singer",
    "Production Design": "production designer",
    "Costume Design": "costume designer",
    "Producer": "producer",
}

#: TMDB's gender codes. 0 means "not set", which is stored as unknown.
GENDERS: dict[int, str] = {1: "female", 2: "male", 3: "non-binary"}


@dataclass(frozen=True, slots=True)
class Person:
    """A person as a film's credits describe them.

    Birthday, birthplace and the rest need a separate call per person, so
    they are not here.
    """

    person_id: int
    name: str
    original_name: str | None
    gender: str | None
    known_for_department: str | None
    popularity: float | None
    profile_path: str | None


@dataclass(frozen=True, slots=True)
class Credit:
    """One person's part in one film."""

    credit_id: str | None
    person_id: int | None
    name: str
    role: str
    job: str
    department: str
    character: str | None
    billing_order: int | None

    @property
    def storable(self) -> bool:
        """Whether it carries the ids the credit and people tables are keyed by."""
        return self.credit_id is not None and self.person_id is not None


@dataclass(frozen=True, slots=True)
class FilmCredits:
    """The kept credits of one film, and the people they name."""

    credits: tuple[Credit, ...]
    people: tuple[Person, ...]

    def names(self, role: str) -> list[str] | None:
        """Names holding `role`, in credit order, each once.

        A person credited twice for one role (Writer and Screenplay) appears
        once. None when nobody holds it, so `IS NULL` reads as "unknown".
        """
        seen: dict[str, None] = {}
        for credit in self.credits:
            if credit.role == role:
                seen.setdefault(credit.name, None)
        return list(seen) or None

    def storable_credits(self) -> list[Credit]:
        return [credit for credit in self.credits if credit.storable]


def extract(credits: dict[str, Any] | None) -> FilmCredits:
    """Pick the kept credits out of a TMDB `credits` object."""
    credits = credits or {}
    kept: list[Credit] = []
    people: dict[int, Person] = {}

    cast = [member for member in credits.get("cast") or [] if member.get("name")]
    # TMDB returns cast in billing order already; sort anyway, keeping the
    # given order for entries without one.
    def billing(pair: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, member = pair
        order = member.get("order")
        return (order if isinstance(order, int) else index, index)

    cast = sorted(enumerate(cast), key=billing)
    for _, member in cast[:CAST_LIMIT]:
        kept.append(_credit(member, ACTOR, "Actor", "Acting"))
        _remember(people, member)

    for member in credits.get("crew") or []:
        role = JOB_ROLES.get(member.get("job") or "")
        if role is None or not member.get("name"):
            continue
        kept.append(_credit(member, role, member["job"], member.get("department") or ""))
        _remember(people, member)

    return FilmCredits(credits=tuple(kept), people=tuple(people.values()))


def _credit(member: dict[str, Any], role: str, job: str, department: str) -> Credit:
    return Credit(
        credit_id=member.get("credit_id"),
        person_id=member.get("id"),
        name=member["name"],
        role=role,
        job=job,
        department=department,
        character=(member.get("character") or None) if role == ACTOR else None,
        billing_order=member.get("order") if role == ACTOR else None,
    )


def _remember(people: dict[int, Person], member: dict[str, Any]) -> None:
    person_id = member.get("id")
    if person_id is None:
        return
    people[person_id] = Person(
        person_id=person_id,
        name=member["name"],
        original_name=member.get("original_name") or None,
        gender=GENDERS.get(member.get("gender") or 0),
        known_for_department=member.get("known_for_department") or None,
        popularity=member.get("popularity"),
        profile_path=member.get("profile_path") or None,
    )


def roles() -> list[str]:
    """Every role a credit can have, actor first."""
    return [ACTOR, *dict.fromkeys(JOB_ROLES.values())]
