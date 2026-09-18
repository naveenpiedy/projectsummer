"""Every query in docs/cookbook.md, run against a small library.

A cookbook nobody runs goes stale the first time a column is renamed. Each
recipe here is extracted from the Markdown, checked to be a single SELECT --
the same check `query` applies -- and run. All but the ones listed in
`MAY_BE_EMPTY` must return rows, so a recipe that quietly matches nothing is a
failure rather than a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from projectsummer.core import db
from projectsummer.core.querying import require_single_select, run_select

COOKBOOK = Path(__file__).resolve().parent.parent / "docs" / "cookbook.md"

#: Recipes that are right to return nothing on a healthy library.
MAY_BE_EMPTY = {"Dangling references"}

# tmdb_id, title, year, runtime, language, genres, keywords, rating, tmdb
# rating, votes, watched, watchlist, rated_on
FILMS = [
    (1, "Barry Lyndon", 1975, 185, "en", ["Drama"], ["period"], 4.5, 8.0, 500, True, False, "2024-01-02"),
    (2, "The Shining", 1980, 146, "en", ["Horror"], ["hotel"], 5.0, 8.2, 900, True, False, "2024-02-02"),
    (3, "Paths of Glory", 1957, 88, "en", ["War", "Drama"], ["trial"], 4.0, 8.3, 300, True, False, "2025-06-02"),
    (4, "The Piano", 1993, 121, "en", ["Drama"], ["piano"], 3.5, 7.5, 200, True, False, None),
    (5, "Lost in Translation", 2003, 102, "ja", ["Drama"], ["tokyo"], 2.0, 7.7, 1000, True, False, None),
    (6, "Days of Heaven", 1978, 94, "en", ["Drama"], ["harvest"], 3.0, 7.5, 150, True, False, None),
    (7, "Solaris", 1972, 167, "ru", ["Science Fiction"], ["space"], None, 8.0, 400, False, True, None),
    (8, "Eyes Wide Shut", 1999, 159, "en", ["Drama"], ["masks"], None, 7.4, 600, False, True, None),
]

# person_id, name, gender, place_of_birth
PEOPLE = [
    (1, "Stanley Kubrick", "male", "New York City, New York, USA"),
    (2, "Jane Campion", "female", "Wellington, New Zealand"),
    (3, "Sofia Coppola", "female", "New York City, New York, USA"),
    (4, "Terrence Malick", "male", "Ottawa, Illinois, USA"),
    (5, "Jack Nicholson", "male", "Neptune, New Jersey, USA"),
    (6, "Holly Hunter", "female", "Conyers, Georgia, USA"),
]

# tmdb_id, person_id, role
CREDITS = [
    (1, 1, "director"), (2, 1, "director"), (3, 1, "director"), (8, 1, "director"),
    (4, 2, "director"), (5, 3, "director"),
    (6, 4, "director"), (6, 4, "writer"),  # wrote and directed the same film
    (2, 5, "actor"), (1, 5, "actor"),
    (4, 6, "actor"), (5, 6, "actor"),
]

# tmdb_id, watched_date, rating, rewatch, review
VIEWINGS = [
    (1, "2024-01-01", 4.5, False, "Every frame a painting."),
    (2, "2024-02-01", 5.0, False, None),
    (2, "2025-03-01", 5.0, True, "Better the second time."),
    (3, "2025-06-01", 4.0, False, None),
    (5, "2025-07-01", 2.0, False, None),
]


@pytest.fixture(scope="module")
def library():
    """A library small enough to read, wide enough for every recipe."""
    db.close_connection()
    conn = db.get_connection(":memory:")
    conn.executemany(
        """
        INSERT INTO films (tmdb_id, title, year, runtime, original_language, genres,
                           keywords, my_rating, tmdb_rating, tmdb_vote_count,
                           watched, on_watchlist, rated_on, enriched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, try_cast(? AS DATE), now())
        """,
        FILMS,
    )
    conn.executemany(
        "INSERT INTO people (person_id, name, gender, place_of_birth, details_fetched_at) VALUES (?, ?, ?, ?, now())",
        PEOPLE,
    )
    conn.executemany(
        """
        INSERT INTO film_credits (credit_id, tmdb_id, person_id, role, job, department)
        VALUES (?, ?, ?, ?, '', '')
        """,
        [(f"c{index}", *credit) for index, credit in enumerate(CREDITS)],
    )
    conn.executemany(
        """
        INSERT INTO diary_entries (tmdb_id, watched_date, rating, rewatch, review)
        VALUES (?, try_cast(? AS DATE), ?, ?, ?)
        """,
        VIEWINGS,
    )
    conn.execute("INSERT INTO lists (list_id, slug, name) VALUES (1, 'faves', 'Favourites'), (2, 'ideas', 'Ideas')")
    conn.executemany(
        "INSERT INTO list_entries (list_id, entry_position, tmdb_id, name) VALUES (?, ?, ?, 'x')",
        [(1, 1, 1), (1, 2, 2), (1, 3, 7), (2, 1, 7), (2, 2, 8)],
    )
    yield conn
    db.close_connection()


def recipes() -> list[tuple[str, str]]:
    """Every ```sql block in the cookbook, under the heading it sits below."""
    found, heading = [], "(no heading)"
    for part in re.split(r"(?m)^(#{2,3} .+)$", COOKBOOK.read_text(encoding="utf-8")):
        if part.startswith("#"):
            heading = part.lstrip("# ").strip()
            continue
        found += [(heading, sql.strip()) for sql in re.findall(r"```sql\n(.*?)```", part, re.S)]
    return found


def test_the_cookbook_holds_recipes():
    """Guards the extraction itself: a rename that broke it would otherwise
    turn every test below into a silent pass."""
    assert len(recipes()) >= 15


@pytest.mark.parametrize("heading, sql", recipes(), ids=lambda value: value[:40])
def test_a_recipe_runs_and_returns_something(heading, sql, library):
    require_single_select(sql)
    result = run_select(sql)

    if heading in MAY_BE_EMPTY:
        assert result.rows == []
    else:
        assert result.rows, f"{heading!r} matched nothing"
