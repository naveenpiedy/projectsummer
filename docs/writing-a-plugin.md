# Writing a plugin

Every feature in Project Summer is one decorated function. The CLI reads its
signature to build a command, and the MCP server reads the same signature to
build a tool, so a feature is written once and never described twice.

This guide covers adding one, whether inside the project or in a directory of
your own.

- [The shortest one that works](#the-shortest-one-that-works)
- [Loading your own](#loading-your-own)
- [The decorator](#the-decorator)
- [What the registry enforces](#what-the-registry-enforces)
- [Results](#results)
- [Access, and what reaches an assistant](#access-and-what-reaches-an-assistant)
- [Talking to the database](#talking-to-the-database)
- [Saying what is happening](#saying-what-is-happening)
- [Asking questions](#asking-questions)
- [Errors](#errors)
- [Files](#files)
- [Changing the schema](#changing-the-schema)
- [Testing](#testing)
- [A worked example](#a-worked-example)
- [Checklist](#checklist)

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

Several directories work too, separated by `;` on Windows and `:` elsewhere.
Every top-level `.py` file in each is imported. A file that fails to import is
reported and skipped rather than taking the tool down, so one broken plugin
cannot stop the others loading.

A plugin is ordinary Python running inside the tool, so only add plugins you
trust. The read-only connection limits what a read plugin can do *through the
database*, not what its own code can do.

## The decorator

```python
@plugin(
    name="list_builder",      # defaults to the function's name
    category="lists",         # groups commands in --help, and tags the tool
    access="write",           # "read" (default) or "write"
    mcp=True,                 # offered to assistants? see below for defaults
    serves=False,             # keeps running after returning, e.g. a server
    destructive=True,         # a write that can overwrite what is there
    idempotent=True,          # calling again changes nothing further
    open_world=False,         # reaches over the network
)
```

`name` becomes the tool name and, with underscores turned into hyphens, the
command name: `list_builder` is `summer list-builder`. `category` decides
which panel of `summer --help` the command appears under, so a new kind of
feature is worth a new category.

`destructive`, `idempotent` and `open_world` only become tool annotations,
which clients use to decide whether to ask you before a call. They are
promises to a reader, not enforcement.

`serves=True` is for a plugin that starts something which must outlive the
call, like `summer ui`. The CLI keeps the process alive until Ctrl+C, and the
plugin is never offered over MCP.

## What the registry enforces

These are checked when the module is imported, so a mistake fails at startup
with a message saying what to fix, rather than producing a subtly broken
command.

**Every parameter has a type hint**, and the return type is a Pydantic model
subclassing `Result`. The hints are the schema: they become CLI option types
and the tool's input schema. Supported hints are the ordinary ones — `str`,
`int`, `float`, `bool`, `Path`, `list[str]`, `Literal[...]`, and `| None`
for optional.

**The result says what it holds.** The model needs a docstring, every field
needs one under it, and `dict`, `Any`, a bare `list` and `dict[str, Any]` are
refused anywhere in it, including nested models. An agent reading the output
schema would learn nothing from those.

**Result models live at module level**, above the function, so they can be
imported and referenced by name.

**The docstring is the documentation.** The first line is the command summary,
the prose becomes the tool description, and the `Args:` section becomes each
option's help text and each argument's description. `Returns:` and `Raises:`
are for whoever reads the code; the tool description leaves them out, since
the output schema already says what comes back.

Write the prose for whoever is deciding whether to use the feature, including
a model choosing between tools.

## Results

A result is a contract. Over MCP it is the tool's output schema, and code an
agent writes will rely on the field names.

```python
class Share(Result):
    """One value of a breakdown, and how many viewings it accounts for."""

    name: str
    """The genre, language code or decade."""
    viewings: int
    """Viewings of a film with this value."""


class TrendPeriod(Result):
    """Your viewing in one year or one month."""

    period: str
    """The year, e.g. "2024", or the month, e.g. "2024-03"."""
    genres: list[Share]
    """The most-watched genres, most viewings first."""
```

Nest models rather than reaching for a dict. Use `Literal["year", "month"]`
where a field is one of a few known values: it documents itself, and over MCP
the schema says exactly which values are possible.

Prefer a flat list carrying its own labels to a deeply nested tree. `taste`
returns one row per entry, each with a `facet` and a `standing` field, which
the CLI lays out as a table and an assistant can filter — nesting them by
facet made the terminal output unreadable.

A field that can be absent is `| None`, and its docstring should say what
None means: not yet fetched, unknown to TMDB, or genuinely nothing.

## Access, and what reaches an assistant

- **`access="read"`** (the default) or **`"write"`**. A plugin that writes the
  database, writes a file, or makes a network request is `"write"`.
- **Read plugins are offered over MCP**; write plugins only with `mcp=True`;
  a `serves=True` plugin never is.
- **The label is enforced, not trusted.** Over MCP a read plugin runs on a
  connection DuckDB itself keeps read-only, with file access switched off, so
  a plugin that claims to read and then writes fails there. The test suite
  runs every read plugin on such a connection, which is where a mislabelled
  one is caught.
- A plugin that needs a person at the terminal — see
  [Asking questions](#asking-questions) — should stay off MCP entirely.

Not every tool is listed in a conversation. The assistant sees
`describe_schema`, `query`, `overview` and every write tool, and finds the
rest through `search_tools`. Search ranks on a plugin's name, description and
argument descriptions, so a docstring using the words someone would actually
ask in is the one that gets found. "Show how your viewing changes over time,
by year or by month" is findable; "Temporal aggregation utility" is not.

## Talking to the database

Plugins never open connections. Call `db.query(sql, params)` for rows as
dictionaries, or `db.get_connection()` for the connection itself:

```python
rows = db.query(
    "SELECT title FROM films WHERE year >= ? AND list_contains(genres, ?)",
    [1990, "Horror"],
)
```

Always pass values as parameters rather than formatting them into the SQL.

The CLI uses one connection for the process; the MCP server opens one per tool
call and closes it, so your own `summer` commands are never locked out while a
chat client is open. Sessions do not nest.

`db.require_films()` raises a clear error when the library has not been
enriched yet, which beats returning nothing and letting the caller guess.

Some habits the schema expects:

- **List columns are real lists.** `list_contains(genres, 'Horror')` and
  `unnest(genres)` work directly; there is no string splitting.
- **People are joined by `person_id`**, through `film_credits` and `people`.
  The name lists on `films` (`directors`, `cast_members`, …) are a quick path
  for display only: two people can share a name.
- **Counts of viewings come from `diary_entries`**, or the `film_watch_stats`
  view, never from a column on `films`.
- **Bulk writes go in as one JSON string** with `json_transform` — see
  `enrich.store_credits`. `executemany` converts and upserts a row at a time:
  50 films with credits took about 30 seconds that way and take 0.2 this way.

[The cookbook](cookbook.md) has worked queries for most of the common shapes.

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
installed to listen, `track` is exactly `iter(items)` — no output, no cost,
nothing to disable in tests.

## Asking questions

A plugin that is a conversation rather than a call — `rank` asks which of two
films is better, over and over — uses `asking`:

```python
from projectsummer.core import asking

asking.require_someone()        # refuse early, before doing any work
asking.tell("Quarter-finals")
answer = asking.choose("Which is better?", {"1": "Heat", "2": "Collateral"})
```

The CLI answers with single keypresses; `tell` shows a message that needs no
answer. With nobody installed to answer, `choose` raises `NoOneToAskError`, so
such a plugin is CLI-only: leave `mcp` alone and call `require_someone()`
before the first slow step.

## Errors

Raise the exceptions in `core.errors` rather than returning a sentinel. The
CLI prints them in red and exits non-zero; the MCP server passes the message
to the assistant. Anything else is masked over MCP, so a stray `KeyError`
tells an assistant nothing — and leaks nothing.

| Raise | When |
|---|---|
| `NoResultError` | The query ran but nothing matched |
| `EmptyDatabaseError` | Nothing imported yet (`db.require_films()` raises it) |
| `InvalidArgumentError` | An argument is out of range or contradictory |
| `MissingArgumentError` | An argument is needed, given the others |
| `LetterboxdError` subclass of your own | Anything specific to your plugin |

Messages say what to do next, and name real alternatives:

```python
raise NoResultError(
    f"No list with slug {slug!r}. Known slugs include: {suggestions}."
)
```

`MissingArgumentError` carries the argument and a question:

```python
raise MissingArgumentError(
    "By month, give the year to show, e.g. year=2024.",
    argument="year",
    question="Which year",
)
```

An assistant reads the message and calls again with the argument; the CLI asks
the question at the terminal and runs the command again with the answer.

## Files

A plugin that writes a file is `access="write"`, and usually `destructive=True`
since it can overwrite something. Take the destination as a `Path` parameter:
over MCP, a `Path` is resolved inside the server's output folder, and an
absolute path or one climbing out with `..` is refused. That confinement is
the server's, not yours, but it only applies to parameters annotated `Path`.

`list_builder` is the worked example, including splitting a file that exceeds
Letterboxd's import limit.

## Changing the schema

Everything in `core/schema.sql` is `CREATE ... IF NOT EXISTS`, re-applied only
when the file's fingerprint changes.

- **A new table** reaches existing libraries automatically. Prefer it, and
  give it every column it will ever need up front.
- **A new or changed column on an existing table** does not. It needs a
  `SCHEMA_VERSION` bump, and with no migration tooling that means every user
  rebuilds — including about twenty minutes of Letterboxd resolution. Think
  hard before asking for that.
- **There are no foreign keys**, deliberately: DuckDB rejects LIST-column
  updates on referenced rows inside a transaction. Add dangling-reference
  checks to the `integrity_orphans` view instead.
- **Every public table and column needs a `COMMENT ON` description**, enforced
  by a test. Agents read them through `describe_schema`, and they are the
  difference between a model writing the right query and guessing. Register a
  new table in `catalog.PUBLIC_RELATIONS` or `INTERNAL_RELATIONS`.

## Testing

The suite never touches the network. `conftest.py` restores the registry after
every test, so a plugin registered inside a test cannot leak into another.

- `conn` gives an in-memory database with sample films; `empty_conn` gives an
  empty one.
- Anything that goes through `db.session` needs a real file under `tmp_path`.
- Call the plugin function directly for its behaviour, and through
  `typer.testing.CliRunner` for the command and its output.
- For a plugin that asks questions, write a fake asker: a small class with
  `tell` and `choose`, installed with `asking.answering_with(...)`. See
  `tests/test_ranking.py`.
- Set up fixture data so the expected answer is arithmetic, not a judgement
  call, and say in a comment why the numbers are what they are.

If you add a built-in plugin to this project, the lists pinning what reaches
MCP have to change with it:
`test_registry.py::test_exactly_these_builtins_are_offered_over_mcp`, and
`EXPOSED` and `LISTED` in `test_mcp_server.py`. That is deliberate: exposing a
tool to agents should be a decision, not a side effect. A read plugin with a
required argument also needs an entry in `test_results.py::SAMPLE_ARGUMENTS`,
or it cannot be run on a read-only connection and the test fails.

## A worked example

A plugin answering "which people keep turning up in the films I love?", with
most of the above in it:

```python
from typing import Literal

from projectsummer.core import db
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result


class Regular(Result):
    """Someone who keeps appearing in the films you rate highly."""

    name: str
    """Their name, as TMDB gives it."""
    person_id: int
    """TMDB person id: the only safe way to tell namesakes apart."""
    films: int
    """Films of theirs you rated at or above the threshold."""
    average_rating: float
    """Your average for those films."""


class Regulars(Result):
    """The people behind your favourites."""

    role: str
    """The credit role counted."""
    at_least: float
    """The rating a film had to reach to count."""
    people: list[Regular]
    """Most films first."""


@plugin(category="analysis")
def regulars(
    role: Literal["director", "actor", "writer", "composer"] = "director",
    at_least: float = 4.0,
    top: int = 10,
) -> Regulars:
    """Find the people who keep turning up in the films you rate highly.

    Counts credits on films you rated at or above a threshold, so it answers
    "whose work do I actually love?" rather than "whose work have I seen?".

    Args:
        role: The credit role to count.
        at_least: The rating a film must reach to count, 0.5 to 5.
        top: How many people to return.

    Returns:
        The people, most films first.

    Raises:
        InvalidArgumentError: If at_least is outside 0.5 to 5.
        EmptyDatabaseError: If the library holds no films yet.
    """
    if not 0.5 <= at_least <= 5:
        raise InvalidArgumentError(
            f"at_least is a rating from 0.5 to 5, not {at_least}."
        )
    db.require_films()

    rows = db.query(
        """
        SELECT p.name,
               p.person_id,
               count(*)                   AS films,
               round(avg(f.my_rating), 2) AS average_rating
        FROM film_credits c
        JOIN people p USING (person_id)
        JOIN films f USING (tmdb_id)
        WHERE c.role = ? AND f.my_rating >= ?
        GROUP BY p.person_id, p.name
        ORDER BY films DESC, average_rating DESC, p.name
        LIMIT ?
        """,
        [role, at_least, top],
    )
    return Regulars(
        role=role,
        at_least=at_least,
        people=[Regular(**row) for row in rows],
    )
```

Worth noticing: the SQL groups by `person_id` and not by name; every value is
a parameter; `Literal` keeps the role honest in both frontends; the threshold
is checked before the database is touched; and every field says what it means.

## Checklist

- [ ] Type hints on every parameter, and a `Result` subclass returned
- [ ] Docstring with a first-line summary and an `Args:` entry per parameter,
      in words someone would search for
- [ ] A docstring under every result field, and no `dict`, `Any` or bare
      `list` anywhere in it
- [ ] `access="write"` if it writes anything, anywhere
- [ ] Nothing printed; `progress` for progress, `asking` for questions
- [ ] Errors from `core.errors`, with messages saying what to do next
- [ ] Values passed as SQL parameters, people grouped by `person_id`
- [ ] Tests: the function directly, the command through `CliRunner`, and the
      pinned MCP lists updated if it is a built-in
