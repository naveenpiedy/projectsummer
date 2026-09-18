"""Resolving Letterboxd URIs to TMDB ids.

No test here touches the network. A fake session returns canned pages, which
also lets the awkward cases -- a redirect to a diary entry, a page with no
TMDB id, a timeout -- be tested deliberately rather than waited for.
"""

from __future__ import annotations

import pytest
import requests

from projectsummer.core import db, resolve
from projectsummer.core.ingest import ingest_export
from projectsummer.core.resolve import Identity, fetch_identity, resolve_all

FILM_PAGE = """
<html><body>
  <div class="film" data-tmdb-id="27205" data-film-name="Inception"></div>
  <a data-track-action="TMDb" href="https://www.themoviedb.org/movie/27205/">TMDb</a>
  <a data-track-action="IMDb" href="http://www.imdb.com/title/tt1375666/maindetails">IMDb</a>
</body></html>
"""

# Some pages carry only the outbound links, with no data-tmdb-id attribute.
LINKS_ONLY_PAGE = """
<html><body>
  <a data-track-action="TMDb" href="https://www.themoviedb.org/movie/694/">TMDb</a>
  <a data-track-action="IMDb" href="http://www.imdb.com/title/tt0081505/">IMDb</a>
</body></html>
"""

NO_IDS_PAGE = "<html><body><h1>Some film</h1></body></html>"


class FakeResponse:
    def __init__(self, url, html, status=200):
        self.url = url
        self.content = html.encode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Stands in for requests.Session, recording what was asked for."""

    def __init__(self, pages):
        self.pages = pages
        self.requested: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, timeout=None, allow_redirects=True):
        self.requested.append(url)
        entry = self.pages[url]
        if isinstance(entry, Exception):
            raise entry
        final_url, html = entry
        return FakeResponse(final_url, html)

    def close(self):
        pass


# ------------------------------------------------------------------ parsing

def test_reads_the_tmdb_id_from_the_attribute():
    session = FakeSession({"https://boxd.it/a": ("https://letterboxd.com/film/inception/", FILM_PAGE)})
    identity = fetch_identity("https://boxd.it/a", session)

    assert identity.tmdb_id == 27205
    assert identity.imdb_id == "tt1375666"
    assert identity.slug == "inception"
    assert identity.error is None


def test_falls_back_to_the_outbound_link():
    session = FakeSession({"https://boxd.it/b": ("https://letterboxd.com/film/the-shining/", LINKS_ONLY_PAGE)})
    identity = fetch_identity("https://boxd.it/b", session)

    assert identity.tmdb_id == 694
    assert identity.imdb_id == "tt0081505"


def test_a_diary_entry_url_still_yields_the_film_slug():
    """Diary links are /<user>/film/<slug>/<n>/ -- the slug is still in there."""
    session = FakeSession(
        {"https://boxd.it/c": ("https://letterboxd.com/someone/film/inception/2/", FILM_PAGE)}
    )
    assert fetch_identity("https://boxd.it/c", session).slug == "inception"


def test_a_page_without_ids_is_an_error_not_a_crash():
    session = FakeSession({"https://boxd.it/d": ("https://letterboxd.com/film/obscure/", NO_IDS_PAGE)})
    identity = fetch_identity("https://boxd.it/d", session)

    assert identity.tmdb_id is None
    assert "no TMDB id" in identity.error
    assert identity.slug == "obscure"  # still worth remembering


def test_a_network_failure_is_recorded_not_raised():
    session = FakeSession({"https://boxd.it/e": requests.Timeout("timed out")})
    identity = fetch_identity("https://boxd.it/e", session)

    assert identity.tmdb_id is None
    assert "Timeout" in identity.error


# ------------------------------------------------------------- host checks

@pytest.mark.parametrize(
    "url",
    [
        "https://boxd.it/film01",
        "https://letterboxd.com/film/inception/",
        "http://letterboxd.com/film/inception/",
        "https://www.letterboxd.com/film/inception/",
        "https://letterboxd.com./film/inception/",  # a trailing dot is the same host
    ],
)
def test_letterboxd_links_are_fetchable(url):
    assert resolve.is_letterboxd_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:8080/admin",
        "https://letterboxd.com@example.com/",  # host is example.com
        "https://evilletterboxd.com/",
        "https://letterboxd.com.example.net/",
        "file:///etc/passwd",
        "gopher://internal:70/",
        "",
        "not a url",
    ],
)
def test_everything_else_is_not(url):
    assert not resolve.is_letterboxd_url(url)


def test_a_uri_pointing_elsewhere_is_never_requested():
    """An export is editable, and every URI in it gets fetched."""
    session = FakeSession({})
    identity = fetch_identity("http://169.254.169.254/latest/meta-data/", session)

    assert identity.tmdb_id is None
    assert identity.error == "not a Letterboxd link"
    assert session.requested == []


def test_a_redirect_off_letterboxd_is_not_read():
    """The request was pinned to Letterboxd; whatever answered is not trusted."""
    session = FakeSession(
        {"https://boxd.it/f": ("https://example.com/anything/", FILM_PAGE)}
    )
    identity = fetch_identity("https://boxd.it/f", session)

    assert identity.tmdb_id is None
    assert identity.error == "redirected off Letterboxd"


def test_the_user_agent_identifies_the_tool():
    """An operator should be able to see what this traffic is."""
    assert "projectsummer" in resolve.USER_AGENT
    assert "http" in resolve.USER_AGENT  # a link to what it is


# ------------------------------------------------------------------ driving

@pytest.fixture
def staged(empty_conn, export):
    ingest_export(export)
    return empty_conn


def _pages_for_all(uris, html=FILM_PAGE):
    return {uri: (f"https://letterboxd.com/film/film-{i}/", html) for i, uri in enumerate(uris)}


def test_resolves_everything_outstanding(staged):
    uris = resolve.unresolved_uris()
    assert len(uris) == 4

    report = resolve_all(delay=0, session=FakeSession(_pages_for_all(uris)))
    assert report.attempted == 4
    assert report.resolved == 4
    assert report.still_unresolved == 0


def test_films_only_on_a_list_are_resolved_too(staged):
    """A list film you have never watched has no other route to an id, and
    without one it cannot be compared with the rest of the library."""
    db.get_connection().execute(
        """
        INSERT INTO list_entries (list_id, entry_position, name, letterboxd_uri)
        SELECT list_id, 98, 'Only Listed', 'https://boxd.it/listonly' FROM lists LIMIT 1
        """
    )
    assert "https://boxd.it/listonly" in resolve.unresolved_uris()


def test_a_second_run_does_no_work(staged):
    uris = resolve.unresolved_uris()
    session = FakeSession(_pages_for_all(uris))
    resolve_all(delay=0, session=session)

    again = FakeSession({})
    report = resolve_all(delay=0, session=again)

    assert report.attempted == 0
    assert again.requested == [], "a resolved film must never be fetched twice"


def test_limit_allows_a_trial_run(staged):
    uris = resolve.unresolved_uris()
    session = FakeSession(_pages_for_all(uris))

    report = resolve_all(limit=2, delay=0, session=session)
    assert report.attempted == 2
    assert report.still_unresolved == 2
    assert len(session.requested) == 2


def test_failures_are_recorded_and_retried_next_time(staged):
    uris = resolve.unresolved_uris()
    pages = _pages_for_all(uris)
    pages[uris[0]] = requests.Timeout("nope")

    report = resolve_all(delay=0, session=FakeSession(pages))
    assert report.resolved == 3
    assert report.failed == 1
    assert report.still_unresolved == 1
    assert any("nope" in f for f in report.failures)

    # The failure is remembered, so it can be reported rather than lost...
    stored = db.query("SELECT error FROM film_identity WHERE tmdb_id IS NULL")
    assert len(stored) == 1
    # ...but it is still outstanding, so a later run tries it again.
    assert resolve.unresolved_uris() == [uris[0]]


def test_a_film_already_known_by_slug_is_not_fetched_again(staged):
    """Several URIs can point at one film; the second must cost nothing."""
    uris = resolve.unresolved_uris()
    pages = {uri: ("https://letterboxd.com/film/same-film/", NO_IDS_PAGE) for uri in uris}
    pages[uris[0]] = ("https://letterboxd.com/film/same-film/", FILM_PAGE)

    report = resolve_all(delay=0, session=FakeSession(pages))

    assert report.resolved == 4
    assert report.from_slug_cache == 3
    ids = {row["tmdb_id"] for row in db.query("SELECT tmdb_id FROM film_identity")}
    assert ids == {27205}


def test_results_survive_an_interruption(staged):
    """Each result is written as it arrives, not batched at the end."""
    uris = resolve.unresolved_uris()
    pages = _pages_for_all(uris)

    class Exploding(FakeSession):
        def get(self, url, **kwargs):
            if len(self.requested) >= 2:
                raise KeyboardInterrupt
            return super().get(url, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        resolve_all(delay=0, session=Exploding(pages))

    assert db.query("SELECT count(*) AS n FROM film_identity")[0]["n"] == 2
    assert len(resolve.unresolved_uris()) == 2


def test_nothing_to_do_is_not_an_error(staged):
    session = FakeSession(_pages_for_all(resolve.unresolved_uris()))
    resolve_all(delay=0, session=session)

    report = resolve_all(delay=0, session=FakeSession({}))
    assert report.model_dump() == {
        "attempted": 0, "resolved": 0, "from_slug_cache": 0,
        "failed": 0, "still_unresolved": 0, "failures": [],
    }


def test_one_bad_uri_does_not_stop_the_rest(staged):
    """Resolution carries on and records the refusal like any other failure."""
    db.get_connection().execute(
        "UPDATE staging_films SET letterboxd_uri = 'http://127.0.0.1:9/x' "
        "WHERE letterboxd_uri = (SELECT min(letterboxd_uri) FROM staging_films)"
    )
    uris = resolve.unresolved_uris()
    session = FakeSession(_pages_for_all(uris))

    report = resolve_all(delay=0, session=session)

    assert report.failed == 1
    assert report.resolved == len(uris) - 1
    assert any("not a Letterboxd link" in failure for failure in report.failures)
    assert "http://127.0.0.1:9/x" not in session.requested
