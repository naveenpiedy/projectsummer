"""The CLI is generated, so these tests check the generation, not the feature.

What matters here is that a plugin's signature and docstring survive the trip
into Typer: correct option names, help text, type coercion, and errors that
surface as messages rather than tracebacks.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from letterboxd_utility_tools.cli import build_app

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
    assert "Case-insensitive" in result.output
    assert "at or under this many minutes" in result.output


def test_command_help_omits_the_args_section(app):
    result = runner.invoke(app, ["random-watchlist-pick", "--help"])
    # The prose survives...
    assert "still unwatched" in result.output
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
    assert "not a valid int" in output_of(bad)


# ------------------------------------------------------------------- errors

def test_no_match_exits_nonzero_without_a_traceback(app, conn):
    result = runner.invoke(
        app, ["random-watchlist-pick", "--genre", "documentary", "--max-runtime", "90"]
    )
    assert result.exit_code == 1
    assert "documentary" in output_of(result)
    assert "Traceback" not in output_of(result)


def test_empty_database_reports_the_real_problem(app, empty_conn):
    result = runner.invoke(app, ["random-watchlist-pick"])
    assert result.exit_code == 1
    assert "ingestion" in output_of(result).lower()


def test_bare_invocation_shows_help(app):
    result = runner.invoke(app, [])
    assert "Usage" in result.output
