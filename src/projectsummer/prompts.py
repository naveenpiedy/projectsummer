"""Prompts the MCP server offers: whole questions, not single tool calls.

A tool is a verb; a prompt is a conversation worth having. These are the ones
that take several steps and a particular order -- a year in review reads the
diary, the ratings and the people behind them -- so a client can offer them by
name instead of the person having to describe the whole thing again.

Each function returns the text of a user message. They name the tools to
reach for, because the server lists only a few and leaves the rest to
`search_tools`, and they say to ask the database for answers rather than rows:
a model that fetches a hundred films to count them spends fifty thousand
tokens on arithmetic.

They are text, never SQL: a prompt cannot read the library itself, and nothing
here runs against the database.
"""

from __future__ import annotations

from collections.abc import Callable

#: How every prompt ends. Repeated advice, kept in one place.
_HOUSE_STYLE = (
    "Use describe_schema before writing SQL, and check a value's exact "
    "spelling there before filtering on it. Ask query for answers -- counts, "
    "averages, top tens -- rather than fetching rows to work them out. Say "
    "when a figure rests on a handful of films, and when something is missing "
    "rather than zero: NULL means unknown."
)


def year_in_review(year: str) -> str:
    """Look back over a year of watching, the way a wrapped-up summary would."""
    return (
        f"Write me a year in review for {year}, from my Letterboxd library.\n\n"
        f"Start with trends for that year, month by month, for the shape of "
        f"it: when I watched most, what I watched, how much I rated.\n\n"
        f"Then go beyond the counts:\n"
        f"- the films of the year for me, and the ones I regretted\n"
        f"- what changed from the year before: more or fewer films, different "
        f"genres, languages or decades, a harsher or kinder average\n"
        f"- the people behind the year: directors and actors I saw most, and "
        f"anyone new to me\n"
        f"- rewatches, and what they say\n"
        f"- where I disagreed with everyone else\n\n"
        f"Write it as a few paragraphs I would enjoy reading, with the "
        f"numbers in them rather than in a table. {_HOUSE_STYLE}"
    )


def what_should_i_watch(mood: str = "anything", minutes: str = "") -> str:
    """Pick something off the watchlist, for a mood and an evening's length."""
    length = f" I have about {minutes} minutes." if minutes.strip() else ""
    return (
        f"Help me choose something from my watchlist tonight. I am in the "
        f"mood for: {mood}.{length}\n\n"
        f"Work from my watchlist, not from films in general. Weigh what I "
        f"already rate highly -- directors, genres, languages, decades -- "
        f"against what the mood asks for, and do not suggest anything I have "
        f"already watched.\n\n"
        f"Give me three candidates, best first. For each: the film, why it "
        f"fits what I asked for, and what in my own history makes you think I "
        f"will like it. Then say which one you would put on, and why.\n\n"
        f"{_HOUSE_STYLE}"
    )


def taste_profile() -> str:
    """Describe what my ratings say about me, and where they mislead."""
    return (
        "Tell me what my ratings say about my taste.\n\n"
        "Cover what I rate highly and what I do not -- directors, actors, "
        "genres, decades, languages, countries -- and where I differ from the "
        "crowd, both the films I love that others do not and the reverse.\n\n"
        "Then the harder part: what would surprise me? A pattern I probably "
        "have not noticed, something my ratings say that my lists do not, or a "
        "blind spot worth filling. Be specific and name films.\n\n"
        "Be honest about how solid each claim is: an average over three films "
        "is a hint, not a fact. " + _HOUSE_STYLE
    )


def person_deep_dive(name: str) -> str:
    """Everything my library holds about one director, actor or writer."""
    return (
        f"Tell me about {name} in my library.\n\n"
        f"Find them in people and film_credits by person_id rather than by "
        f"name, since two people can share a name, and say so if more than one "
        f"person matches.\n\n"
        f"Cover: which of their films I have seen and when, how I rated them "
        f"against my own average and against TMDB's, what roles they held, who "
        f"they keep working with among the people in my library, and what of "
        f"theirs is on my watchlist or missing from my library altogether.\n\n"
        f"{_HOUSE_STYLE}"
    )


def catch_up() -> str:
    """Fetch recent Letterboxd activity and say what changed."""
    return (
        "Catch my library up with Letterboxd, then tell me what changed.\n\n"
        "Run sync first. Then, for whatever it brought in: what I watched, "
        "how I rated it against my usual, whether it fits what I have been "
        "watching lately or is a departure, and anything it completes -- a "
        "director's filmography, a list I am close to finishing.\n\n"
        "If sync found nothing new, say so plainly and tell me what I last "
        "watched instead. " + _HOUSE_STYLE
    )


#: Every prompt the server offers, in the order a client should show them.
PROMPTS: tuple[Callable[..., str], ...] = (
    year_in_review,
    what_should_i_watch,
    taste_profile,
    person_deep_dive,
    catch_up,
)
