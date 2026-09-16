"""Progress reporting a frontend can render and a library can ignore.

Long operations need to say what they are doing -- `resolve` runs for twenty
minutes -- but a plugin must not print. It has to work unchanged when called
over MCP, from a script, or from a test, none of which want a progress bar
written to their stdout.

So core code announces progress and says nothing about how it looks:

    for uri in progress.track(pending, "Resolving films"):
        ...

By default that is a plain iteration with no output at all. A frontend
installs a reporter for the duration of a call, and only then does anything
appear. The CLI does this for every command; a fast one simply never calls
`track`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class Reporter(Protocol):
    """Renders progress. Implemented by frontends, never by core code."""

    @contextmanager
    def task(self, description: str, total: int) -> Iterator[Callable[[], None]]:
        """Begin a task, yielding a function to call once per item done."""
        ...


_reporter: Reporter | None = None


@contextmanager
def reporting_to(reporter: Reporter | None) -> Iterator[None]:
    """Install a reporter for the duration of the block.

    Restores whatever was there before, so nested calls and tests cannot
    leak a reporter into unrelated work.
    """
    global _reporter
    previous = _reporter
    _reporter = reporter
    try:
        yield
    finally:
        _reporter = previous


def current_reporter() -> Reporter | None:
    return _reporter


def track(items: Iterable[T], description: str, total: int | None = None) -> Iterator[T]:
    """Iterate `items`, reporting progress if anyone is listening.

    With no reporter installed this is exactly `iter(items)` -- no output, no
    measurable cost, nothing to disable.
    """
    if _reporter is None:
        yield from items
        return

    if total is None:
        items = list(items)
        total = len(items)

    with _reporter.task(description, total) as advance:
        for item in items:
            yield item
            advance()


def note(message: str) -> None:
    """Report a one-off step that is not part of a counted loop."""
    reporter = _reporter
    if reporter is None:
        return
    printer: Any = getattr(reporter, "note", None)
    if callable(printer):
        printer(message)
