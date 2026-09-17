"""Progress reporting.

The point of the seam is that core code says what it is doing without
deciding how that looks -- so the same function is silent in a test, silent
over MCP, and draws a bar in the terminal.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from projectsummer.core import progress


class RecordingReporter:
    """Captures what was reported instead of drawing anything."""

    def __init__(self):
        self.tasks: list[tuple[str, int]] = []
        self.advances = 0
        self.notes: list[str] = []
        self.finished = 0

    @contextmanager
    def task(self, description, total):
        self.tasks.append((description, total))
        try:
            yield self._advance
        finally:
            self.finished += 1

    def _advance(self):
        self.advances += 1

    def note(self, message):
        self.notes.append(message)


@pytest.fixture(autouse=True)
def no_leaked_reporter():
    """A reporter must never outlive the block that installed it."""
    yield
    assert progress.current_reporter() is None


# ------------------------------------------------------------------ silence

def test_without_a_reporter_track_is_just_iteration():
    assert list(progress.track([1, 2, 3], "counting")) == [1, 2, 3]


def test_without_a_reporter_a_note_is_a_no_op():
    progress.note("something")  # must not raise


def test_a_generator_is_not_consumed_when_nobody_is_listening():
    """No reporter means no reason to materialise a lazy sequence."""
    consumed = []

    def source():
        for i in range(3):
            consumed.append(i)
            yield i

    iterator = progress.track(source(), "counting")
    next(iterator)
    assert consumed == [0], "should still be lazy"


# ----------------------------------------------------------------- reporting

def test_track_reports_a_task_and_advances_once_per_item():
    reporter = RecordingReporter()
    with progress.reporting_to(reporter):
        assert list(progress.track("abc", "lettering")) == ["a", "b", "c"]

    assert reporter.tasks == [("lettering", 3)]
    assert reporter.advances == 3
    assert reporter.finished == 1


def test_an_explicit_total_avoids_materialising_the_items():
    reporter = RecordingReporter()
    with progress.reporting_to(reporter):
        list(progress.track(iter([1, 2]), "counting", total=99))
    assert reporter.tasks == [("counting", 99)]


def test_notes_reach_the_reporter():
    reporter = RecordingReporter()
    with progress.reporting_to(reporter):
        progress.note("rebuilding")
    assert reporter.notes == ["rebuilding"]


def test_a_reporter_without_note_is_tolerated():
    """Implementing `task` is enough; `note` is optional."""

    class Minimal:
        @contextmanager
        def task(self, description, total):
            yield lambda: None

    with progress.reporting_to(Minimal()):
        progress.note("ignored")  # must not raise


def test_the_task_is_closed_even_if_iteration_stops_early():
    reporter = RecordingReporter()
    with progress.reporting_to(reporter):
        for item in progress.track([1, 2, 3], "counting"):
            if item == 2:
                break
    assert reporter.finished == 1


def test_the_previous_reporter_is_restored():
    outer, inner = RecordingReporter(), RecordingReporter()
    with progress.reporting_to(outer):
        with progress.reporting_to(inner):
            assert progress.current_reporter() is inner
        assert progress.current_reporter() is outer


def test_a_reporter_is_removed_even_after_a_failure():
    with pytest.raises(ValueError):
        with progress.reporting_to(RecordingReporter()):
            raise ValueError("boom")
    assert progress.current_reporter() is None


# --------------------------------------------------- the long commands report

def test_resolve_reports_progress(empty_conn, export):
    from projectsummer.core.ingest import ingest_export
    from projectsummer.core.resolve import resolve_all

    from test_resolve import FILM_PAGE, FakeSession

    ingest_export(export)
    from projectsummer.core import resolve as resolve_module

    uris = resolve_module.unresolved_uris()
    pages = {u: (f"https://letterboxd.com/film/f{i}/", FILM_PAGE) for i, u in enumerate(uris)}

    reporter = RecordingReporter()
    with progress.reporting_to(reporter):
        resolve_all(delay=0, session=FakeSession(pages))

    assert reporter.tasks == [("Looking up films on Letterboxd", 4)]
    assert reporter.advances == 4


def test_enrich_reports_progress_and_its_final_steps(empty_conn, export):
    from projectsummer.core.enrich import enrich_all
    from projectsummer.core.ingest import ingest_export
    from projectsummer.core.resolve import Identity, remember

    from export_fixture import ALIEN, GODFATHER, SHINING, UNSEEN
    from test_enrich import FakeClient

    ingest_export(export)
    for uri, tmdb_id in [(SHINING, 694), (ALIEN, 348), (GODFATHER, 238), (UNSEEN, 393)]:
        remember(Identity(letterboxd_uri=uri, slug=f"s{tmdb_id}", tmdb_id=tmdb_id))

    reporter = RecordingReporter()
    with progress.reporting_to(reporter):
        enrich_all(client=FakeClient())

    # Films, then the 18 people credited on them -- the longest step, so it
    # needs a bar of its own.
    assert reporter.tasks == [("Fetching metadata from TMDB", 4), ("Fetching people from TMDB", 18)]
    assert reporter.advances == 4 + 18
    # The steps after fetching take a while on a big library and would
    # otherwise look like a hang.
    assert reporter.notes == ["Applying your viewing data", "Rebuilding diary entries"]
