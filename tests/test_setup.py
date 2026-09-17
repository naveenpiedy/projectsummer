"""First-time setup: ingest, resolve and enrich as one command.

The three steps have their own tests; these check only that setup runs them in
order, and refuses before anything starts when enrich could never succeed.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from projectsummer.cli import build_app
from projectsummer.core import db
from projectsummer.core.enrich import EnrichResult, MissingTokenError
from projectsummer.core.plugins import library
from projectsummer.core.resolve import ResolveResult

runner = CliRunner()

RESOLVED = ResolveResult(
    attempted=4, resolved=4, from_slug_cache=0, failed=0, still_unresolved=0, failures=[]
)
ENRICHED = EnrichResult.model_validate(
    {name: 0 for name in EnrichResult.model_fields} | {"attempted": 4}
)


@pytest.fixture
def steps(monkeypatch):
    """Stand in for the network steps, recording the order they ran in."""
    ran: list[str] = []

    def resolve_all():
        # Resolution must see what ingestion imported.
        ran.append(f"resolve after {db.query('SELECT count(*) AS n FROM staging_films')[0]['n']} films")
        return RESOLVED

    def enrich_all():
        ran.append("enrich")
        return ENRICHED

    monkeypatch.setattr(library.config, "tmdb_token", lambda: "token")
    monkeypatch.setattr(library, "resolve_all", resolve_all)
    monkeypatch.setattr(library, "enrich_all", enrich_all)
    return ran


def test_setup_runs_each_step_in_turn(empty_conn, export, steps):
    result = library.setup(export)

    assert steps == ["resolve after 4 films", "enrich"]
    assert result.ingest.films == 4
    assert result.resolve is RESOLVED
    assert result.enrich is ENRICHED


def test_a_missing_token_stops_setup_before_anything_is_imported(
    empty_conn, export, steps, monkeypatch
):
    monkeypatch.setattr(library.config, "tmdb_token", lambda: None)

    with pytest.raises(MissingTokenError, match="TMDB_API_KEY"):
        library.setup(export)

    assert steps == []
    assert db.query("SELECT count(*) AS n FROM staging_films")[0]["n"] == 0


def test_setup_may_create_a_library_from_the_cli(tmp_path, export, steps):
    fresh = tmp_path / "new.duckdb"
    db.close_connection()
    try:
        result = runner.invoke(build_app(), ["--db", str(fresh), "setup", str(export), "--json"])
    finally:
        db.close_connection()

    assert result.exit_code == 0, result.output
    assert fresh.exists()
    assert json.loads(result.output)["ingest"]["films"] == 4

