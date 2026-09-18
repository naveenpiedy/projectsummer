"""MCP server, generated from the plugin registry.

No tool is written by hand. Every plugin the registry marks for MCP becomes a
tool whose input schema comes from its signature, whose output schema is its
result model, and whose description is its docstring -- so the server and the
CLI cannot describe a feature differently.

What this module adds is the boundary an agent must not cross, and every part
of it is enforced here rather than suggested to the model:

* **Only plugins marked for MCP exist.** Others are never registered, so no
  prompt, client or `call_tool` can reach them. `LETTERBOXD_MCP_READ_ONLY`
  (or `--read-only`) leaves out every write plugin as well.
* **A read plugin runs on a read-only session**, where DuckDB refuses any
  write, whatever SQL or code runs inside it.
* **No session can reach the filesystem through DuckDB**, writing ones
  included. list_builder takes SQL too, and a query that reads a file can
  leak it through an error message.
* **The database is opened per call and closed after**, so the server never
  locks the user's own `summer` commands out while their chat client is open.
* **Files are written only inside the output folder.** A path parameter is
  resolved against it, and an absolute path or one that climbs out is
  refused -- a list builder must not be able to overwrite an arbitrary file.
* **Only deliberate errors reach the client in detail.** A plugin's own
  errors explain themselves; anything unexpected is reported generically, so
  tracebacks and local paths stay out of the conversation.

The tool annotations (`readOnlyHint` and the rest) describe these rules to
clients. They are a mirror of the enforcement above, never a substitute.

**Tools are discovered, not all listed.** Every listed tool's schema costs
context on every turn, and the analysis tools will outnumber the ones a
question usually needs. So the listing holds describe_schema, query and
overview, every tool that changes something, and a search_tools / call_tool
pair that finds and runs the rest. Tools that write are always listed, and
call_tool refuses them: a client decides whether to ask the user before a
call from that tool's own annotations, which a call through the proxy would
hide.
"""

from __future__ import annotations

import sys
import typing
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any

import typer

from projectsummer import config
from projectsummer.prompts import PROMPTS
from projectsummer.core import db, registry
from projectsummer.core.errors import DatabaseBusyError, LetterboxdError, NoDatabaseError
from projectsummer.core.registry import Plugin

try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.server.context import Context
    from fastmcp.server.providers import Provider
    from fastmcp.server.transforms.search import BM25SearchTransform
    from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown
    from fastmcp.tools import Tool
    from fastmcp.tools.base import ToolResult
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover -- exercised by installing without the extra
    FastMCP = None  # type: ignore[assignment,misc]

SERVER_NAME = "projectsummer"

#: Read tools listed directly rather than found by search: the two nearly
#: every question goes through, and the one that says whether there is a
#: library to ask about yet.
ALWAYS_LISTED = ("describe_schema", "overview", "query")


def instructions(output_dir: Path) -> str:
    """What the server tells a client about itself, before any tool is called."""
    return (
        "The user's own Letterboxd library: the films they have watched, rated, "
        "liked or put on their watchlist, their diary of viewings, and their "
        "lists, enriched with TMDB metadata.\n\n"
        "To answer a question about it, use describe_schema, then query. "
        "describe_schema shows the tables, then a table's columns, then the "
        "values a column actually holds -- check a value's exact spelling there "
        "before filtering on it, because a wrong spelling silently matches "
        "nothing. query runs one SELECT; ask it for the answer (a count, an "
        "average, a top ten) rather than fetching many rows to work it out.\n\n"
        "people and film_credits hold who made each film -- gender, birthday, "
        "birthplace -- where a missing value means unknown.\n\n"
        "More tools exist than are listed, such as ready-made analyses of "
        "viewing trends over time. Find them with search_tools, describing "
        "what you want, and run one with call_tool; prefer one that fits over "
        "writing the SQL yourself.\n\n"
        "overview says whether the library is imported and enriched yet. Tools "
        "not marked read-only change something: sync fetches recent diary "
        "entries from Letterboxd, set_list_ranked marks a list as ranked, and "
        f"list_builder writes an importable list file under {output_dir}."
    )


class PathOutsideOutputError(LetterboxdError):
    """A file path given over MCP would land outside the output folder."""


# ----------------------------------------------------------------- tools

def exposed_plugins(plugins: Mapping[str, Plugin], *, read_only: bool) -> list[Plugin]:
    """The plugins this server offers, in name order.

    Name order because clients and model providers cache the tool list: the
    same server should list the same tools the same way every time.
    """
    return sorted(
        (
            item
            for item in plugins.values()
            if item.mcp and not (read_only and item.access == "write")
        ),
        key=lambda item: item.name,
    )


def as_tool(item: Plugin, database: Path, output_dir: Path) -> Tool:
    """Wrap one plugin as a FastMCP tool that enforces the rules above."""
    signature = item.signature
    path_parameters = {
        name for name, parameter in signature.parameters.items()
        if _is_path(parameter.annotation)
    }
    read_only = item.access == "read"

    def call(**arguments: Any) -> Any:
        try:
            for name in path_parameters:
                if arguments.get(name) is not None:
                    arguments[name] = confine_path(Path(arguments[name]), output_dir)
            if not database.exists():
                # A read-write session would quietly create an empty database
                # here, which is never what a write tool was asked to do.
                raise NoDatabaseError(
                    f"No library at {database} yet. Import a Letterboxd export "
                    f"first, with: summer ingest <your-export.zip>"
                )
            with db.session(database, read_only=read_only, external_access=False):
                return item.func(**arguments)
        except LetterboxdError as error:
            # Written to be read. Everything else is masked by the server.
            raise ToolError(str(error)) from None

    # FastMCP builds schemas from the function it is given: the parameters
    # from its annotations, their descriptions from its docstring's Args:
    # section, and the output schema from its return annotation.
    call.__name__ = item.name
    call.__qualname__ = item.name
    call.__doc__ = item.description
    call.__signature__ = signature  # type: ignore[attr-defined]
    call.__annotations__ = {
        name: parameter.annotation for name, parameter in signature.parameters.items()
    } | {"return": signature.return_annotation}

    return Tool.from_function(
        call,
        name=item.name,
        title=item.name.replace("_", " ").capitalize(),
        # The prose only: parameters and the result are described by their
        # schemas, so repeating Args:/Returns: would only spend tokens.
        description=item.help_text,
        tags={item.category, item.access},
        annotations=ToolAnnotations(
            read_only_hint=read_only,
            # Per the spec these only mean something for tools that write.
            destructive_hint=None if read_only else item.destructive,
            idempotent_hint=None if read_only else item.idempotent,
            open_world_hint=item.open_world,
        ),
    )


def confine_path(path: Path, output_dir: Path) -> Path:
    """Resolve a path an agent gave against the output folder, or refuse it.

    Raises:
        PathOutsideOutputError: If the path is absolute, or climbs out of the
            output folder by `..` or a link.
    """
    if path.is_absolute() or path.drive or path.anchor:
        raise PathOutsideOutputError(
            f"Give a path relative to the output folder ({output_dir}), such as "
            f"'lists/nolan.csv'; {path} is absolute."
        )
    base = output_dir.resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise PathOutsideOutputError(
            f"{path} would be written outside the output folder ({output_dir})."
        )
    return target


def _is_path(annotation: Any) -> bool:
    return annotation is Path or Path in typing.get_args(annotation)


if FastMCP is not None:

    class RegistryProvider(Provider):
        """Serves the registry's MCP plugins as tools, built once at startup."""

        def __init__(self, plugins: Sequence[Plugin], database: Path, output_dir: Path):
            super().__init__()
            self._tools = [as_tool(item, database, output_dir) for item in plugins]

        async def _list_tools(self) -> Sequence[Tool]:
            return self._tools

    class ToolDiscovery(BM25SearchTransform):
        """Lists the pinned tools, and lets the rest be searched for and called.

        FastMCP's own call_tool proxy would run any tool in the catalog. This
        one runs only the read tools that search can find, so a call that
        changes something is always made to that tool by name, where the
        client can see its annotations.
        """

        def __init__(self, pinned: Sequence[str]):
            super().__init__(
                always_visible=list(pinned),
                search_result_serializer=serialize_tools_for_output_markdown,
            )

        def _make_call_tool(self) -> Tool:
            transform = self

            async def call_tool(
                name: Annotated[str, "The name of a tool found with search_tools"],
                arguments: Annotated[
                    dict[str, Any] | None, "Arguments to pass to that tool"
                ] = None,
                ctx: Context = None,  # type: ignore[assignment]
            ) -> ToolResult:
                """Run a tool found with search_tools, by name.

                Tools already in your tool list are called directly instead.
                """
                searchable = {
                    tool.name for tool in await transform._get_visible_tools(ctx)
                    if tool.annotations is not None and tool.annotations.read_only_hint
                }
                if name not in searchable:
                    catalog = {tool.name for tool in await transform.get_tool_catalog(ctx)}
                    if name in catalog:
                        raise ToolError(
                            f"{name} is in your tool list; call it directly rather "
                            f"than through call_tool."
                        )
                    raise ToolError(
                        f"Unknown tool: {name!r}. Use search_tools to find one."
                    )
                return await ctx.fastmcp.call_tool(name, arguments or {})

            return Tool.from_function(fn=call_tool, name=self._call_tool_name)


def pinned_tools(plugins: Sequence[Plugin]) -> list[str]:
    """The tools listed directly: the core read tools and every write tool."""
    return [
        item.name for item in plugins
        if item.name in ALWAYS_LISTED or item.access == "write"
    ]


# ----------------------------------------------------------------- server

def build_server(
    database: Path | None = None,
    *,
    read_only: bool | None = None,
    plugins: Mapping[str, Plugin] | None = None,
    output_dir: Path | None = None,
) -> FastMCP:
    """Build the server.

    Args:
        database: The library to serve. Defaults to :func:`config.db_path`.
        read_only: Leave out every write plugin. Defaults to
            ``LETTERBOXD_MCP_READ_ONLY``.
        plugins: The plugins to consider. Defaults to everything discovered,
            built-in and third-party.
        output_dir: Where tools may write files. Defaults to
            :func:`config.output_dir`.
    """
    if FastMCP is None:
        raise RuntimeError(_MISSING_EXTRA)

    database = Path(database) if database is not None else config.db_path()
    output_dir = Path(output_dir) if output_dir is not None else config.output_dir()
    read_only = config.mcp_read_only() if read_only is None else read_only
    plugins = registry.discover() if plugins is None else plugins

    exposed = exposed_plugins(plugins, read_only=read_only)
    server = FastMCP(
        SERVER_NAME,
        instructions=instructions(output_dir),
        version=_package_version(),
        providers=[RegistryProvider(exposed, database, output_dir)],
        transforms=[ToolDiscovery(pinned_tools(exposed))],
        mask_error_details=True,
    )
    # Prompts are text a client offers by name, and run through the same
    # tools as anything else. catch_up asks for sync, so it is left out of a
    # read-only server rather than offering something that cannot work.
    for prompt in PROMPTS:
        if read_only and prompt.__name__ == "catch_up":
            continue
        server.prompt(prompt)
    return server


def prepare_database(database: Path) -> str | None:
    """Bring an existing library's schema up to date before serving it.

    Tools open read-only sessions, which cannot apply schema.sql -- so without
    this, a library upgraded to a newer version of the tool would never get
    the newer column descriptions. It is one read-write open, closed at once.

    Returns:
        A message worth showing on stderr, or None if all went well.
    """
    if not database.exists():
        return (
            f"No library at {database} yet; tools will say so until an export is "
            f"imported with: summer ingest <your-export.zip>"
        )
    try:
        with db.session(database, external_access=False):
            pass
    except DatabaseBusyError:
        return f"{database} is in use, so its schema was not refreshed; serving it as it is."
    except LetterboxdError as error:
        return str(error)
    return None


def _package_version() -> str | None:
    try:
        return version("projectsummer")
    except PackageNotFoundError:
        return None


_MISSING_EXTRA = (
    "The MCP server needs the optional mcp dependencies. Install them with: "
    "pip install 'projectsummer[mcp]'  (or: uv sync --extra mcp)"
)


# ------------------------------------------------------------ entry point

def _serve(
    database: Annotated[
        Path | None,
        typer.Option("--db", metavar="PATH", help="Library to serve. Defaults to LETTERBOXD_DB or your per-user data directory."),
    ] = None,
    read_only: Annotated[
        bool,
        typer.Option("--read-only", help="Offer no tool that changes anything. Also set by LETTERBOXD_MCP_READ_ONLY."),
    ] = False,
) -> None:
    """Serve your Letterboxd library to an MCP client over stdio."""
    if FastMCP is None:
        print(_MISSING_EXTRA, file=sys.stderr)
        raise typer.Exit(code=1)

    # stdout is the protocol channel; anything for a human goes to stderr.
    server = build_server(database, read_only=read_only or None)
    for failure in registry.load_failures():
        print(f"Skipped plugin {failure}", file=sys.stderr)
    message = prepare_database(Path(database) if database else config.db_path())
    if message:
        print(message, file=sys.stderr)

    server.run(show_banner=False)


def main() -> None:
    """Console-script entry point: `summer-mcp`."""
    typer.run(_serve)


if __name__ == "__main__":
    main()
