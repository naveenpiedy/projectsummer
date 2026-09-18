"""Fetch film metadata from TMDB and build the queryable tables.

Resolution (`resolve.py`) turns Letterboxd URIs into TMDB ids. This module
takes it from there, and never touches Letterboxd:

1. Fetch each film from TMDB -- one request per film, with credits and
   keywords appended, so a film costs a single round trip. The film, its kept
   credits and the people they name are stored together (see `credits.py`).
2. Overlay your own data: watched, rating, watchlist, likes.
3. Rebuild `diary_entries`, one row per viewing.
4. Fetch the details of each credited person -- birthday, birthplace and so on
   -- one request per person, and only once. This comes last because it is
   by far the longest step, so interrupting it leaves everything else done.

Multi-value fields become DuckDB LISTs rather than the pipe-separated strings
the previous version used, so `list_contains(genres, 'Horror')` works without
splitting anything.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, TypeVar

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

#: A short pause after each request, per worker. TMDB allows ~50 requests a
#: second; with WORKERS running at once the real rate settles around 13.
RATE_LIMIT_DELAY = 0.05

#: Requests sent to TMDB at once. A request takes about half a second, almost
#: all of it TMDB's response time, so one at a time manages under two a
#: second -- over two hours for a library's people. Eight at once stays far
#: inside TMDB's ~50 requests a second.
WORKERS = 8

TIMEOUT = 20


class MissingTokenError(LetterboxdError):
    """No TMDB API token is configured."""


class RateLimitedError(LetterboxdError):
    """TMDB kept refusing requests as too frequent, even after waiting."""


class TMDBClient:
    """A small, deliberate wrapper over the handful of TMDB calls needed."""

    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        # requests.Session is not promised to be thread-safe, so each worker
        # thread gets its own -- unless one was handed in, as tests do.
        self._shared_session = session
        if session is not None:
            session.headers.update(self._headers())
        self._local = threading.local()
        # When TMDB says to slow down, every worker waits, not just the one
        # that was told: the limit applies to the token, not the thread.
        self._pause_lock = threading.Lock()
        self._resume_at = 0.0

    @property
    def session(self) -> requests.Session:
        if self._shared_session is not None:
            return self._shared_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers())
            self._local.session = session
        return session

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json", "Authorization": f"Bearer {self.token}"}

    def _wait_for_turn(self) -> None:
        with self._pause_lock:
            wait = self._resume_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def pause(self, seconds: float) -> None:
        """Hold every request, from every thread, for `seconds`."""
        with self._pause_lock:
            self._resume_at = max(self._resume_at, time.monotonic() + seconds)

    def fetch_movie(self, tmdb_id: int, max_retries: int = 3) -> dict[str, Any] | None:
        """Fetch one film, or None if TMDB does not have it."""
        data = self._get(
            f"/movie/{tmdb_id}",
            params={"append_to_response": APPEND_TO_RESPONSE},
            max_retries=max_retries,
        )
        return None if data is None else as_film_row(tmdb_id, data)

    def fetch_person(self, person_id: int, max_retries: int = 3) -> dict[str, Any] | None:
        """Fetch one person's details, or None if TMDB no longer has them."""
        data = self._get(f"/person/{person_id}", max_retries=max_retries)
        return None if data is None else as_person_details(person_id, data)

    def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any] | None:
        """GET a TMDB endpoint, or None if what it names does not exist.

        Handles the two failure modes worth distinguishing: a 404 means the
        thing is genuinely gone and retrying is pointless, while a 429 means
        slow down and try again.

        Raises:
            RateLimitedError: If TMDB is still refusing after every retry.
                Never None: callers record None as "gone for good", which a
                busy moment must not turn into.
        """
        for attempt in range(max_retries):
            self._wait_for_turn()
            try:
                response = self.session.get(
                    f"{TMDB_BASE}{path}", params=params, timeout=TIMEOUT
                )
            except requests.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2**attempt)
                continue

            if response.status_code == 404:
                return None
            if response.status_code == 429:
                if attempt == max_retries - 1:
                    raise RateLimitedError(
                        f"TMDB is refusing requests as too frequent ({path}). Wait a "
                        f"minute and run again; everything fetched so far is kept."
                    )
                self.pause(float(response.headers.get("Retry-After", 5)))
                continue
            if response.status_code >= 400:
                if attempt == max_retries - 1:
                    response.raise_for_status()
                time.sleep(2**attempt)
                continue

            time.sleep(RATE_LIMIT_DELAY)
            return response.json()

        raise AssertionError("unreachable: every attempt returns or raises")


T = TypeVar("T")


def fetch_all(
    ids: list[int],
    fetch: Callable[[int], T],
    description: str,
) -> Iterator[tuple[int, T]]:
    """Call `fetch` for every id, several at a time, yielding results as they land.

    Results arrive in completion order, not id order. Everything the caller
    does with them -- writing to the database above all -- happens on the
    caller's own thread; only the requests run on workers.

    If the caller stops early, or a fetch raises, requests not yet started are
    cancelled. At most a handful already in flight run on to completion, and
    their results are discarded.
    """
    if not ids:
        return
    executor = ThreadPoolExecutor(
        max_workers=min(WORKERS, len(ids)), thread_name_prefix="tmdb"
    )
    try:
        futures = {executor.submit(fetch, item): item for item in ids}
        for future in progress.track(as_completed(futures), description, total=len(ids)):
            yield futures[future], future.result()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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


def as_person_details(person_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """The fields of a TMDB person record that credits do not carry.

    Gender and main department come too: credits sometimes lack them where
    the person's own record has them.
    """
    return {
        "person_id": person_id,
        "birthday": data.get("birthday") or None,
        "deathday": data.get("deathday") or None,
        "place_of_birth": data.get("place_of_birth") or None,
        "also_known_as": [name for name in data.get("also_known_as") or [] if name] or None,
        "imdb_id": data.get("imdb_id") or None,
        "gender": film_credits.GENDERS.get(data.get("gender") or 0),
        "known_for_department": data.get("known_for_department") or None,
    }


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


_PERSON_DETAILS_JSON = (
    '[{"person_id": "BIGINT", "birthday": "VARCHAR", "deathday": "VARCHAR", '
    '"place_of_birth": "VARCHAR", "also_known_as": ["VARCHAR"], "imdb_id": "VARCHAR", '
    '"gender": "VARCHAR", "known_for_department": "VARCHAR"}]'
)


def store_person_details(details: list[dict[str, Any]], gone: list[int]) -> None:
    """Record fetched details, and mark people TMDB no longer has as done.

    A person who is gone is marked fetched anyway, with their details left
    empty -- otherwise every run would ask for them again, forever.
    """
    if not details and not gone:
        return

    conn = db.get_connection()
    conn.execute("BEGIN TRANSACTION")
    try:
        if details:
            conn.execute(
                f"""
                UPDATE people SET
                    birthday = try_cast(d.birthday AS DATE),
                    deathday = try_cast(d.deathday AS DATE),
                    place_of_birth = d.place_of_birth,
                    also_known_as = d.also_known_as,
                    imdb_id = d.imdb_id,
                    gender = coalesce(d.gender, people.gender),
                    known_for_department = coalesce(d.known_for_department,
                                                    people.known_for_department),
                    details_fetched_at = now()
                FROM (
                    SELECT r.person_id, r.birthday, r.deathday, r.place_of_birth,
                           r.also_known_as, r.imdb_id, r.gender, r.known_for_department
                    FROM (SELECT unnest(json_transform(?, '{_PERSON_DETAILS_JSON}')) AS r)
                ) AS d
                WHERE people.person_id = d.person_id
                """,
                [json.dumps(details)],
            )
        if gone:
            conn.execute(
                "UPDATE people SET details_fetched_at = now() "
                "WHERE person_id IN (SELECT unnest(?::BIGINT[]))",
                [gone],
            )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise


def pending_people(limit: int | None = None, films: list[int] | None = None) -> list[int]:
    """Credited people whose details have not been fetched yet.

    Args:
        limit: At most this many.
        films: Only people credited on these films.
    """
    conditions = ["p.details_fetched_at IS NULL"]
    params: list[Any] = []
    if films is not None:
        conditions.append("c.tmdb_id IN (SELECT unnest(?::BIGINT[]))")
        params.append(films)
    rows = db.query(
        f"""
        SELECT DISTINCT p.person_id
        FROM people p
        JOIN film_credits c ON c.person_id = p.person_id
        WHERE {" AND ".join(conditions)}
        ORDER BY p.person_id
        {"LIMIT " + str(int(limit)) if limit else ""}
        """,
        params,
    )
    return [row["person_id"] for row in rows]


#: People whose details are written to the database together. Small enough
#: that an interruption loses little, large enough that writes stay cheap.
PEOPLE_BATCH = 100


@dataclass(frozen=True, slots=True)
class PeopleFetch:
    """What a round of person-detail fetching did."""

    fetched: int
    gone: int


def fetch_people(
    client: TMDBClient,
    limit: int | None = None,
    films: list[int] | None = None,
) -> PeopleFetch:
    """Fetch details for credited people who do not have them yet.

    Written in batches as they arrive, so an interrupted run keeps what it
    fetched and the next one carries on from there.
    """
    pending = pending_people(limit=limit, films=films)
    details: list[dict[str, Any]] = []
    gone: list[int] = []
    fetched = missing = 0

    for person_id, found in fetch_all(pending, client.fetch_person, "Fetching people from TMDB"):
        if found is None:
            gone.append(person_id)
        else:
            details.append(found)
        if len(details) + len(gone) >= PEOPLE_BATCH:
            store_person_details(details, gone)
            fetched, missing = fetched + len(details), missing + len(gone)
            details, gone = [], []

    store_person_details(details, gone)
    return PeopleFetch(fetched=fetched + len(details), gone=missing + len(gone))


def pending_ids(limit: int | None = None) -> list[int]:
    """Films to fetch from TMDB: resolved but never fetched, or without credits.

    The second kind are films stored before credits were kept. Fetching them
    again stores their credits, which is how an existing library catches up.
    That includes films `sync` brought in, which were never resolved and so
    are found through `films` rather than `film_identity`.

    Films TMDB has already said it does not have are left out, however they
    were asked for; otherwise every run would ask again for ever.
    """
    rows = db.query(
        f"""
        SELECT tmdb_id FROM (
            SELECT i.tmdb_id
            FROM film_identity i
            LEFT JOIN films f ON f.tmdb_id = i.tmdb_id
            WHERE i.tmdb_id IS NOT NULL AND f.enriched_at IS NULL
            UNION
            SELECT f.tmdb_id
            FROM films f
            WHERE NOT EXISTS (
                SELECT 1 FROM film_credit_fetches c WHERE c.tmdb_id = f.tmdb_id
            )
        )
        WHERE tmdb_id NOT IN (SELECT tmdb_id FROM films_not_on_tmdb)
        ORDER BY tmdb_id
        {"LIMIT " + str(int(limit)) if limit else ""}
        """
    )
    return [row["tmdb_id"] for row in rows]


def mark_not_on_tmdb(ids: list[int]) -> None:
    """Record films TMDB has no metadata for.

    A film TMDB 404s is never stored in `films`, so nothing else remembers it
    was asked for. Without this it comes back as pending on every run, and is
    counted in `still_pending` for ever.
    """
    if not ids:
        return
    db.get_connection().execute(
        """
        INSERT INTO films_not_on_tmdb (tmdb_id, checked_at)
        SELECT unnest(?::BIGINT[]), now()
        ON CONFLICT (tmdb_id) DO UPDATE SET checked_at = now()
        """,
        [ids],
    )


def forget_not_on_tmdb() -> int:
    """Forget every film TMDB said it did not have, so they are asked for again."""
    rows = db.query("SELECT count(*) AS n FROM films_not_on_tmdb")[0]["n"]
    db.get_connection().execute("DELETE FROM films_not_on_tmdb")
    return rows


def mark_credits_fetched(ids: list[int]) -> None:
    """Record films as done without credits.

    For a film already in the library that TMDB no longer has: there is
    nothing to fetch, and without this every run would ask again.
    """
    if not ids:
        return
    db.get_connection().execute(
        """
        INSERT INTO film_credit_fetches (tmdb_id, fetched_at)
        SELECT tmdb_id, now() FROM films WHERE tmdb_id IN (SELECT unnest(?::BIGINT[]))
        ON CONFLICT (tmdb_id) DO UPDATE SET fetched_at = now()
        """,
        [ids],
    )


def link_list_entries() -> int:
    """Attach TMDB ids to list entries, from resolutions already made.

    A Letterboxd export identifies list films by link alone, so a list entry
    is only comparable with the rest of the library once its link has been
    resolved. Entries whose film was never resolved keep a NULL tmdb_id.

    Returns:
        How many entries now carry an id.
    """
    db.get_connection().execute(
        """
        UPDATE list_entries e
        SET tmdb_id = i.tmdb_id
        FROM film_identity i
        WHERE i.letterboxd_uri = e.letterboxd_uri
          AND i.tmdb_id IS NOT NULL
          AND e.tmdb_id IS DISTINCT FROM i.tmdb_id
        """
    )
    return db.query("SELECT count(tmdb_id) AS n FROM list_entries")[0]["n"]


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
    """Films fetched from TMDB in this run, including films already in the
    library that were fetched again for their cast and crew."""
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
    """Films still waiting to be fetched: never fetched, or without their cast
    and crew."""
    people_fetched: int
    """People whose birthday, birthplace and other details were fetched."""
    people_not_on_tmdb: int
    """Of those asked for, people TMDB no longer has."""
    people_pending: int
    """Credited people still waiting for their details."""


def enrich_all(
    limit: int | None = None,
    token: str | None = None,
    client: TMDBClient | None = None,
    retry_missing: bool = False,
) -> EnrichResult:
    """Fetch metadata for everything resolved, build the real tables, then people.

    Safe to interrupt and re-run: films and people are written in batches as
    they are fetched, and a second run picks up only what is still missing.
    People come last, so an interruption there leaves films, your viewing
    data and the diary complete.
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

    if retry_missing:
        forgotten = forget_not_on_tmdb()
        if forgotten:
            progress.note(f"Asking again for {forgotten} films TMDB did not have")

    pending = pending_ids(limit)
    fetched: list[dict[str, Any]] = []
    missing: list[int] = []

    for tmdb_id, row in fetch_all(pending, client.fetch_movie, "Fetching metadata from TMDB"):
        if row is None:
            missing.append(tmdb_id)
            continue
        fetched.append(row)
        if len(fetched) >= 50:
            store_films(fetched)
            fetched = []

    store_films(fetched)
    mark_credits_fetched(missing)
    mark_not_on_tmdb(missing)

    progress.note("Applying your viewing data")
    link_list_entries()
    watched_or_listed = apply_user_state()
    progress.note("Rebuilding diary entries")
    diary = rebuild_diary()

    people = fetch_people(client, limit=limit)

    return EnrichResult(
        attempted=len(pending),
        enriched=len(pending) - len(missing),
        not_on_tmdb=len(missing),
        films_total=db.query("SELECT count(*) AS n FROM films")[0]["n"],
        with_your_data=watched_or_listed,
        diary_entries=diary.diary_entries,
        unmatched=diary.unmatched,
        still_pending=len(pending_ids()),
        people_fetched=people.fetched,
        people_not_on_tmdb=people.gone,
        people_pending=len(pending_people()),
    )
