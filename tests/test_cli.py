"""The CLI is generated, so these tests check the generation, not the feature.

What matters here is that a plugin's signature and docstring survive the trip
into Typer: correct option names, help text, type coercion, and errors that
surface as messages rather than tracebacks.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from letterboxd_utility_tools.cli import _format, build_app, err_console

# Rich wraps help text to the terminal width, which would make assertions
# depend on the size of whatever terminal the suite happens to run in.
runner = CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def app():
    return build_app()


def output_of(result) -> str:
    """Combined stdout and stderr, across click versions that split them."""
    text = result.output or ""
    try:
        text += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return text


def flat(text: str) -> str:
    """Collapse whitespace so assertions do not depend on the terminal width.

    Rich wraps to the console width, which can split a phrase like
    "not a directory" across two lines. Only the words matter here.
    """
    return " ".join(text.split())


# --------------------------------------------------------------- generation

def test_plugin_becomes_a_kebab_case_command(app):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "random-watchlist-pick" in result.output


def test_commands_are_grouped_by_category(app):
    result = runner.invoke(app, ["--help"])
    assert "Discovery" in result.output


def test_parameters_become_options_not_positionals(app):
    result = runner.invoke(app, ["random-watchlist-pick", "--help"])
    assert "--genre" in result.output
    assert "--max-runtime" in result.output  # underscores become dashes


def test_option_help_comes_from_the_docstring(app):
    result = runner.invoke(app, ["random-watchlist-pick", "--help"])
    assert "Case-insensitive" in flat(result.output)
    assert "at or under this many minutes" in flat(result.output)


def test_command_help_omits_the_args_section(app):
    result = runner.invoke(app, ["random-watchlist-pick", "--help"])
    # The prose survives...
    assert "still unwatched" in flat(result.output)
    # ...but the raw docstring sections do not; Typer renders options itself.
    assert "Raises:" not in result.output


def test_json_flag_is_added_to_every_command(app):
    result = runner.invoke(app, ["random-watchlist-pick", "--help"])
    assert "--json" in result.output


# ------------------------------------------------------------------ running

def test_runs_the_plugin_and_prints_a_result(app, conn):
    result = runner.invoke(app, ["random-watchlist-pick", "--genre", "horror", "--seed", "7"])
    assert result.exit_code == 0
    assert "Title" in result.output


def test_json_output_is_valid_and_keeps_list_columns(app, conn):
    result = runner.invoke(
        app, ["random-watchlist-pick", "--genre", "horror", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload["genres"], list)
    assert "Horror" in payload["genres"]


def test_type_hints_drive_coercion(app, conn):
    result = runner.invoke(app, ["random-watchlist-pick", "--max-runtime", "120"])
    assert result.exit_code == 0

    bad = runner.invoke(app, ["random-watchlist-pick", "--max-runtime", "abc"])
    assert bad.exit_code == 2
    assert "not a valid int" in flat(output_of(bad))


# ------------------------------------------------------------------- errors

def test_no_match_exits_nonzero_without_a_traceback(app, conn):
    result = runner.invoke(
        app, ["random-watchlist-pick", "--genre", "documentary", "--max-runtime", "90"]
    )
    assert result.exit_code == 1
    assert "documentary" in flat(output_of(result))
    assert "Traceback" not in output_of(result)


def test_empty_database_reports_the_real_problem(app, empty_conn):
    result = runner.invoke(app, ["random-watchlist-pick"])
    assert result.exit_code == 1
    assert "import a letterboxd export" in flat(output_of(result)).lower()


def test_bare_invocation_shows_help(app):
    result = runner.invoke(app, [])
    assert "Usage" in result.output


# ------------------------------------- generated commands with required args
#
# Until ingestion existed, every plugin parameter had a default, so the
# argument path through _as_cli_parameter was never exercised.

def test_a_required_parameter_becomes_a_positional_argument(app):
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "Arguments" in result.output
    assert "export_dir" in result.output
    assert "required" in result.output
    assert "--export-dir" not in result.output  # required means positional


def test_a_required_argument_takes_its_help_from_the_docstring(app):
    result = runner.invoke(app, ["ingest", "--help"])
    assert "The unzipped export directory" in flat(result.output)


def test_a_path_annotation_reaches_the_help_as_a_path_type(app):
    result = runner.invoke(app, ["ingest", "--help"])
    assert "<path>" in result.output


def test_a_missing_required_argument_is_an_error(app, empty_conn):
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 2
    assert "export_dir" in output_of(result)


def test_ingest_appears_under_its_own_category(app):
    result = runner.invoke(app, ["--help"])
    assert "Library" in result.output
    assert "ingest" in result.output


def test_ingest_runs_through_the_cli_and_reports_counts(app, empty_conn, export):
    result = runner.invoke(app, ["ingest", str(export)])
    assert result.exit_code == 0
    assert "Films" in result.output
    assert "4" in result.output


def test_ingest_json_output_is_machine_readable(app, empty_conn, export):
    result = runner.invoke(app, ["ingest", str(export), "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["films"] == 4
    assert payload["diary_entries"] == 3
    assert payload["profile"] == "someone"
    assert isinstance(payload["skipped"], list)


def test_a_bad_export_path_is_a_message_not_a_traceback(app, empty_conn, tmp_path):
    result = runner.invoke(app, ["ingest", str(tmp_path / "nowhere")])
    assert result.exit_code == 1
    assert "Traceback" not in output_of(result)
    assert "not a directory" in flat(output_of(result))


def test_path_arguments_are_converted_from_their_type_hint(app, empty_conn, export):
    """The plugin is annotated `export_dir: Path`; Typer must honour that."""
    from letterboxd_utility_tools.core import registry

    parameter = registry.get("ingest").signature.parameters["export_dir"]
    assert parameter.annotation is Path
    assert runner.invoke(app, ["ingest", str(export)]).exit_code == 0


# ------------------------------------------------- broken plugins are visible

def test_a_skipped_plugin_is_reported_to_the_user(tmp_path, monkeypatch):
    """A file that cannot import must be announced, not silently dropped."""
    (tmp_path / "broken.py").write_text("not valid python\n", encoding="utf-8")
    monkeypatch.setenv("LETTERBOXD_PLUGIN_PATH", str(tmp_path))

    with err_console.capture() as captured:
        built = build_app()

    assert "Skipped plugin" in flat(captured.get())
    assert "broken.py" in flat(captured.get())
    # ...and the app is still usable.
    assert runner.invoke(built, ["--help"]).exit_code == 0


def test_a_working_plugin_beside_a_broken_one_still_reaches_the_cli(tmp_path, monkeypatch):
    (tmp_path / "broken.py").write_text("not valid python\n", encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "from letterboxd_utility_tools.core.registry import plugin\n"
        "\n"
        "@plugin(category='contrib')\n"
        "def greet(name: str = 'world') -> str:\n"
        "    '''Say hello.'''\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LETTERBOXD_PLUGIN_PATH", str(tmp_path))

    built = build_app()
    result = runner.invoke(built, ["greet", "--name", "there"])
    assert result.exit_code == 0
    assert "hello there" in flat(result.output)


# -------------------------------------------------------------- cell rendering

def test_none_and_empty_list_both_read_as_absent():
    assert _format(None) == _format([])


def test_lists_render_comma_separated():
    assert _format(["Horror", "Mystery"]) == "Horror, Mystery"


def test_booleans_read_as_words():
    assert _format(True) == "yes"
    assert _format(False) == "no"


def test_dates_render_iso():
    assert _format(date(2024, 1, 10)) == "2024-01-10"


def test_an_unreadable_schema_is_a_message_not_a_traceback(tmp_path):
    """Raised in the --db callback, which the command wrapper never reaches."""
    import duckdb

    from letterboxd_utility_tools.core import db as db_module

    stale = tmp_path / "old.duckdb"
    raw = duckdb.connect(str(stale))
    raw.execute("CREATE TABLE films (tmdb_id BIGINT PRIMARY KEY)")
    raw.close()

    db_module.close_connection()
    try:
        result = runner.invoke(build_app(), ["--db", str(stale), "random-watchlist-pick"])
    finally:
        db_module.close_connection()

    assert result.exit_code == 1
    assert "Traceback" not in output_of(result)
    assert "schema version" in flat(output_of(result))
