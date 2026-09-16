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
