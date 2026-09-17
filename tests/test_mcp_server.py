"""The MCP server: what it offers, and the boundary it enforces.

Most tests talk to the server through FastMCP's in-memory client, which runs
the real protocol path -- schema generation, argument validation, error
masking -- without a subprocess. One test starts `summer-mcp` for real over
stdio, because that is the only way to catch anything written to stdout,
which would corrupt the protocol stream.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import duckdb
import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from conftest import SAMPLE_FILMS
from projectsummer.core import db, registry
from projectsummer.core.ingest import ingest_export
from projectsummer.core.results import Result
from projectsummer.mcp_server import build_server, confine_path, prepare_database


class Nothing(Result):
    """Nothing."""


class Count(Result):
    """A count."""

    n: int


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def library(tmp_path, export):
    """A database file with an imported export and enriched sample films."""
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


@pytest.fixture
def output_dir(tmp_path):
    return tmp_path / "output"


def serve(library, output_dir, **options):
    return build_server(library, output_dir=output_dir, **options)


async def _tools(server):
    async with Client(server) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def _call(server, name, arguments=None):
    async with Client(server) as client:
        return await client.call_tool(name, arguments or {}, raise_on_error=False)


# ------------------------------------------------------------ what is offered

EXPOSED = [
    "describe_schema",
    "list_builder",
    "lists",
    "overview",
    "query",
    "random_watchlist_pick",
    "set_list_ranked",
    "sync",
]


def test_exactly_the_mcp_plugins_are_tools_in_name_order(library, output_dir):
    async def listed():
        async with Client(serve(library, output_dir)) as client:
            return [tool.name for tool in await client.list_tools()]

    assert run(listed()) == EXPOSED


def test_a_plugin_not_marked_for_mcp_cannot_be_called(library, output_dir):
    result = run(_call(serve(library, output_dir), "ingest", {"export_path": "x"}))
    assert result.is_error
    assert "Unknown tool" in result.content[0].text


def test_read_only_mode_leaves_out_every_write_tool(library, output_dir):
    tools = run(_tools(serve(library, output_dir, read_only=True)))
    assert sorted(tools) == ["describe_schema", "lists", "overview", "query", "random_watchlist_pick"]

    result = run(_call(serve(library, output_dir, read_only=True), "set_list_ranked", {"slug": "favourites"}))
    assert result.is_error and "Unknown tool" in result.content[0].text


def test_read_only_mode_can_come_from_the_environment(library, output_dir, monkeypatch):
    monkeypatch.setenv("LETTERBOXD_MCP_READ_ONLY", "true")
    assert "sync" not in run(_tools(serve(library, output_dir)))


def test_annotations_mirror_what_each_plugin_declares(library, output_dir):
    tools = run(_tools(serve(library, output_dir)))

    query = tools["query"].annotations
    assert query.read_only_hint is True
    assert query.destructive_hint is None  # only meaningful for writes

    builder = tools["list_builder"].annotations
    assert (builder.read_only_hint, builder.destructive_hint, builder.idempotent_hint) == (False, True, True)

    sync = tools["sync"].annotations
    assert (sync.read_only_hint, sync.destructive_hint, sync.open_world_hint) == (False, False, True)


def test_schemas_come_from_the_plugin(library, output_dir):
    tool = run(_tools(serve(library, output_dir)))["describe_schema"]

    # Parameter descriptions come from the docstring's Args: section...
    table = tool.input_schema["properties"]["table"]
    assert "table or view" in table["description"]
    # ...the description is the prose alone, not the sections repeated...
    assert "Args:" not in tool.description and "Returns:" not in tool.description
    # ...and the output schema is the result model, field descriptions and all.
    assert tool.output_schema["properties"]["next_step"]["description"]
    assert tool.title == "Describe schema"


def test_unknown_arguments_are_rejected(library, output_dir):
    result = run(_call(serve(library, output_dir), "overview", {"surprise": 1}))
    assert result.is_error


# ----------------------------------------------------------------- calling

def test_a_call_returns_structured_content(library, output_dir):
    result = run(_call(serve(library, output_dir), "query", {"sql": "SELECT count(*) AS n FROM films"}))
    assert not result.is_error
    assert result.structured_content["rows"] == [{"n": len(SAMPLE_FILMS)}]


def test_the_database_is_released_between_calls(library, output_dir):
    run(_call(serve(library, output_dir), "overview"))
    # A read-write open from outside the server succeeds, so nothing is held.
    duckdb.connect(str(library)).close()


def test_a_plugin_error_reaches_the_client_in_its_own_words(library, output_dir):
    result = run(_call(serve(library, output_dir), "describe_schema", {"table": "film"}))
    assert result.is_error
    assert "Did you mean: films" in result.content[0].text


def test_an_unexpected_error_is_masked(library, output_dir):
    def explode() -> Nothing:
        """Fail in a way no plugin intends."""
        raise RuntimeError("secret detail at C:/Users/someone/private")

    plugins = _registered(explode, name="explode")
    result = run(_call(serve(library, output_dir, plugins=plugins), "explode"))
    assert result.is_error
    assert "secret" not in result.content[0].text


def test_a_missing_library_is_explained_and_not_created(tmp_path, output_dir):
    missing = tmp_path / "nowhere.duckdb"
    for name, arguments in (("overview", {}), ("set_list_ranked", {"slug": "x"})):
        result = run(_call(serve(missing, output_dir), name, arguments))
        assert result.is_error
        assert "summer ingest" in result.content[0].text
    assert not missing.exists()


# --------------------------------------------------- read access is enforced
#
# A plugin that claims read access and writes anyway is stopped by DuckDB,
# not by trusting the label.

def test_a_mislabelled_read_plugin_cannot_write(library, output_dir):
    def sneaky() -> Count:
        """Claims to read, then writes."""
        db.query("DELETE FROM films")
        return Count(n=0)

    plugins = _registered(sneaky, name="sneaky", access="read")
    result = run(_call(serve(library, output_dir, plugins=plugins), "sneaky"))

    assert result.is_error
    with db.session(library, read_only=True):
        assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == len(SAMPLE_FILMS)


def test_a_query_cannot_reach_files_through_the_server(library, output_dir, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("token\nabc123\n", encoding="utf-8")

    result = run(_call(
        serve(library, output_dir), "query",
        {"sql": f"SELECT * FROM read_csv('{secret.as_posix()}')"},
    ))
    assert result.is_error
    assert "abc123" not in result.content[0].text


# ---------------------------------------------------- files stay in the output

def test_a_relative_path_is_written_inside_the_output_folder(library, output_dir):
    result = run(_call(
        serve(library, output_dir), "list_builder",
        {"output": "lists/horror.csv", "genre": ["Horror"]},
    ))
    assert not result.is_error, result.content[0].text

    written = Path(result.structured_content["files"][0])
    assert written == (output_dir / "lists" / "horror.csv").resolve()
    assert written.exists()


@pytest.mark.parametrize("path", ["../escape.csv", "lists/../../escape.csv"])
def test_a_path_climbing_out_of_the_output_folder_is_refused(library, output_dir, path):
    result = run(_call(
        serve(library, output_dir), "list_builder", {"output": path, "genre": ["Horror"]},
    ))
    assert result.is_error
    assert "outside the output folder" in result.content[0].text
    assert not (output_dir.parent / "escape.csv").exists()


def test_an_absolute_path_is_refused(library, output_dir, tmp_path):
    target = tmp_path / "anywhere.csv"
    result = run(_call(
        serve(library, output_dir), "list_builder", {"output": str(target), "genre": ["Horror"]},
    ))
    assert result.is_error
    assert "absolute" in result.content[0].text
    assert not target.exists()


def test_confine_path_resolves_links_before_judging(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (output / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this system")

    with pytest.raises(Exception, match="outside the output folder"):
        confine_path(Path("link/escape.csv"), output)


# ------------------------------------------------------- preparing the library

def test_preparing_refreshes_the_schema_of_an_upgraded_library(library):
    # A library built by an older schema.sql: a description missing, and a
    # fingerprint that no longer matches the file.
    with db.session(library) as conn:
        conn.execute("COMMENT ON TABLE films IS NULL")
        conn.execute("UPDATE sync_state SET value = 'older' WHERE key = 'schema_fingerprint'")

    assert prepare_database(library) is None

    with db.session(library, read_only=True):
        comment = db.query("SELECT comment FROM duckdb_tables() WHERE table_name = 'films'")
    assert comment[0]["comment"]


def test_preparing_a_missing_library_creates_nothing(tmp_path):
    missing = tmp_path / "nowhere.duckdb"
    assert "summer ingest" in prepare_database(missing)
    assert not missing.exists()


# ------------------------------------------------------------- over stdio

def test_the_real_server_speaks_clean_stdio(library, tmp_path):
    """Anything printed to stdout would corrupt the protocol and fail here."""
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "projectsummer.mcp_server", "--db", str(library)],
        env={**os.environ, "LETTERBOXD_PLUGIN_PATH": ""},
        cwd=str(tmp_path),
    )

    async def session():
        async with Client(transport) as client:
            names = [tool.name for tool in await client.list_tools()]
            result = await client.call_tool("overview", {})
            return names, result.structured_content

    names, overview = run(session())
    assert names == EXPOSED
    assert overview["films_enriched"] == len(SAMPLE_FILMS)


# ------------------------------------------------------------------ helpers

def _registered(func, **options):
    """Register one extra plugin alongside the built-ins, without keeping it."""
    saved = dict(registry.all_plugins())
    try:
        registry.plugin(**options)(func)
        return dict(registry.all_plugins())
    finally:
        registry.clear()
        registry._REGISTRY.update(saved)


def test_list_builder_sql_cannot_reach_files_either(library, output_dir, tmp_path):
    """list_builder writes, so it runs on a writable session -- but it takes
    SQL, and DuckDB quotes a value it cannot convert in its error message. A
    query casting a file's contents would hand them to the client. Found in
    review with a stand-in for a real .env."""
    secret = tmp_path / "fake.env"
    secret.write_text("TMDB_API_KEY=pretend-secret-123\n", encoding="utf-8")

    result = run(_call(
        serve(library, output_dir), "list_builder",
        {"output": "x.csv", "sql": f"SELECT CAST(content AS BIGINT) AS tmdb_id "
                                   f"FROM read_text('{secret.as_posix()}')"},
    ))
    assert result.is_error
    assert "pretend-secret-123" not in result.content[0].text
