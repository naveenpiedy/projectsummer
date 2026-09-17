"""Keep the library current from Letterboxd's RSS feed.

Importing an export is a one-off. This is the ongoing path, and it is much
cheaper: Letterboxd's feed publishes `tmdb:movieId` on every film item, so
nothing here has to resolve anything or read a web page. New films still need
their metadata fetched from TMDB, but that is one API call apiece.

Three things about the feed shape drive the code:

* **It mixes three kinds of item.** Guids are prefixed `letterboxd-watch-`,
  `letterboxd-review-` and `letterboxd-list-`. List items are list
  *publications* -- they carry no film fields at all -- so iterating `<item>`
  and reading `tmdb:movieId` yields silent Nones for them.
* **`rewatch` is `Yes`/`No` here**, where the CSV export writes `Yes` or
  nothing. Same fact, two encodings.
* **Only diary activity appears.** Watchlist additions and likes have no
  public feed of their own (`/watchlist/rss/` answers 403), so those still
  come from an export.
"""

from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from projectsummer.core import db, progress
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.results import Result

#: Namespaces Letterboxd declares on its feed.
NAMESPACES = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}

FEED_URL = "https://letterboxd.com/{username}/rss/"

USER_AGENT = (
    "projectsummer/2.0 (+https://github.com/naveenpiedy/projectsummer) "
    "personal Letterboxd library tool"
)

TIMEOUT = 20

#: Letterboxd appends this to the description of every diary item.
_WATCHED_ON = re.compile(r"\s*Watched on \w+ \w+ \d+, \d{4}\.\s*")

#: Where the last successful poll is recorded.
LAST_SYNC_KEY = "last_rss_sync"


class NoUsernameError(LetterboxdError):
    """No Letterboxd username is configured and none could be inferred."""


class FeedUnavailableError(LetterboxdError):
    """The feed could not be fetched."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One film item from the feed."""

    guid: str
    tmdb_id: int
    title: str
    year: int | None
    watched_date: date | None
    logged_date: date | None
    rating: float | None
    rewatch: bool
    liked: bool
    review: str | None
    link: str | None

    @property
    def is_review(self) -> bool:
        return self.guid.startswith("letterboxd-review")


# ------------------------------------------------------------------ parsing

def parse_feed(xml: str) -> list[Entry]:
    """Turn feed XML into film entries, ignoring everything that is not one."""
    root = ElementTree.fromstring(xml)
    entries: list[Entry] = []

    for item in root.findall(".//item"):
        guid = _text(item, "guid") or ""
        tmdb_id = _text(item, "tmdb:movieId")

        # List publications share the feed and have no film fields.
        if not tmdb_id or not guid.startswith(("letterboxd-watch", "letterboxd-review")):
            continue

        entries.append(
            Entry(
                guid=guid,
                tmdb_id=int(tmdb_id),
                title=_text(item, "letterboxd:filmTitle") or _text(item, "title") or "",
                year=_int(_text(item, "letterboxd:filmYear")),
                watched_date=_date(_text(item, "letterboxd:watchedDate")),
                logged_date=_published(_text(item, "pubDate")),
                rating=_float(_text(item, "letterboxd:memberRating")),
                rewatch=_yes(_text(item, "letterboxd:rewatch")),
                liked=_yes(_text(item, "letterboxd:memberLike")),
                review=_review_text(_text(item, "description")),
                link=_text(item, "link"),
            )
        )
    return entries


def _text(item: ElementTree.Element, path: str) -> str | None:
    node = item.find(path, NAMESPACES)
    return node.text if node is not None and node.text else None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _yes(value: str | None) -> bool:
    """The feed writes 'Yes'/'No' where the CSV export writes 'Yes'/nothing."""
    return (value or "").strip().lower() in {"yes", "true", "1"}


def _date(value: str | None) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def _published(value: str | None) -> date | None:
    """`pubDate` is RFC 2822 with an offset; the local calendar date is what
    corresponds to the export's `Date` column."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _review_text(description: str | None) -> str | None:
    """Pull review prose out of the description, dropping Letterboxd's furniture.

    Every diary item's description holds a poster image and a sentence reading
    "Watched on <date>." A review adds the actual text alongside them.
    """
    if not description:
        return None
    text = BeautifulSoup(description, "html.parser").get_text("\n", strip=True)
    text = _WATCHED_ON.sub(" ", text).strip()
    return text or None


# ----------------------------------------------------------------- fetching

def feed_url(username: str) -> str:
    return FEED_URL.format(username=username)


def fetch_feed(username: str) -> str:
    """Fetch the raw feed for `username`."""
    request = urllib.request.Request(
        feed_url(username), headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 -- urllib raises a wide range
        raise FeedUnavailableError(
            f"Could not fetch {feed_url(username)}: {error}. Check the username "
            f"and that the profile is public."
        ) from None


def resolve_username(explicit: str | None = None) -> str:
    """Work out whose feed to poll.

    Prefers what the caller said, then the configured variable, then the
    profile imported from an export -- so an import alone is usually enough.
    """
    from projectsummer import config

    if explicit:
        return explicit
    configured = config.username()
    if configured:
        return configured

    rows = db.query("SELECT username FROM profile LIMIT 1")
    if rows and rows[0]["username"]:
        return rows[0]["username"]

    raise NoUsernameError(
        "No Letterboxd username configured. Set LETTERBOXD_USERNAME, or "
        "import an export, which records the username from your profile."
    )


# ------------------------------------------------------------------ storing

def store_entry(entry: Entry) -> str:
    """Record one feed entry, returning what it did.

    Matching is on (tmdb_id, watched_date) rather than the full natural key.
    The export's `Date` and the feed's `pubDate` are different clocks -- the
    feed carries a timezone offset -- so insisting they agree would create a
    duplicate row for a viewing already imported from an export.

    Returns:
        ``"added"``, ``"updated"`` or ``"unchanged"``.
    """
    conn = db.get_connection()
    existing = db.query(
        "SELECT entry_id, rating, review FROM diary_entries "
        "WHERE tmdb_id = ? AND watched_date IS NOT DISTINCT FROM ?",
        [entry.tmdb_id, entry.watched_date],
    )

    if existing:
        row = existing[0]
        if row["rating"] == entry.rating and (row["review"] or None) == entry.review:
            return "unchanged"
        conn.execute(
            """
            UPDATE diary_entries
            SET rating = ?, rewatch = ?, review = coalesce(?, review)
            WHERE entry_id = ?
            """,
            [entry.rating, entry.rewatch, entry.review if entry.is_review else None,
             row["entry_id"]],
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO diary_entries
            (tmdb_id, watched_date, logged_date, rating, rewatch, review, letterboxd_uri)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [entry.tmdb_id, entry.watched_date, entry.logged_date, entry.rating,
         entry.rewatch, entry.review if entry.is_review else None, entry.link],
    )
    return "added"


def apply_film_state(entry: Entry) -> None:
    """Update the standing facts a diary entry implies about its film."""
    db.get_connection().execute(
        """
        UPDATE films
        SET watched    = TRUE,
            liked      = liked OR ?,
            my_rating  = coalesce(?, my_rating),
            updated_at = now()
        WHERE tmdb_id = ?
        """,
        [entry.liked, entry.rating, entry.tmdb_id],
    )


def known_film_ids(tmdb_ids: list[int]) -> set[int]:
    if not tmdb_ids:
        return set()
    rows = db.query(
        "SELECT tmdb_id FROM films WHERE tmdb_id IN (SELECT unnest(?::BIGINT[]))",
        [tmdb_ids],
    )
    return {row["tmdb_id"] for row in rows}


# ------------------------------------------------------------------- driver

class SyncResult(Result):
    """What the feed held, and what was new."""

    username: str
    """Whose feed was read."""
    feed_items: int
    """Diary entries and reviews in the feed, roughly the last fifty."""
    new_films: int
    """Films never seen before, fetched from TMDB."""
    new_entries: int
    """Diary entries added."""
    updated_entries: int
    """Diary entries already known whose rating, review or tags changed."""
    already_known: int
    """Diary entries already in the library, unchanged."""


def sync_feed(
    username: str | None = None,
    xml: str | None = None,
    client: Any | None = None,
) -> SyncResult:
    """Poll the feed and fold anything new into the library.

    Args:
        username: Whose feed to read. Defaults to the configured or imported one.
        xml: Feed content, if it has already been fetched. Mostly for testing.
        client: A TMDB client for films not seen before.
    """
    from projectsummer import config
    from projectsummer.core.enrich import MissingTokenError, TMDBClient, store_films

    who = resolve_username(username)
    entries = parse_feed(xml if xml is not None else fetch_feed(who))

    # Films in the feed we have never seen need metadata before a diary entry
    # can refer to them.
    unknown = sorted({e.tmdb_id for e in entries} - known_film_ids([e.tmdb_id for e in entries]))
    fetched = 0
    if unknown:
        if client is None:
            token = config.tmdb_token()
            if not token:
                raise MissingTokenError(
                    f"{len(unknown)} new film(s) in the feed need metadata from "
                    f"TMDB, but no API token is configured. Set TMDB_API_KEY."
                )
            client = TMDBClient(token)

        rows = []
        for tmdb_id in progress.track(unknown, "Fetching new films from TMDB"):
            row = client.fetch_movie(tmdb_id)
            if row is not None:
                rows.append(row)
        store_films(rows)
        fetched = len(rows)

    counts = {"added": 0, "updated": 0, "unchanged": 0}
    available = known_film_ids([e.tmdb_id for e in entries])
    for entry in progress.track(entries, "Reading your feed"):
        if entry.tmdb_id not in available:
            continue  # TMDB has no record of it; nothing to attach to
        counts[store_entry(entry)] += 1
        apply_film_state(entry)

    # Deliberately NOT apply_user_state(): that overlays the staging tables,
    # which hold the last export. The feed is newer than any export, so
    # re-applying staging here would undo what we just learned -- unmarking a
    # film watched, or dropping a like made since the export was taken.
    _record_sync(who)

    return SyncResult(
        username=who,
        feed_items=len(entries),
        new_films=fetched,
        new_entries=counts["added"],
        updated_entries=counts["updated"],
        already_known=counts["unchanged"],
    )


def _record_sync(username: str) -> None:
    db.get_connection().execute(
        """
        INSERT INTO sync_state (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
        """,
        [LAST_SYNC_KEY, f"{username}@{datetime.now().isoformat(timespec='seconds')}"],
    )
