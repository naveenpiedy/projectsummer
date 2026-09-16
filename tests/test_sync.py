"""RSS polling.

The fixture mirrors the real feed's quirks: three kinds of item sharing one
channel, rewatch written as Yes/No rather than the export's Yes/blank, an
entry URL with a viewing number on it, and review prose buried in a
description alongside a poster and a "Watched on" sentence.
"""

from __future__ import annotations

import pytest

from projectsummer.core import db, sync
from projectsummer.core.enrich import enrich_all
from projectsummer.core.ingest import ingest_export
from projectsummer.core.resolve import Identity, remember
from projectsummer.core.sync import (
    FeedUnavailableError,
    NoUsernameError,
    parse_feed,
    resolve_username,
    sync_feed,
)

from export_fixture import ALIEN, GODFATHER, SHINING, UNSEEN
from test_enrich import FakeClient

SHINING_TMDB, ALIEN_TMDB, GODFATHER_TMDB, SOLARIS_TMDB = 694, 348, 238, 393
NEW_FILM_TMDB = 1234


def feed(items: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:letterboxd="https://letterboxd.com" '
        'xmlns:tmdb="https://themoviedb.org" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        "<title>Letterboxd - Someone</title>" + items + "</channel></rss>"
    )


def watch_item(tmdb_id, title, watched="2026-09-14", rating="3.5", rewatch="No",
               like="No", guid=None, published="Tue, 15 Sep 2026 12:58:33 +1200"):
    return f"""
    <item>
      <title>{title}, 1999 - ★★★½</title>
      <link>https://letterboxd.com/someone/film/{title.lower()}/</link>
      <guid isPermaLink="false">{guid or f"letterboxd-watch-{tmdb_id}"}</guid>
      <pubDate>{published}</pubDate>
      <letterboxd:watchedDate>{watched}</letterboxd:watchedDate>
      <letterboxd:rewatch>{rewatch}</letterboxd:rewatch>
      <letterboxd:filmTitle>{title}</letterboxd:filmTitle>
      <letterboxd:filmYear>1999</letterboxd:filmYear>
      <letterboxd:memberRating>{rating}</letterboxd:memberRating>
      <letterboxd:memberLike>{like}</letterboxd:memberLike>
      <tmdb:movieId>{tmdb_id}</tmdb:movieId>
      <description><![CDATA[ <p><img src="poster.jpg"/></p>
      <p>Watched on Monday September 14, 2026.</p> ]]></description>
      <dc:creator>Someone</dc:creator>
    </item>"""


def review_item(tmdb_id, title, text, watched="2026-09-10"):
    return f"""
    <item>
      <title>{title}, 1999 - ★★★</title>
      <link>https://letterboxd.com/someone/film/{title.lower()}/2/</link>
      <guid isPermaLink="false">letterboxd-review-{tmdb_id}</guid>
      <pubDate>Fri, 11 Sep 2026 13:23:04 +1200</pubDate>
      <letterboxd:watchedDate>{watched}</letterboxd:watchedDate>
      <letterboxd:rewatch>Yes</letterboxd:rewatch>
      <letterboxd:filmTitle>{title}</letterboxd:filmTitle>
      <letterboxd:filmYear>1999</letterboxd:filmYear>
      <letterboxd:memberRating>3.0</letterboxd:memberRating>
      <letterboxd:memberLike>Yes</letterboxd:memberLike>
      <tmdb:movieId>{tmdb_id}</tmdb:movieId>
      <description><![CDATA[ <p><img src="poster.jpg"/></p>
      <p>{text}</p> <p>Watched on Thursday September 10, 2026.</p> ]]></description>
    </item>"""


LIST_ITEM = """
    <item>
      <title>Kamal Hassan Comedies</title>
      <link>https://letterboxd.com/someone/list/kamal-hassan-comedies/</link>
      <guid isPermaLink="false">letterboxd-list-87273387</guid>
      <pubDate>Thu, 3 Sep 2026 15:08:31 +1200</pubDate>
      <description>&lt;p&gt;A list of films.&lt;/p&gt;</description>
    </item>"""


# ------------------------------------------------------------------ parsing

def test_film_items_are_parsed():
    entries = parse_feed(feed(watch_item(SHINING_TMDB, "Shining")))
    assert len(entries) == 1

    entry = entries[0]
    assert entry.tmdb_id == SHINING_TMDB
    assert entry.title == "Shining"
    assert entry.year == 1999
    assert str(entry.watched_date) == "2026-09-14"
    assert entry.rating == 3.5


def test_list_publications_are_ignored():
    """They share the feed and carry no film fields at all."""
    entries = parse_feed(feed(LIST_ITEM + watch_item(SHINING_TMDB, "Shining")))
    assert len(entries) == 1
    assert entries[0].tmdb_id == SHINING_TMDB


def test_a_feed_of_only_lists_yields_nothing():
    assert parse_feed(feed(LIST_ITEM)) == []


def test_rewatch_is_yes_or_no_here_not_yes_or_blank():
    """The export writes Yes or nothing; the feed writes Yes or No."""
    assert parse_feed(feed(watch_item(1, "A", rewatch="No")))[0].rewatch is False
    assert parse_feed(feed(watch_item(1, "A", rewatch="Yes")))[0].rewatch is True


def test_likes_are_parsed():
    assert parse_feed(feed(watch_item(1, "A", like="Yes")))[0].liked is True
    assert parse_feed(feed(watch_item(1, "A", like="No")))[0].liked is False


def test_the_logged_date_comes_from_pubdate():
    entries = parse_feed(feed(watch_item(1, "A", published="Tue, 15 Sep 2026 12:58:33 +1200")))
    assert str(entries[0].logged_date) == "2026-09-15"


def test_review_text_is_extracted_without_the_furniture():
    """Letterboxd appends a poster and a 'Watched on' sentence to every item."""
    entries = parse_feed(feed(review_item(1, "A", "Went downhill in the second half.")))
    assert entries[0].review == "Went downhill in the second half."
    assert entries[0].is_review is True


def test_a_plain_watch_has_no_review_text():
    assert parse_feed(feed(watch_item(1, "A")))[0].review is None


def test_an_unrated_entry_is_none_not_zero():
    assert parse_feed(feed(watch_item(1, "A", rating="")))[0].rating is None


def test_malformed_values_do_not_break_the_feed():
    item = watch_item(1, "A", watched="not-a-date", rating="rubbish")
    entry = parse_feed(feed(item))[0]
    assert entry.watched_date is None
    assert entry.rating is None


# ----------------------------------------------------------------- username

def test_username_prefers_what_the_caller_said(empty_conn):
    assert resolve_username("explicit") == "explicit"


def test_username_falls_back_to_the_imported_profile(empty_conn, export, monkeypatch):
    monkeypatch.delenv("LETTERBOXD_USERNAME", raising=False)
    monkeypatch.setattr("projectsummer.config.username", lambda: None)
    ingest_export(export)
    assert resolve_username() == "someone"


def test_no_username_anywhere_says_what_to_do(empty_conn, monkeypatch):
    monkeypatch.setattr("projectsummer.config.username", lambda: None)
    with pytest.raises(NoUsernameError, match="LETTERBOXD_USERNAME"):
        resolve_username()


def test_a_bad_username_is_a_message_not_a_traceback(empty_conn):
    with pytest.raises(FeedUnavailableError, match="Check the username"):
        sync.fetch_feed("a-user-that-does-not-exist-------")


# ------------------------------------------------------------------ syncing

@pytest.fixture
def library(empty_conn, export):
    """A fully built library: imported, resolved, enriched."""
    ingest_export(export)
    for uri, tmdb_id in [
        (SHINING, SHINING_TMDB), (ALIEN, ALIEN_TMDB),
        (GODFATHER, GODFATHER_TMDB), (UNSEEN, SOLARIS_TMDB),
    ]:
        remember(Identity(letterboxd_uri=uri, slug=f"s{tmdb_id}", tmdb_id=tmdb_id))
    enrich_all(client=FakeClient())
    return empty_conn


def test_a_new_viewing_becomes_a_diary_entry(library):
    before = db.query("SELECT count(*) AS n FROM diary_entries")[0]["n"]

    report = sync_feed(username="someone", xml=feed(watch_item(ALIEN_TMDB, "Alien")),
                       client=FakeClient())

    assert report["new_entries"] == 1
    assert db.query("SELECT count(*) AS n FROM diary_entries")[0]["n"] == before + 1


def test_a_viewing_already_imported_is_not_duplicated(library):
    """The export's Date and the feed's pubDate are different clocks, so
    matching on the full natural key would double-count."""
    # The Shining has two viewings at different ratings, so order explicitly
    # and echo back that entry's own rating -- otherwise "unchanged" depends
    # on which row the database happened to return.
    existing = db.query(
        "SELECT tmdb_id, watched_date, rating FROM diary_entries "
        "WHERE tmdb_id = ? ORDER BY watched_date LIMIT 1",
        [SHINING_TMDB],
    )[0]
    item = watch_item(SHINING_TMDB, "Shining",
                      watched=str(existing["watched_date"]),
                      rating=str(existing["rating"]))

    report = sync_feed(username="someone", xml=feed(item), client=FakeClient())

    assert report["new_entries"] == 0
    assert report["already_known"] == 1


def test_a_changed_rating_updates_the_existing_entry(library):
    existing = db.query(
        "SELECT tmdb_id, watched_date FROM diary_entries "
        "WHERE tmdb_id = ? ORDER BY watched_date LIMIT 1",
        [SHINING_TMDB],
    )[0]
    item = watch_item(SHINING_TMDB, "Shining",
                      watched=str(existing["watched_date"]), rating="1.0")

    report = sync_feed(username="someone", xml=feed(item), client=FakeClient())

    assert report["updated_entries"] == 1
    assert db.query(
        "SELECT rating FROM diary_entries WHERE tmdb_id = ? AND watched_date = ?",
        [SHINING_TMDB, existing["watched_date"]],
    )[0]["rating"] == 1.0


def test_a_film_never_seen_before_is_fetched_and_stored(library):
    client = FakeClient()
    report = sync_feed(username="someone",
                       xml=feed(watch_item(NEW_FILM_TMDB, "Newcomer")), client=client)

    assert report["new_films"] == 1
    assert NEW_FILM_TMDB in client.requested
    assert db.query("SELECT count(*) AS n FROM films WHERE tmdb_id = ?",
                    [NEW_FILM_TMDB])[0]["n"] == 1


def test_a_film_already_known_is_not_refetched(library):
    client = FakeClient()
    sync_feed(username="someone", xml=feed(watch_item(ALIEN_TMDB, "Alien")), client=client)
    assert client.requested == []


def test_a_like_in_the_feed_reaches_the_film(library):
    sync_feed(username="someone",
              xml=feed(watch_item(GODFATHER_TMDB, "Godfather", like="Yes")),
              client=FakeClient())
    assert db.query("SELECT liked FROM films WHERE tmdb_id = ?",
                    [GODFATHER_TMDB])[0]["liked"] is True


def test_watching_a_watchlisted_film_marks_it_watched(library):
    assert db.query("SELECT watched FROM films WHERE tmdb_id = ?",
                    [SOLARIS_TMDB])[0]["watched"] is False

    sync_feed(username="someone",
              xml=feed(watch_item(SOLARIS_TMDB, "Solaris", watched="2026-09-20")),
              client=FakeClient())

    assert db.query("SELECT watched FROM films WHERE tmdb_id = ?",
                    [SOLARIS_TMDB])[0]["watched"] is True


def test_a_review_stores_its_text(library):
    sync_feed(username="someone",
              xml=feed(review_item(ALIEN_TMDB, "Alien", "Holds up.")),
              client=FakeClient())

    # Alien already has a review imported from the export, so target the
    # viewing the feed just added rather than whichever row comes first.
    assert db.query(
        "SELECT review FROM diary_entries WHERE tmdb_id = ? AND watched_date = ?",
        [ALIEN_TMDB, "2026-09-10"],
    )[0]["review"] == "Holds up."


def test_syncing_twice_changes_nothing_the_second_time(library):
    xml = feed(watch_item(ALIEN_TMDB, "Alien", watched="2026-09-30"))
    first = sync_feed(username="someone", xml=xml, client=FakeClient())
    second = sync_feed(username="someone", xml=xml, client=FakeClient())

    assert first["new_entries"] == 1
    assert second["new_entries"] == 0
    assert second["already_known"] == 1


def test_the_sync_is_recorded(library):
    sync_feed(username="someone", xml=feed(watch_item(ALIEN_TMDB, "Alien")),
              client=FakeClient())
    recorded = db.query("SELECT value FROM sync_state WHERE key = ?",
                        [sync.LAST_SYNC_KEY])
    assert recorded and recorded[0]["value"].startswith("someone@")


def test_watch_stats_stay_consistent_after_a_sync(library):
    """film_watch_stats is derived, so a new entry must simply show up in it."""
    sync_feed(username="someone",
              xml=feed(watch_item(ALIEN_TMDB, "Alien", watched="2026-09-30")),
              client=FakeClient())

    stats = db.query("SELECT * FROM film_watch_stats WHERE tmdb_id = ?", [ALIEN_TMDB])[0]
    assert stats["watch_count"] == 2
    assert str(stats["last_watched_date"]) == "2026-09-30"


def test_an_empty_feed_is_not_an_error(library):
    report = sync_feed(username="someone", xml=feed(""), client=FakeClient())
    assert report["feed_items"] == 0
    assert report["new_entries"] == 0


def test_the_integrity_view_stays_empty_after_syncing(library):
    sync_feed(username="someone",
              xml=feed(watch_item(NEW_FILM_TMDB, "Newcomer")), client=FakeClient())
    assert db.query("SELECT * FROM integrity_orphans") == []
