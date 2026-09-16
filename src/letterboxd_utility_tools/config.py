"""Runtime configuration.

Everything user-specific lives here so that no other module needs to know
where files are on disk. A user's library is a single DuckDB file; one user
per database, but any user can run this on their own machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "letterboxd-utility-tools"

#: Environment variable that overrides the database location.
DB_PATH_ENV = "LETTERBOXD_DB"

#: Environment variable holding the Letterboxd username used for RSS polling.
#: Optional -- ingestion falls back to the username in the imported profile.
USERNAME_ENV = "LETTERBOXD_USERNAME"

#: Environment variable holding the TMDB API read-access token.
TMDB_TOKEN_ENV = "TMDB_API_KEY"


def data_dir() -> Path:
    """Return the per-user directory where this app keeps its files.

    Follows each platform's convention rather than writing into the current
    working directory, so the tool behaves the same however it was installed.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


def db_path() -> Path:
    """Resolve the DuckDB file path, honouring ``LETTERBOXD_DB``."""
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return data_dir() / "letterboxd.duckdb"


def tmdb_token() -> str | None:
    """Return the TMDB API token, or ``None`` if it is not configured."""
    return os.environ.get(TMDB_TOKEN_ENV) or None


def username() -> str | None:
    """Return the configured Letterboxd username, or ``None``."""
    return os.environ.get(USERNAME_ENV) or None
