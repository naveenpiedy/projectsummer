"""Questions a plugin asks the person running it, one keypress at a time.

Some plugins are a conversation rather than a call: `rank` asks which of two
films is better, over and over. A plugin still must not print or read the
terminal -- it has to stay silent over MCP and in tests -- so, as with
`progress`, core code asks and a frontend answers:

    answer = asking.choose("Which is better?", {"1": "Heat", "2": "Collateral"})

With nobody installed to answer, `choose` raises rather than guessing, and
`tell` says nothing. The CLI installs an asker when it runs at a terminal.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Protocol

from projectsummer.core.errors import LetterboxdError


class NoOneToAskError(LetterboxdError):
    """A plugin needs answers, and nobody is there to give them."""


class Asker(Protocol):
    """Puts questions to a person. Implemented by frontends, never by core code."""

    def tell(self, message: str) -> None:
        """Show something that needs no answer."""
        ...

    def choose(self, question: str, options: Mapping[str, str]) -> str:
        """Ask a question, returning the key of the option chosen."""
        ...


_asker: Asker | None = None


@contextmanager
def answering_with(asker: Asker | None) -> Iterator[None]:
    """Install an asker for the duration of the block, restoring the previous one."""
    global _asker
    previous = _asker
    _asker = asker
    try:
        yield
    finally:
        _asker = previous


def can_ask() -> bool:
    """Whether anyone is there to answer."""
    return _asker is not None


def tell(message: str) -> None:
    """Show a message to whoever is answering, if anyone is."""
    if _asker is not None:
        _asker.tell(message)


def require_someone() -> None:
    """Refuse up front when nobody can answer, before any work is done.

    Raises:
        NoOneToAskError: If no asker is installed.
    """
    if _asker is None:
        raise NoOneToAskError(
            "This needs someone to answer questions as it runs. Run it from "
            "`summer` in a terminal."
        )


def choose(question: str, options: Mapping[str, str]) -> str:
    """Ask a question and return the key of the chosen option.

    Args:
        question: What is being decided.
        options: Each option's key -- a single character, what the person
            presses -- and its label.

    Raises:
        NoOneToAskError: If no asker is installed.
    """
    require_someone()
    assert _asker is not None
    answer = _asker.choose(question, options)
    if answer not in options:
        raise ValueError(f"The asker answered {answer!r}, which is not an option.")
    return answer
