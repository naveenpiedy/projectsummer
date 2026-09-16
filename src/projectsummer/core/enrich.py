"""Fetch film metadata from TMDB and build the queryable tables.

Resolution (`resolve.py`) turns Letterboxd URIs into TMDB ids. This module
takes it from there, and never touches Letterboxd:

1. Fetch each film from TMDB -- one request per film, with credits and
   keywords appended, so a film costs a single round trip.
2. Overlay your own data: watched, rating, watchlist, likes.
3. Rebuild `diary_entries`, one row per viewing.

Multi-value fields become DuckDB LISTs rather than the pipe-separated strings
the previous version used, so `list_contains(genres, 'Horror')` works without
splitting anything.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from projectsummer.core import db, progress
from projectsummer.core.errors import LetterboxdError

TMDB_BASE = "https://api.themoviedb.org/3"

#: Everything needed for one film in a single request.
APPEND_TO_RESPONSE = "credits,keywords,external_ids"

#: TMDB allows ~50 requests/second. 20/s leaves generous headroom and still
#: gets through a thousand-film library in about a minute.
RATE_LIMIT_DELAY = 0.05

TIMEOUT = 20

#: How many cast members to keep. The full list runs to hundreds for big
#: productions, and the tail is bit parts nobody queries by.
CAST_LIMIT = 10


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
    """Flatten a TMDB response into the shape of a `films` row."""
    credits = data.get("credits") or {}
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
        "directors": crew_named(credits, jobs={"Director"}),
        "cast_members": leading_cast(credits),
        "writers": crew_named(credits, jobs={"Writer", "Screenplay", "Story"}),
        "composers": crew_named(credits, jobs={"Original Music Composer", "Music"}),
        "cinematographers": crew_named(credits, jobs={"Director of Photography"}),
        "editors": crew_named(credits, jobs={"Editor"}),
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
    }


def names_of(entries: list[dict[str, Any]] | None, key: str = "name") -> list[str] | None:
    """Pull a name out of each entry of a TMDB sub-list."""
    if not entries:
        return None
    found = [entry[key] for entry in entries if entry.get(key)]
    return found or None


def crew_named(credits: dict[str, Any], jobs: set[str]) -> list[str] | None:
    """Crew holding any of `jobs`, in credit order and without repeats.

    A person can be credited twice (Writer and Screenplay, say), and the same
    job can be shared, so order is preserved but duplicates are dropped.
    """
    seen: dict[str, None] = {}
    for member in credits.get("crew") or []:
        if member.get("job") in jobs and member.get("name"):
            seen.setdefault(member["name"], None)
    return list(seen) or None


def leading_cast(credits: dict[str, Any]) -> list[str] | None:
    cast = credits.get("cast") or []
    names = [member["name"] for member in cast[:CAST_LIMIT] if member.get("name")]
    return names or None


# ----------------------------------------------------------------- storage

_FILM_COLUMNS = (
    "tmdb_id", "imdb_id", "title", "original_title", "release_date", "runtime",
    "overview", "tagline", "poster_path", "directors", "cast_members", "writers",
    "composers", "cinematographers", "editors", "genres", "keywords",
    "original_language", "spoken_languages", "production_companies",
    "production_countries", "budget", "revenue", "status", "tmdb_rating",
    "tmdb_vote_count", "tmdb_popularity",
)


def store_films(rows: list[dict[str, Any]]) -> None:
    """Insert or refresh film metadata, leaving user state untouched."""
    if not rows:
        return

    columns = ", ".join(_FILM_COLUMNS)
    placeholders = ", ".join(
        "try_cast(? AS DATE)" if name == "release_date" else "?"
        for name in _FILM_COLUMNS
    )
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in _FILM_COLUMNS if name != "tmdb_id"
    )

    db.get_connection().executemany(
        f"""
        INSERT INTO films ({columns}, year, enriched_at, updated_at)
        VALUES ({placeholders}, NULL, now(), now())
        ON CONFLICT (tmdb_id) DO UPDATE SET
            {updates}, enriched_at = now(), updated_at = now()
        """,
        [tuple(row.get(name) for name in _FILM_COLUMNS) for row in rows],
    )
    # Derived from release_date rather than stored twice and left to disagree.
    db.get_connection().execute(
        "UPDATE films SET year = year(release_date) WHERE release_date IS NOT NULL"
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


def rebuild_diary() -> dict[str, int]:
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
    return {"diary_entries": matched, "unmatched": total - matched}


# ------------------------------------------------------------------ driver

def enrich_all(
    limit: int | None = None,
    token: str | None = None,
    client: TMDBClient | None = None,
) -> dict[str, Any]:
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

    return {
        "attempted": len(pending),
        "enriched": len(pending) - len(missing),
        "not_on_tmdb": len(missing),
        "films_total": db.query("SELECT count(*) AS n FROM films")[0]["n"],
        "with_your_data": watched_or_listed,
        **diary,
        "still_pending": len(pending_ids()),
    }
