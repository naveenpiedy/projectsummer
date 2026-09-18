"""Plugin results as a contract: described, strict, and honest about access."""

from __future__ import annotations

import duckdb
import pytest
from pydantic import ValidationError

from conftest import SAMPLE_FILMS
from projectsummer.core import db, registry
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.ingest import ingest_export
from projectsummer.core.results import Result


def _builtins() -> list[registry.Plugin]:
    return sorted(registry.discover().values(), key=lambda item: item.name)


def _undescribed(schema: dict) -> list[str]:
    """Properties with no description, in the model and every nested model.

    Pydantic puts nested models under `$defs`, so those are checked as well.
    """
    models = {schema.get("title", "result"): schema, **schema.get("$defs", {})}
    return [
        f"{model_name}.{field_name}"
        for model_name, model in models.items()
        for field_name, field_schema in model.get("properties", {}).items()
        if "description" not in field_schema
    ]


@pytest.mark.parametrize("item", _builtins(), ids=lambda item: item.name)
def test_every_builtin_result_field_is_described(item):
    """An agent reads these descriptions to know what a field means."""
    schema = item.result_type.model_json_schema()
    assert "description" in schema, f"{item.result_type.__name__} has no docstring"
    assert _undescribed(schema) == []


def test_a_result_refuses_keys_it_does_not_declare():
    class Small(Result):
        """Small."""

        count: int
        """How many."""

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Small(count=1, surprise=True)


# ------------------------------------------------ read plugins really only read
#
# The MCP server will run every `access="read"` plugin on a read-only session,
# where DuckDB refuses any write and any file access. A plugin mislabelled as
# read would fail there -- so run each one there now, in the tests, instead.

@pytest.fixture
def library(tmp_path, export):
    db.close_connection()
    path = tmp_path / "library.duckdb"
    with db.session(path) as conn:
        ingest_export(export)
        conn.executemany(
            """
            INSERT INTO films
                (tmdb_id, title, year, runtime, genres, directors, on_watchlist, watched)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            SAMPLE_FILMS,
        )
    yield path
    db.close_connection()


_READ_PLUGINS = [
    item
    for item in _builtins()
    if item.access == "read"
]


def test_there_are_read_plugins_to_check():
    assert {item.name for item in _READ_PLUGINS} >= {"lists", "overview", "random_watchlist_pick"}


#: Arguments for read plugins that cannot be called bare. Without these the
#: plugin would go unchecked, which is how a mislabelled one would slip out.
SAMPLE_ARGUMENTS = {
    "query": {"sql": "SELECT title FROM films"},
    "list_overlap": {"first": "watched", "second": "watchlist"},
}


@pytest.mark.parametrize("item", _READ_PLUGINS, ids=lambda item: item.name)
def test_a_read_plugin_runs_on_a_read_only_connection(item, library):
    required = [
        name
        for name, parameter in item.signature.parameters.items()
        if parameter.default is parameter.empty
    ]
    arguments = SAMPLE_ARGUMENTS.get(item.name, {})
    missing = [name for name in required if name not in arguments]
    assert not missing, (
        f"{item.name} needs {', '.join(missing)}; add them to SAMPLE_ARGUMENTS so "
        f"it is checked here rather than skipped."
    )

    with db.session(library, read_only=True):
        try:
            result = item.func(**arguments)
        except LetterboxdError:
            return  # an expected, domain-level outcome; not a write attempt
        except duckdb.Error as error:
            pytest.fail(f"{item.name} is marked read but did something a read-only connection refuses: {error}")

    assert isinstance(result, item.result_type)
