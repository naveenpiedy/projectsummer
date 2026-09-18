# Writing a plugin

Every feature in Project Summer is one decorated function. The CLI reads its
signature to build a command, and the MCP server reads the same signature to
build a tool, so a feature is written once and never described twice.

This guide is for adding your own, either inside the project or in a directory
of your own.

## The shortest one that works

```python
from projectsummer.core import db
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result


class LongestFilms(Result):
    """The longest films you have watched."""

    films: list[str]
    """Titles, longest first."""
    longest_minutes: int | None
    """The running time of the longest, or None if none have one."""


@plugin(category="explore")
def longest_films(limit: int = 5) -> LongestFilms:
    """List the longest films you have watched.

    Args:
        limit: How many to return.

    Returns:
        The titles, longest first.
    """
    rows = db.query(
        "SELECT title, runtime FROM films WHERE watched AND runtime IS NOT NULL "
        "ORDER BY runtime DESC LIMIT ?",
        [limit],
    )
    return LongestFilms(
        films=[row["title"] for row in rows],
        longest_minutes=rows[0]["runtime"] if rows else None,
    )
```

That is a `summer longest-films --limit 10` command and an MCP tool, with help
text, argument validation, JSON output and a table, none of it written here.

## Loading your own

Put the file in a directory and point `LETTERBOXD_PLUGIN_PATH` at it:

```bash
LETTERBOXD_PLUGIN_PATH=~/my-summer-plugins summer longest-films
```

Several directories work too, separated by `;` on Windows and `:` elsewhere. A
file that fails to import is reported and skipped rather than taking the tool
down, so one broken plugin cannot stop the others loading.

A plugin is ordinary Python running inside the tool, so only add plugins you
trust. The read-only connection limits what a read plugin can do *through the
database*, not what its own code can do.

## The rules the registry enforces

A plugin that breaks one of these is refused at import, with a message saying
what to fix.

**Every parameter needs a type hint**, and the return type must be a Pydantic
model subclassing `Result`. The hints are the schema: they become CLI option
types and the tool's input schema.

**The result must say what it holds.** Every field needs a docstring under it,
and the model itself needs one. `dict`, `Any`, a bare `list` and
`dict[str, Any]` are refused anywhere in a result, because an agent reading
the output schema would learn nothing from them. Define result models at
module level, above the function.

**The docstring is the documentation.** Its first line is the command's
summary, the prose becomes the tool description, and the `Args:` section
becomes each option's help text and each argument's description. Write it for
whoever is deciding whether to use the feature, including a model choosing
between tools.

## Access, and what reaches an assistant

```python
@plugin(category="lists", access="write", mcp=True, idempotent=True)
```

- **`access`** is `"read"` (the default) or `"write"`. A plugin that writes the
  database, writes a file, or makes a network request is `"write"`.
- **Read plugins are offered over MCP**; write plugins only with `mcp=True`;
  a plugin with `serves=True` (one that starts a server and keeps running)
  never is.
- **The label is enforced, not trusted.** Over MCP a read plugin runs on a
  connection DuckDB itself keeps read-only, with file access switched off, so
  a plugin that claims to read and then writes fails there.
- **`destructive`, `idempotent` and `open_world`** become tool annotations.
  Clients use them to decide whether to ask you before a call.

Not every tool is listed in a conversation: the assistant sees the core read
tools and every write tool, and finds the rest through `search_tools`. Search
ranks on a plugin's name, description and argument descriptions, so a plugin
whose docstring uses the words someone would ask in is the one that gets
found.

## Talking to the database

Plugins never open connections. Call `db.query(sql, params)` for rows as
dictionaries, or `db.get_connection()` for the connection itself. The CLI has
one connection for the process; the MCP server opens one per tool call and
closes it, so your own `summer` commands are never locked out while a chat
client is open.

`db.require_films()` raises a clear error when the library has not been
enriched yet, which is better than returning nothing.

## Saying what is happening

**Never print.** Over MCP, stdout carries the protocol, and anything written
to it corrupts the conversation.

For long work, report progress and let the frontend decide how to show it:

```python
from projectsummer.core import progress

for film in progress.track(films, "Fetching metadata"):
    ...
progress.note("Rebuilding the diary")
```

The CLI draws a progress bar; the MCP server ignores both. With nothing
installed to listen, `track` is exactly `iter(items)`.

For a plugin that must ask questions as it runs, like `rank`:

```python
from projectsummer.core import asking

answer = asking.choose("Which is better?", {"1": "Heat", "2": "Collateral"})
asking.tell("Quarter-finals")
```

The CLI answers with single keypresses. With nobody there to answer, `choose`
raises, so such a plugin is CLI-only: call `asking.require_someone()` early
rather than failing halfway through.

## Errors

Raise the exceptions in `core.errors` rather than returning a sentinel. The
CLI prints them in red and exits non-zero; the MCP server passes the message
to the assistant. Anything else is masked over MCP, so a stray `KeyError`
tells an assistant nothing and leaks nothing.

Write messages that say what to do next: "No list with slug 'favs'. Known
slugs include: faves, horror."

`MissingArgumentError` is for an argument only sometimes needed:

```python
raise MissingArgumentError(
    "By month, give the year to show, e.g. year=2024.",
    argument="year",
    question="Which year",
)
```

An assistant reads the message and calls again with the argument; the CLI asks
the question at the terminal and runs the command again with the answer.

## Testing one

The suite never touches the network. `conftest.py` restores the registry after
every test, so a plugin registered inside a test cannot leak into another.

- `conn` gives an in-memory database with sample films; `empty_conn` gives an
  empty one.
- Anything that goes through `db.session` needs a real file under `tmp_path`.
- Call the plugin function directly for its own behaviour, and through
  `typer.testing.CliRunner` for the command.

If you add a built-in plugin to this project, the lists pinning what reaches
MCP have to change with it: `test_registry.py::test_exactly_these_builtins_are_offered_over_mcp`,
and `EXPOSED` and `LISTED` in `test_mcp_server.py`. That is deliberate:
exposing a tool to agents should be a decision, not a side effect.
