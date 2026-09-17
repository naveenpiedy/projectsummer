"""Runtime configuration.

Everything user-specific lives here so that no other module needs to know
where files are on disk. A user's library is a single DuckDB file; one user
per database, but any user can run this on their own machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

APP_NAME = "projectsummer"

#: Environment variable that overrides the database location.
DB_PATH_ENV = "LETTERBOXD_DB"

#: Environment variable holding the Letterboxd username used for RSS polling.
#: Optional -- ingestion falls back to the username in the imported profile.
USERNAME_ENV = "LETTERBOXD_USERNAME"

#: Environment variable holding the TMDB API read-access token.
TMDB_TOKEN_ENV = "TMDB_API_KEY"

#: Environment variable that, when set to a true value, stops the MCP server
#: offering any tool that writes -- whatever each plugin declares.
MCP_READ_ONLY_ENV = "LETTERBOXD_MCP_READ_ONLY"


#: Local settings file. `python-dotenv` finds it by walking up from the
#: working directory, so it works from anywhere inside a project.
ENV_FILE = ".env"

_env_loaded = False


def find_env_file() -> Path | None:
    """Locate the nearest `.env`, searching upwards from the current directory.

    `usecwd=True` matters: without it the search starts from this module's
    own directory, which for an installed package is inside site-packages.
    """
    found = find_dotenv(ENV_FILE, usecwd=True)
    return Path(found) if found else None


def load_env_file(path: Path | None = None, override: bool = False) -> bool:
    """Load a `.env` into the environment.

    The real environment wins unless `override` is set, so an explicitly
    exported variable is never silently replaced by a stale file.
    """
    path = path or find_env_file()
    if path is None:
        return False
    return load_dotenv(path, override=override)


def _ensure_env_loaded() -> None:
    """Load the `.env` once, on first use rather than at import."""
    global _env_loaded
    if not _env_loaded:
        _env_loaded = True
        load_env_file()


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
    _ensure_env_loaded()
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return data_dir() / "letterboxd.duckdb"


def tmdb_token() -> str | None:
    """Return the TMDB API token, or ``None`` if it is not configured."""
    _ensure_env_loaded()
    return os.environ.get(TMDB_TOKEN_ENV) or None


def username() -> str | None:
    """Return the configured Letterboxd username, or ``None``."""
    _ensure_env_loaded()
    return os.environ.get(USERNAME_ENV) or None


def output_dir() -> Path:
    """Where files made over MCP are written, such as built lists.

    An agent's relative path is resolved here, never against the working
    directory: a chat client starts the server wherever it likes.
    """
    return data_dir() / "output"


def mcp_read_only() -> bool:
    """Whether ``LETTERBOXD_MCP_READ_ONLY`` asks for a read-only MCP server."""
    _ensure_env_loaded()
    return os.environ.get(MCP_READ_ONLY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
