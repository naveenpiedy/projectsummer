"""Fetch film metadata from TMDB and build the queryable tables.

Resolution (`resolve.py`) turns Letterboxd URIs into TMDB ids. This module
takes it from there, and never touches Letterboxd:

1. Fetch each film from TMDB -- one request per film, with credits and
   keywords appended, so a film costs a single round trip. The film, its kept
   credits and the people they name are stored together (see `credits.py`).
2. Overlay your own data: watched, rating, watchlist, likes.
3. Rebuild `diary_entries`, one row per viewing.

Multi-value fields become DuckDB LISTs rather than the pipe-separated strings
the previous version used, so `list_contains(genres, 'Horror')` works without
splitting anything.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

import duckdb

from projectsummer.core import credits as film_credits
from projectsummer.core import db, progress
from projectsummer.core.credits import FilmCredits
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.results import Result

TMDB_BASE = "https://api.themoviedb.org/3"

#: Everything needed for one film in a single request.
APPEND_TO_RESPONSE = "credits,keywords,external_ids"

#: TMDB allows ~50 requests/second. 20/s leaves generous headroom and still
#: gets through a thousand-film library in about a minute.
RATE_LIMIT_DELAY = 0.05

TIMEOUT = 20


class MissingTokenError(LetterboxdError):
    """No TMDB API token is configured."""


class TMDBClient:
    """A small, deliberate wrapper over the handful of TMDB calls needed."""

    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        self.session = session or requests.Session()
        self.session.headers.update(
            {"accept": "application/json", "Authorization": f"Bearer {token}"}
        )

    def fetch_movie(self, tmdb_id: int, max_retries: int = 3) -> dict[str, Any] | None:
        """Fetch one film, or None if TMDB does not have it.

        Handles the two failure modes worth distinguishing: a 404 means the
        film is genuinely gone and retrying is pointless, while a 429 means
        slow down and try again.
        """
        for attempt in range(max_retries):
            try:
                response = self.session.get(
                    f"{TMDB_BASE}/movie/{tmdb_id}",
                    params={"append_to_response": APPEND_TO_RESPONSE},
                    timeout=TIMEOUT,
                )
            except requests.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2**attempt)
                continue

            if response.status_code == 404:
                return None
            if response.status_code == 429:
                time.sleep(int(response.headers.get("Retry-After", 5)))
                continue
            if response.status_code >= 400:
                if attempt == max_retries - 1:
                    response.raise_for_status()
                time.sleep(2**attempt)
                continue

            time.sleep(RATE_LIMIT_DELAY)
            return as_film_row(tmdb_id, response.json())

        return None


# ------------------------------------------------------------- extraction

def as_film_row(tmdb_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Flatten a TMDB response into the shape of a `films` row.

    The row also carries the film's kept credits under ``film_credits``, which
    `store_films` writes alongside it.
    """
    credits = film_credits.extract(data.get("credits"))
    return {
        "tmdb_id": tmdb_id,
        "imdb_id": (data.get("external_ids") or {}).get("imdb_id"),
        "title": data.get("title") or data.get("original_title") or f"TMDB {tmdb_id}",
        "original_title": data.get("original_title"),
        "release_date": data.get("release_date") or None,
        "runtime": data.get("runtime"),
        "overview": data.get("overview") or None,
        "tagline": data.get("tagline") or None,
        "poster_path": data.get("poster_path"),
        "directors": credits.names("director"),
        "cast_members": credits.names(film_credits.ACTOR),
        "writers": credits.names("writer"),
        "composers": credits.names("composer"),
        "cinematographers": credits.names("cinematographer"),
        "editors": credits.names("editor"),
        "genres": names_of(data.get("genres")),
        "keywords": names_of((data.get("keywords") or {}).get("keywords")),
        "original_language": data.get("original_language"),
        "spoken_languages": names_of(data.get("spoken_languages"), key="english_name"),
        "production_companies": names_of(data.get("production_companies")),
        "production_countries": names_of(data.get("production_countries")),
        "budget": data.get("budget") or None,
        "revenue": data.get("revenue") or None,
        "status": data.get("status"),
        "tmdb_rating": data.get("vote_average"),
        "tmdb_vote_count": data.get("vote_count"),
        "tmdb_popularity": data.get("popularity"),
        "film_credits": credits,
    }


def names_of(entries: list[dict[str, Any]] | None, key: str = "name") -> list[str] | None:
    """Pull a name out of each entry of a TMDB sub-list."""
    if not entries:
        return None
    found = [entry[key] for entry in entries if entry.get(key)]
    return found or None


# ----------------------------------------------------------------- storage

_FILM_COLUMNS = (
    "tmdb_id", "imdb_id", "title", "original_title", "release_date", "runtime",
    "overview", "tagline", "poster_path", "directors", "cast_members", "writers",
    "composers", "cinematographers", "editors", "genres", "keywords",
    "original_language", "spoken_languages", "production_companies",
    "production_countries", "budget", "revenue", "status", "tmdb_rating",
    "tmdb_vote_count", "tmdb_popularity",
)


#: JSON shapes for bulk loading. Handing DuckDB one JSON string and letting it
#: build the rows is orders of magnitude faster than binding Python values:
#: a thousand credits take milliseconds this way and half a minute through
#: executemany, which converts and upserts one row at a time.
_FILMS_JSON = (
    '[{"tmdb_id": "BIGINT", "imdb_id": "VARCHAR", "title": "VARCHAR", '
    '"original_title": "VARCHAR", "release_date": "VARCHAR", "runtime": "INTEGER", '
    '"overview": "VARCHAR", "tagline": "VARCHAR", "poster_path": "VARCHAR", '
    '"directors": ["VARCHAR"], "cast_members": ["VARCHAR"], "writers": ["VARCHAR"], '
    '"composers": ["VARCHAR"], "cinematographers": ["VARCHAR"], "editors": ["VARCHAR"], '
    '"genres": ["VARCHAR"], "keywords": ["VARCHAR"], "original_language": "VARCHAR", '
    '"spoken_languages": ["VARCHAR"], "production_companies": ["VARCHAR"], '
    '"production_countries": ["VARCHAR"], "budget": "BIGINT", "revenue": "BIGINT", '
    '"status": "VARCHAR", "tmdb_rating": "DOUBLE", "tmdb_vote_count": "INTEGER", '
    '"tmdb_popularity": "DOUBLE"}]'
)

def store_films(rows: list[dict[str, Any]]) -> None:
    """Insert or refresh films, with their credits and people, leaving user state untouched.

    One transaction, so a film is never stored without its credits or the
    other way round.
    """
    if not rows:
        return

    conn = db.get_connection()
    conn.execute("BEGIN TRANSACTION")
    try:
        _store_film_rows(conn, rows)
        store_credits(
            conn,
            [(row["tmdb_id"], row["film_credits"]) for row in rows if "film_credits" in row],
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except duckdb.Error:
            pass  # already aborted by DuckDB; the original error matters more
        raise


def _store_film_rows(conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    columns = ", ".join(_FILM_COLUMNS)
    selected = ", ".join(
        "try_cast(f.release_date AS DATE)" if name == "release_date" else f"f.{name}"
        for name in _FILM_COLUMNS
    )
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in _FILM_COLUMNS if name != "tmdb_id"
    )

    conn.execute(
        f"""
        INSERT INTO films ({columns}, year, enriched_at, updated_at)
        SELECT {selected}, NULL, now(), now()
        FROM (SELECT unnest(json_transform(?, '{_FILMS_JSON}')) AS f)
        ON CONFLICT (tmdb_id) DO UPDATE SET
            {updates}, enriched_at = now(), updated_at = now()
        """,
        [json.dumps([{name: row.get(name) for name in _FILM_COLUMNS} for row in rows])],
    )
    # Derived from release_date rather than stored twice and left to disagree.
    conn.execute(
        "UPDATE films SET year = year(release_date) WHERE release_date IS NOT NULL"
    )


_PEOPLE_JSON = (
    '[{"person_id": "BIGINT", "name": "VARCHAR", "original_name": "VARCHAR", '
    '"gender": "VARCHAR", "known_for_department": "VARCHAR", '
    '"popularity": "DOUBLE", "profile_path": "VARCHAR"}]'
)
_CREDITS_JSON = (
    '[{"credit_id": "VARCHAR", "tmdb_id": "BIGINT", "person_id": "BIGINT", '
    '"role": "VARCHAR", "job": "VARCHAR", "department": "VARCHAR", '
    '"character": "VARCHAR", "billing_order": "INTEGER"}]'
)


def store_credits(
    conn: duckdb.DuckDBPyConnection,
    films: list[tuple[int, FilmCredits]],
) -> None:
    """Replace each film's credits, and add or refresh the people they name.

    A film's credits are replaced wholesale, so a credit TMDB has since
    corrected or removed does not linger. A person is refreshed only in what
    credits say about them -- never their birthday or birthplace, which come
    from a separate call -- and a credit that does not know their gender does
    not erase one already known.
    """
    if not films:
        return

    ids = [tmdb_id for tmdb_id, _ in films]
    conn.execute(
        "DELETE FROM film_credits WHERE tmdb_id IN (SELECT unnest(?::BIGINT[]))", [ids]
    )

    people = {
        person.person_id: {
            "person_id": person.person_id,
            "name": person.name,
            "original_name": person.original_name,
            "gender": person.gender,
            "known_for_department": person.known_for_department,
            "popularity": person.popularity,
            "profile_path": person.profile_path,
        }
        for _, credits in films
        for person in credits.people
    }
    if people:
        conn.execute(
            f"""
            INSERT INTO people (person_id, name, original_name, gender,
                                known_for_department, popularity, profile_path)
            SELECT p.person_id, p.name, p.original_name, p.gender,
                   p.known_for_department, p.popularity, p.profile_path
            FROM (SELECT unnest(json_transform(?, '{_PEOPLE_JSON}')) AS p)
            ON CONFLICT (person_id) DO UPDATE SET
                name = excluded.name,
                original_name = coalesce(excluded.original_name, people.original_name),
                gender = coalesce(excluded.gender, people.gender),
                known_for_department = coalesce(excluded.known_for_department,
                                                people.known_for_department),
                popularity = coalesce(excluded.popularity, people.popularity),
                profile_path = coalesce(excluded.profile_path, people.profile_path)
            """,
            [json.dumps(list(people.values()))],
        )

    rows = [
        {
            "credit_id": credit.credit_id,
            "tmdb_id": tmdb_id,
            "person_id": credit.person_id,
            "role": credit.role,
            "job": credit.job,
            "department": credit.department,
            "character": credit.character,
            "billing_order": credit.billing_order,
        }
        for tmdb_id, credits in films
        for credit in credits.storable_credits()
    ]
    if rows:
        conn.execute(
            f"""
            INSERT INTO film_credits (credit_id, tmdb_id, person_id, role, job,
                                      department, character, billing_order)
            SELECT c.credit_id, c.tmdb_id, c.person_id, c.role, c.job,
                   c.department, c.character, c.billing_order
            FROM (SELECT unnest(json_transform(?, '{_CREDITS_JSON}')) AS c)
            ON CONFLICT (credit_id) DO UPDATE SET
                tmdb_id = excluded.tmdb_id, person_id = excluded.person_id,
                role = excluded.role, job = excluded.job,
                department = excluded.department, character = excluded.character,
                billing_order = excluded.billing_order
            """,
            [json.dumps(rows)],
        )

    conn.execute(
        """
        INSERT INTO film_credit_fetches (tmdb_id, fetched_at)
        SELECT unnest(?::BIGINT[]), now()
        ON CONFLICT (tmdb_id) DO UPDATE SET fetched_at = now()
        """,
        [ids],
    )


def pending_ids(limit: int | None = None) -> list[int]:
    """Resolved films whose metadata has not been fetched yet."""
    rows = db.query(
        f"""
        SELECT DISTINCT i.tmdb_id
        FROM film_identity i
        LEFT JOIN films f ON f.tmdb_id = i.tmdb_id
        WHERE i.tmdb_id IS NOT NULL AND f.enriched_at IS NULL
        ORDER BY i.tmdb_id
        {"LIMIT " + str(int(limit)) if limit else ""}
        """
    )
    return [row["tmdb_id"] for row in rows]


def apply_user_state() -> int:
    """Copy watched / watchlist / liked / rating from staging onto `films`.

    Aggregated by tmdb_id, because more than one Letterboxd URI can resolve
    to the same film and the states have to be combined rather than one of
    them winning arbitrarily.
    """
    db.get_connection().execute(
        """
        WITH state AS (
            SELECT i.tmdb_id,
                   bool_or(s.watched)          AS watched,
                   max(s.watched_logged_on)    AS watched_logged_on,
                   bool_or(s.on_watchlist)     AS on_watchlist,
                   max(s.watchlist_added_on)   AS watchlist_added_on,
                   bool_or(s.liked)            AS liked,
                   max(s.liked_on)             AS liked_on,
                   max(s.my_rating)            AS my_rating,
                   max(s.rated_on)             AS rated_on,
                   any_value(s.letterboxd_uri) AS letterboxd_uri
            FROM staging_films s
            JOIN film_identity i ON i.letterboxd_uri = s.letterboxd_uri
            WHERE i.tmdb_id IS NOT NULL
            GROUP BY i.tmdb_id
        )
        UPDATE films f SET
            watched            = state.watched,
            watched_logged_on  = state.watched_logged_on,
            on_watchlist       = state.on_watchlist,
            watchlist_added_on = state.watchlist_added_on,
            liked              = state.liked,
            liked_on           = state.liked_on,
            my_rating          = state.my_rating,
            rated_on           = state.rated_on,
            letterboxd_uri     = state.letterboxd_uri,
            updated_at         = now()
        FROM state
        WHERE f.tmdb_id = state.tmdb_id
        """
    )
    return db.query("SELECT count(*) AS n FROM films WHERE watched OR on_watchlist")[0]["n"]


class DiaryRebuild(Result):
    """How much of the imported diary matched an enriched film."""

    diary_entries: int
    """Diary rows now attached to a film."""
    unmatched: int
    """Imported diary rows with no enriched film to attach to."""


def rebuild_diary() -> DiaryRebuild:
    """Rebuild `diary_entries` from staging, one row per viewing.

    Diary URIs identify a *viewing*, not a film, and share no namespace with
    the film URIs -- so entries are matched to films on (name, year), which
    is why both are kept in staging.
    """
    conn = db.get_connection()
    conn.execute("DELETE FROM diary_entries")
    conn.execute(
        """
        INSERT INTO diary_entries
            (tmdb_id, watched_date, logged_date, rating, rewatch, tags, review,
             letterboxd_uri)
        WITH film_by_title AS (
            SELECT s.name, s.year, min(i.tmdb_id) AS tmdb_id
            FROM staging_films s
            JOIN film_identity i ON i.letterboxd_uri = s.letterboxd_uri
            WHERE i.tmdb_id IS NOT NULL
            GROUP BY s.name, s.year
        )
        SELECT m.tmdb_id, d.watched_date, d.logged_date, d.rating, d.rewatch,
               d.tags, d.review, d.entry_uri
        FROM staging_diary d
        JOIN film_by_title m
          ON m.name = d.name AND m.year IS NOT DISTINCT FROM d.year
        -- The natural key is (tmdb_id, watched_date, logged_date); keep one
        -- row per key so a duplicate in the export cannot break the import.
        QUALIFY row_number() OVER (
            PARTITION BY m.tmdb_id, d.watched_date, d.logged_date
            ORDER BY d.entry_uri
        ) = 1
        """
    )

    total = db.query("SELECT count(*) AS n FROM staging_diary")[0]["n"]
    matched = db.query("SELECT count(*) AS n FROM diary_entries")[0]["n"]
    return DiaryRebuild(diary_entries=matched, unmatched=total - matched)


# ------------------------------------------------------------------ driver

class EnrichResult(Result):
    """What an enrichment run fetched and built."""

    attempted: int
    """Films fetched from TMDB in this run."""
    enriched: int
    """Of those, films TMDB returned metadata for."""
    not_on_tmdb: int
    """Of those, films TMDB has no record of."""
    films_total: int
    """Films in the library after this run."""
    with_your_data: int
    """Films you have watched or put on your watchlist."""
    diary_entries: int
    """Diary rows attached to a film."""
    unmatched: int
    """Imported diary rows with no enriched film to attach to."""
    still_pending: int
    """Resolved films still waiting to be fetched."""


def enrich_all(
    limit: int | None = None,
    token: str | None = None,
    client: TMDBClient | None = None,
) -> EnrichResult:
    """Fetch metadata for everything resolved, then build the real tables.

    Safe to interrupt and re-run: films are written in batches as they are
    fetched, and a second run picks up only what is still missing.
    """
    from projectsummer import config

    if client is None:
        token = token or config.tmdb_token()
        if not token:
            raise MissingTokenError(
                "No TMDB API token. Create a free account at "
                "https://www.themoviedb.org/settings/api, then set "
                "TMDB_API_KEY in your environment or in a .env file."
            )
        client = TMDBClient(token)

    pending = pending_ids(limit)
    fetched: list[dict[str, Any]] = []
    missing: list[int] = []

    for tmdb_id in progress.track(pending, "Fetching metadata from TMDB"):
        row = client.fetch_movie(tmdb_id)
        if row is None:
            missing.append(tmdb_id)
            continue
        fetched.append(row)
        if len(fetched) >= 50:
            store_films(fetched)
            fetched = []

    store_films(fetched)

    progress.note("Applying your viewing data")
    watched_or_listed = apply_user_state()
    progress.note("Rebuilding diary entries")
    diary = rebuild_diary()

    return EnrichResult(
        attempted=len(pending),
        enriched=len(pending) - len(missing),
        not_on_tmdb=len(missing),
        films_total=db.query("SELECT count(*) AS n FROM films")[0]["n"],
        with_your_data=watched_or_listed,
        diary_entries=diary.diary_entries,
        unmatched=diary.unmatched,
        still_pending=len(pending_ids()),
    )
