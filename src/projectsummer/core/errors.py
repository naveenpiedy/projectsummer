"""Exceptions shared by plugins and translated by each frontend.

Plugins raise these instead of returning sentinel values, so that a single
function body reads naturally from Python, prints a clean message in the CLI,
and becomes a proper tool error over MCP.
"""

from __future__ import annotations


class LetterboxdError(Exception):
    """Base class for every error this package raises deliberately."""


class NoResultError(LetterboxdError):
    """A query ran fine but nothing matched the given filters."""


class EmptyDatabaseError(LetterboxdError):
    """The database has no films yet -- ingestion has not been run."""


class NoDatabaseError(LetterboxdError):
    """An explicitly named database file does not exist."""


class DatabaseBusyError(LetterboxdError):
    """Another process holds the database file's lock."""


class DatabaseRecoveryError(LetterboxdError):
    """The database's write-ahead log cannot be replayed, so it will not open."""


class MissingArgumentError(LetterboxdError):
    """An optional argument turns out to be needed, given the others.

    `trends(by="month")` needs a year, but `year` cannot simply be required:
    by year it means something else. The message is written for an agent,
    which passes the argument and calls again. The CLI asks the person at the
    terminal instead, using `question`, and runs the command again with the
    answer.
    """

    def __init__(self, message: str, *, argument: str, question: str):
        super().__init__(message)
        self.argument = argument
        self.question = question
