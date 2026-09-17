"""Command-line interface, generated from the plugin registry.

No command is written by hand. Every registered plugin becomes a subcommand
whose options, types and help text come from its signature and docstring, so
adding a feature to the registry adds it to the CLI.

What this module contributes on top of the raw function is presentation:
turning a returned value into a table or JSON, mapping domain errors onto exit
codes, and grouping commands by category.
"""

from __future__ import annotations

import inspect
import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from contextlib import contextmanager

from projectsummer.core import db, progress as progress_api, registry
from projectsummer.core.errors import LetterboxdError, NoDatabaseError
from projectsummer.core.registry import Plugin

#: Parameter name used internally for the --json flag. Underscore-prefixed so
#: it cannot collide with a plugin's own parameter.
_JSON_FLAG = "_json_output"

console = Console()
err_console = Console(stderr=True)


# ----------------------------------------------------------------- app

def build_app() -> typer.Typer:
    """Construct the Typer app, one command per registered plugin."""
    app = typer.Typer(
        name="summer",
        help="Query and explore your Letterboxd library.",
        no_args_is_help=True,
    )
    app.callback()(_root)

    for item in registry.discover().values():
        app.command(
            name=item.name.replace("_", "-"),
            help=item.help_text,
            rich_help_panel=item.category.capitalize(),
        )(_build_command(item))

    # The registry records broken plugin files rather than raising, so that one
    # of them cannot stop the tool from starting. Say so, or they vanish.
    for failure in registry.load_failures():
        err_console.print(f"[yellow]Skipped plugin[/yellow] {failure}")

    return app


def _root(
    ctx: typer.Context,
    database: Annotated[
        Path | None,
        typer.Option(
            "--db",
            metavar="PATH",
            help="Database file to use. Defaults to your per-user data directory.",
        ),
    ] = None,
) -> None:
    """Query and explore your Letterboxd library."""
    if database is None:
        return

    if not database.exists() and not _may_create(ctx.invoked_subcommand):
        # A path given explicitly and not found is far more likely to be a
        # typo than an intention. Creating it silently means the next command
        # reports an empty library instead of a wrong path.
        raise _fail(
            NoDatabaseError(
                f"No database at {database}. Import an export to create one:"
                f" summer --db {database} ingest <your-export.zip>"
            )
        ) from None

    try:
        db.get_connection(database)
    except LetterboxdError as exc:
        # Opening the database can fail for expected reasons -- a schema
        # this version cannot read, for one. The command wrapper below
        # never sees those, because they happen before it runs.
        raise _fail(exc) from None


#: Commands in this category may bring a database into existence. Everything
#: else expects one to be there already.
_BOOTSTRAP_CATEGORY = "library"


def _may_create(command_name: str | None) -> bool:
    """Is the command about to run one that legitimately creates a database?"""
    if command_name is None:
        return True  # --help, completion, and other non-commands
    try:
        item = registry.get(command_name.replace("-", "_"))
    except KeyError:
        return True
    return item.category == _BOOTSTRAP_CATEGORY


def _fail(exc: LetterboxdError) -> typer.Exit:
    """Report an expected failure as a message, and exit non-zero."""
    err_console.print(f"[red]{exc}[/red]")
    return typer.Exit(code=1)


# ------------------------------------------------------- command building

def _build_command(item: Plugin):
    """Wrap a plugin in a Typer-compatible command.

    Typer reads `inspect.signature`, so the wrapper advertises a synthesised
    signature: the plugin's own parameters re-annotated as CLI arguments and
    options, plus a `--json` flag. The plugin itself is untouched.
    """
    signature = item.signature
    parameters = [
        _as_cli_parameter(parameter, item.param_help.get(name, ""))
        for name, parameter in signature.parameters.items()
    ]
    parameters.append(
        inspect.Parameter(
            _JSON_FLAG,
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[
                bool,
                typer.Option("--json", help="Emit raw JSON instead of a table."),
            ],
        )
    )

    def command(**kwargs: Any) -> None:
        as_json = kwargs.pop(_JSON_FLAG, False)
        try:
            # JSON output must stay machine-readable, so nothing is drawn then.
            reporter = None if as_json else TerminalProgress()
            with progress_api.reporting_to(reporter):
                result = item.func(**kwargs)
        except LetterboxdError as exc:
            # Expected outcomes, not crashes: a clear message beats a traceback.
            raise _fail(exc) from None
        _render(result, as_json=as_json)

        if item.serves:
            # Whatever this started dies with the process, so hold it open.
            _serve_until_interrupted()

    command.__name__ = item.name
    command.__doc__ = item.help_text
    command.__signature__ = signature.replace(
        parameters=parameters, return_annotation=inspect.Signature.empty
    )
    return command


def _as_cli_parameter(parameter: inspect.Parameter, help_text: str) -> inspect.Parameter:
    """Re-annotate a plugin parameter as a CLI argument or option.

    A parameter with no default is required, so it becomes a positional
    argument. Anything with a default becomes a keyword-only `--option`, which
    keeps callers from depending on parameter order.
    """
    if parameter.default is inspect.Parameter.empty:
        return parameter.replace(
            annotation=Annotated[parameter.annotation, typer.Argument(help=help_text)]
        )
    return parameter.replace(
        kind=inspect.Parameter.KEYWORD_ONLY,
        annotation=Annotated[parameter.annotation, typer.Option(help=help_text)],
    )


class TerminalProgress:
    """Draws a progress bar for work a plugin reports.

    Only installed by this module. A plugin calls `progress.track` and knows
    nothing about bars, so the same function is silent over MCP or in a test.
    """

    @contextmanager
    def task(self, description: str, total: int):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TextColumn("eta"),
            TimeRemainingColumn(),
            console=console,
            # Redirected output should stay clean; a bar redrawing itself into
            # a pipe or a log file is noise rather than information.
            disable=not console.is_terminal,
            transient=True,
        ) as bar:
            task_id = bar.add_task(description, total=total)
            yield lambda: bar.advance(task_id)

    def note(self, message: str) -> None:
        if console.is_terminal:
            console.print(f"[dim]{message}...[/dim]")


# ------------------------------------------------------------- rendering

def _render(result: Any, *, as_json: bool) -> None:
    """Print whatever a plugin returned."""
    if result is None:
        return

    if isinstance(result, BaseModel):
        if as_json:
            # Pydantic's own serialisation, so the CLI's JSON is exactly what
            # the output schema describes.
            console.print_json(result.model_dump_json())
            return
        result = result.model_dump()

    if as_json:
        console.print_json(json.dumps(result, default=_json_fallback))
        return

    if isinstance(result, dict):
        _render_mapping(result)
    elif _is_row_list(result):
        _render_rows(result)
    else:
        console.print(result)


def _render_mapping(result: dict[str, Any]) -> None:
    """Render a result: its plain fields as a record, each list of rows as a table."""
    tables = {key: value for key, value in result.items() if _is_row_list(value)}
    fields = {key: value for key, value in result.items() if key not in tables}

    if fields:
        _render_record(fields)
    for key, rows in tables.items():
        _render_rows(rows, title=_humanise(key) if fields or len(tables) > 1 else None)


def _is_row_list(result: Any) -> bool:
    return (
        isinstance(result, list)
        and bool(result)
        and all(isinstance(row, dict) for row in result)
    )


def _render_record(record: dict[str, Any]) -> None:
    """Render a single result as a field/value table."""
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="bold cyan")
    table.add_column(overflow="fold")
    for key, value in record.items():
        table.add_row(_humanise(key), _format(value))
    console.print(table)


def _render_rows(rows: list[dict[str, Any]], title: str | None = None) -> None:
    """Render a list of results as a table, one row each."""
    table = Table(header_style="bold cyan", title=title)
    for key in rows[0]:
        table.add_column(_humanise(key), overflow="fold")
    for row in rows:
        table.add_row(*(_format(value) for value in row.values()))
    console.print(table)
    console.print(f"[dim]{len(rows)} row{'s' if len(rows) != 1 else ''}[/dim]")


def _humanise(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _format(value: Any) -> str:
    """Format a cell. LIST columns arrive as real Python lists."""
    if value is None:
        return "[dim]-[/dim]"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        # An empty list is absence, and should read like one.
        return ", ".join(str(item) for item in value) if value else "[dim]-[/dim]"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _serve_until_interrupted() -> None:
    """Block until Ctrl+C, keeping a server started by a plugin alive."""
    console.print("[dim]Serving. Press Ctrl+C to stop.[/dim]")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        console.print("[dim]Stopped.[/dim]")


def _json_fallback(value: Any) -> str:
    """Make dates and anything else exotic JSON-serialisable."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def main() -> None:
    """Console-script entry point."""
    try:
        build_app()()
    except LetterboxdError as exc:
        # Backstop for anything raised while the app is being built, before
        # any command or callback could have caught it.
        err_console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
