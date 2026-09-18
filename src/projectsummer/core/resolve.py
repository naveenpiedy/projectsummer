"""Turn Letterboxd URIs into TMDB ids.

A Letterboxd export identifies films only by a `boxd.it` short link -- no TMDB
or IMDb id anywhere in the 38 files it contains. The ids are on the film's
Letterboxd page, so resolution means fetching that page and reading them out.

This is the only part of the tool that reads Letterboxd's website, and it is
built to do so once per film, ever:

* Results are cached in `film_identity`, keyed by URI, and a cached film is
  never fetched again -- not on a re-import, not on a later export.
* Slugs are cached too. Several URIs can point at the same film (a short
  link, a full link, a diary entry), and once any of them resolves, the
  others are satisfied from the slug without a request.
* Requests are spaced by a delay and identify the tool honestly.
* Only Letterboxd's own hosts are fetched. An export is a file like any
  other -- it can be edited, and it can come from someone else -- and every
  URI in it is looked up.
* Failures are recorded rather than retried in a loop, so a second run picks
  up only what is genuinely outstanding.

Metadata never comes from here. Once a film has a TMDB id, everything else is
fetched from TMDB's API, which is what it is for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from projectsummer.core import db, progress
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.results import Result

#: Sent with every request. An honest identifier is the least a scraper owes
#: the site it reads: it lets an operator see what the traffic is and block it
#: specifically rather than having to guess.
USER_AGENT = (
    "projectsummer/2.0 (+https://github.com/naveenpiedy/projectsummer) "
    "personal Letterboxd library tool"
)

#: Seconds between requests. Deliberately unhurried -- a first run is a
#: one-off, and finishing ten minutes sooner is worth nothing to anyone.
DEFAULT_DELAY = 0.5

#: Give up on a single page rather than hanging the whole run.
TIMEOUT = 15

#: Hosts a film link may point at. An export is a file like any other: it can
#: be edited, and it can come from someone else. Every URI in it is fetched,
#: so without this check a crafted export turns resolution into a request to
#: whatever host it names -- a cloud metadata endpoint, or a service on the
#: user's own network -- and the failure text comes back through
#: `ResolveResult.failures`, which an agent reads. Only Letterboxd can say
#: what a Letterboxd film is, so anything else is refused unfetched.
ALLOWED_HOSTS = frozenset({"letterboxd.com", "boxd.it"})


class ResolutionError(LetterboxdError):
    """Resolution could not be carried out at all."""


@dataclass(frozen=True, slots=True)
class Identity:
    """What a Letterboxd film page tells us about a film."""

    letterboxd_uri: str
    slug: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    error: str | None = None


def is_letterboxd_url(url: str) -> bool:
    """Whether `url` is an http(s) link to Letterboxd, and so safe to fetch.

    The host is read by `urlparse`, not by matching the text, so the usual
    disguises do not work: in `https://letterboxd.com@example.com/`, the host
    is example.com and this returns False.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    # A trailing dot names the same host to DNS but not to a string compare.
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in ("http", "https") and any(
        host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS
    )


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_identity(uri: str, session: requests.Session) -> Identity:
    """Fetch one Letterboxd page and read the ids out of it.

    Kept separate from the loop below so it can be exercised without any
    network, and so the parsing rules live in one readable place.
    """
    if not is_letterboxd_url(uri):
        # Recorded as a failure rather than raised, so one bad row in an
        # export cannot stop the rest of the library resolving.
        return Identity(letterboxd_uri=uri, error="not a Letterboxd link")

    try:
        response = session.get(uri, timeout=TIMEOUT, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as error:
        return Identity(letterboxd_uri=uri, error=f"{type(error).__name__}: {error}")

    final_url = response.url
    if not is_letterboxd_url(final_url):
        # The request was pinned to Letterboxd, so getting here means
        # Letterboxd itself redirected off-site. Do not read ids from
        # whatever answered.
        return Identity(letterboxd_uri=uri, error="redirected off Letterboxd")

    slug = _slug_from_url(final_url)

    soup = BeautifulSoup(response.content, "html.parser")
    tmdb_id = _extract_tmdb_id(soup)
    imdb_id = _extract_imdb_id(soup)

    if tmdb_id is None:
        return Identity(
            letterboxd_uri=uri, slug=slug, imdb_id=imdb_id,
            error="no TMDB id on the page",
        )
    return Identity(letterboxd_uri=uri, slug=slug, tmdb_id=tmdb_id, imdb_id=imdb_id)


def _slug_from_url(url: str) -> str | None:
    """Pull the film slug out of whatever URL we landed on.

    Both shapes appear: `/film/<slug>/` for a film, and `/<user>/film/<slug>/`
    (optionally with a viewing number) for a diary entry. The slug is the
    segment after `film`, either way.
    """
    parts = url.rstrip("/").split("/")
    if "film" in parts:
        index = parts.index("film")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _extract_tmdb_id(soup: BeautifulSoup) -> int | None:
    """Read the TMDB id, trying the attribute before the outbound link."""
    element = soup.find(attrs={"data-tmdb-id": True})
    if element:
        value = str(element["data-tmdb-id"]).strip()
        if value.isdigit():
            return int(value)

    link = soup.find("a", attrs={"data-track-action": "TMDb"})
    if link and link.get("href"):
        tail = str(link["href"]).rstrip("/").split("/")[-1]
        if tail.isdigit():
            return int(tail)
    return None


def _extract_imdb_id(soup: BeautifulSoup) -> str | None:
    link = soup.find("a", attrs={"data-track-action": "IMDb"})
    if not link or not link.get("href"):
        return None
    return next(
        (part for part in str(link["href"]).split("/") if part.startswith("tt")), None
    )


# ------------------------------------------------------------------- cache

def unresolved_uris(limit: int | None = None) -> list[str]:
    """Film URIs with no TMDB id yet, hardest-working query in the module.

    Excludes anything already resolved, and anything whose slug another URI
    has already resolved -- so a re-run never re-fetches settled work.

    Films on your lists count too, even ones you have never watched or put on
    your watchlist: without an id a list entry cannot be compared with
    anything, which is what `list_overlap` and `rank` need.
    """
    rows = db.query(
        f"""
        SELECT uri AS letterboxd_uri FROM (
            SELECT letterboxd_uri AS uri FROM staging_films
            UNION
            SELECT letterboxd_uri FROM list_entries WHERE letterboxd_uri IS NOT NULL
        )
        WHERE uri NOT IN (
            SELECT letterboxd_uri FROM film_identity WHERE tmdb_id IS NOT NULL
        )
        ORDER BY uri
        {"LIMIT " + str(int(limit)) if limit else ""}
        """
    )
    return [row["letterboxd_uri"] for row in rows]


def remember(identity: Identity) -> None:
    """Record a resolution attempt, successful or not."""
    db.get_connection().execute(
        """
        INSERT INTO film_identity
            (letterboxd_uri, letterboxd_slug, tmdb_id, imdb_id, resolved_at, error)
        VALUES (?, ?, ?, ?, now(), ?)
        ON CONFLICT (letterboxd_uri) DO UPDATE SET
            letterboxd_slug = excluded.letterboxd_slug,
            tmdb_id         = excluded.tmdb_id,
            imdb_id         = excluded.imdb_id,
            resolved_at     = excluded.resolved_at,
            error           = excluded.error
        """,
        [
            identity.letterboxd_uri,
            identity.slug,
            identity.tmdb_id,
            identity.imdb_id,
            identity.error,
        ],
    )


def known_slug(slug: str | None) -> Identity | None:
    """A film already resolved under a different URI, if we have one."""
    if not slug:
        return None
    rows = db.query(
        "SELECT tmdb_id, imdb_id FROM film_identity "
        "WHERE letterboxd_slug = ? AND tmdb_id IS NOT NULL LIMIT 1",
        [slug],
    )
    if not rows:
        return None
    return Identity(
        letterboxd_uri="", slug=slug,
        tmdb_id=rows[0]["tmdb_id"], imdb_id=rows[0]["imdb_id"],
    )


# ------------------------------------------------------------------- driver

class ResolveResult(Result):
    """What a resolution run looked up."""

    attempted: int
    """Films looked up in this run."""
    resolved: int
    """Films that now have a TMDB id."""
    from_slug_cache: int
    """Of those, films settled by a lookup already made for another link."""
    failed: int
    """Films whose page gave no TMDB id."""
    still_unresolved: int
    """Films still waiting for a lookup, including failures."""
    failures: list[str]
    """Up to ten failures, each as `link: reason`."""


def resolve_all(
    limit: int | None = None,
    delay: float = DEFAULT_DELAY,
    session: requests.Session | None = None,
) -> ResolveResult:
    """Resolve outstanding film URIs, returning what happened.

    Safe to interrupt and re-run: every result is written as it arrives, so a
    second run resumes rather than restarting.
    """
    pending = unresolved_uris(limit)
    owned_session = session is None
    session = session or _session()

    resolved = failed = from_cache = 0
    failures: list[str] = []

    try:
        tracked = progress.track(pending, "Looking up films on Letterboxd")
        for index, uri in enumerate(tracked):
            if index and delay:
                time.sleep(delay)

            identity = fetch_identity(uri, session)

            # A different URI may already have settled this film.
            if identity.tmdb_id is None and identity.slug:
                cached = known_slug(identity.slug)
                if cached is not None:
                    identity = Identity(
                        letterboxd_uri=uri, slug=identity.slug,
                        tmdb_id=cached.tmdb_id, imdb_id=cached.imdb_id,
                    )
                    from_cache += 1

            remember(identity)
            if identity.tmdb_id is not None:
                resolved += 1
            else:
                failed += 1
                failures.append(f"{uri}: {identity.error}")
    finally:
        if owned_session:
            session.close()

    return ResolveResult(
        attempted=len(pending),
        resolved=resolved,
        from_slug_cache=from_cache,
        failed=failed,
        still_unresolved=len(unresolved_uris()),
        failures=failures[:10],
    )
