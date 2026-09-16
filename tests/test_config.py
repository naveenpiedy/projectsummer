"""Configuration, and reading a local .env.

Parsing is python-dotenv's job; what is tested here is the wiring around it,
because without that wiring a token in .env looks like it should work and
silently does nothing -- the accessors read os.environ, and nothing had
populated it.
"""

from __future__ import annotations

import pytest

from projectsummer import config


@pytest.fixture(autouse=True)
def fresh_env(monkeypatch):
    """Forget any .env already loaded, and keep tests off the real one."""
    monkeypatch.setattr(config, "_env_loaded", False)
    for name in (config.TMDB_TOKEN_ENV, config.USERNAME_ENV, config.DB_PATH_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    monkeypatch.setattr(config, "_env_loaded", False)


# ------------------------------------------------------------------ loading

def test_loads_into_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('TMDB_API_KEY="tok"\nLETTERBOXD_USERNAME=someone\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert config.tmdb_token() == "tok"
    assert config.username() == "someone"


def test_the_real_environment_wins(tmp_path, monkeypatch):
    """An exported variable must not be replaced by a stale file."""
    env = tmp_path / ".env"
    env.write_text("TMDB_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMDB_API_KEY", "from-environment")

    assert config.tmdb_token() == "from-environment"


def test_override_is_available_when_wanted(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TMDB_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TMDB_API_KEY", "from-environment")

    config.load_env_file(env, override=True)
    assert config.tmdb_token() == "from-file"


def test_found_from_a_subdirectory(tmp_path, monkeypatch):
    """Running from anywhere inside the project should still find it."""
    (tmp_path / ".env").write_text("TMDB_API_KEY=tok\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert config.find_env_file() == tmp_path / ".env"
    assert config.tmdb_token() == "tok"


def test_no_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.load_env_file() is False
    assert config.tmdb_token() is None


def test_the_file_is_read_only_once(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TMDB_API_KEY=first\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert config.tmdb_token() == "first"
    env.write_text("TMDB_API_KEY=second\n", encoding="utf-8")
    assert config.tmdb_token() == "first", "should not re-read on every access"


# -------------------------------------------------------------------- paths

def test_database_path_honours_an_override(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DB_PATH_ENV, str(tmp_path / "custom.duckdb"))
    assert config.db_path() == tmp_path / "custom.duckdb"


def test_database_path_defaults_under_the_app_directory(monkeypatch):
    monkeypatch.delenv(config.DB_PATH_ENV, raising=False)
    assert config.db_path().parent.name == config.APP_NAME
