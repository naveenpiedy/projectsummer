# Contributing

Thank you for looking. Project Summer is a small, finished-feeling tool, so
the most useful contributions are usually a bug report with a reproduction, or
a plugin that does something the built-ins do not.

- [Getting set up](#getting-set-up)
- [Running the tests](#running-the-tests)
- [Do not point write commands at a real library](#do-not-point-write-commands-at-a-real-library)
- [What the code depends on](#what-the-code-depends-on)
- [Style](#style)
- [Pull requests](#pull-requests)
- [Reporting a bug](#reporting-a-bug)

## Getting set up

Needs Python 3.11 or newer.

```bash
git clone https://github.com/naveenpiedy/projectsummer
cd projectsummer
uv sync
```

`uv sync` installs the development dependencies too, FastMCP among them, so
the MCP server's tests run. Without [uv](https://docs.astral.sh/uv/),
`pip install -e '.[mcp]'` and `pip install pytest` get you the same thing.

You do not need a TMDB token or a Letterboxd export to work on most of the
project: the tests build their own library from a fixture.

## Running the tests

```bash
uv run pytest
```

About 670 tests, a little over two minutes, and not one of them touches the
network. HTTP is faked at the session boundary, so the awkward cases — a
redirect to a diary entry, a page with no ids, a timeout, TMDB rate limiting,
an interruption mid-run — are exercised deliberately rather than waited for.
Keep it that way: a test that needs the internet will fail for someone else,
on someone else's schedule, for reasons that have nothing to do with their
change.

The documentation is tested with everything else. Every SQL block in the
[query cookbook](docs/cookbook.md) runs against a fixture library and must
return rows; every `@plugin` example in
[writing a plugin](docs/writing-a-plugin.md) is loaded as a third-party plugin
and called; every link between Markdown files is resolved, anchors included.
Moving a page or renaming a column fails a test, which is the point.

## Do not point write commands at a real library

`ingest`, `resolve`, `enrich`, `sync` and `setup` change a database, and the
last three reach the network. Run them against a scratch database, never
against your own library while you are debugging:

```bash
LETTERBOXD_DB=./scratch.duckdb uv run summer ingest path/to/export.zip
```

`resolve` reads Letterboxd's website a page at a time, and `enrich` spends
TMDB requests for tens of minutes. A running `enrich` also holds the database
read-write, which locks every MCP client out of it.

## What the code depends on

These are not style preferences; tests fail when they are broken.

**A feature is one decorated function.** It needs `@plugin(...)`, full type
hints and a Google-style docstring, whose `Args:` section becomes both CLI
help and MCP argument descriptions. It must return a Pydantic model
subclassing `Result` with a docstring under every field, defined at module
level above the function. The registry refuses `dict`, `Any`, bare `list` and
`dict[str, Any]` anywhere in a result, because the MCP schema would be useless
without the shape. [Writing a plugin](docs/writing-a-plugin.md) is the full
guide, and the fastest way in.

**Access is enforced, not advisory.** `access="read"` plugins run on a DuckDB
connection the server keeps read-only. A plugin that writes the database,
writes files, or needs the network is `access="write"`. Read plugins are
exposed over MCP by default, write plugins only with `mcp=True`, and
`serves=True` plugins never. Changing what is exposed means changing the
pinned lists in `tests/test_registry.py` and `tests/test_mcp_server.py` in the
same commit — they exist so that no tool reaches an assistant by accident.

**Schema changes are expensive for the people already using it.** A new table
reaches existing databases automatically, so prefer one, and give it every
column it will ever need up front. A new or changed column on an existing
table does not: it needs a `SCHEMA_VERSION` bump, and with no migrations that
means every user rebuilds, including about twenty minutes of Letterboxd
resolution. Every public table and column needs a `COMMENT ON` description —
a test enforces it, and assistants read them. [The data
model](docs/data-model.md) explains the shape and why it is that way.

**Nothing prints to stdout.** It is the MCP protocol channel. Report progress
with `progress.track(...)` and `progress.note(...)`, and ask questions with
`asking.choose(...)`; the CLI renders both, and the server ignores or refuses
them.

**Nothing is hardcoded to one machine or one person.** No paths, no
usernames, no tokens. Configuration comes from environment variables or
`.env`. Never commit `.env`, a `.duckdb` file, or a Letterboxd export; they
are all gitignored, and an export is somebody's viewing history.

If you are changing a boundary rather than adding a feature, please read
[SECURITY.md](SECURITY.md) first — it lists the ones that are deliberate.

## Style

Match the code around you. Docstrings and comments explain *why*, in full
sentences, with British spelling ("summarise", "serialisation"). Error
messages say what to do next, because a user reading one is stuck.

Commit subjects are imperative and short; the body explains the reasoning
rather than restating the diff.

## Pull requests

`main` is protected, so changes arrive by pull request. Branch, push, open it,
and say what problem you are solving — a PR that only describes the diff makes
a reviewer guess at the intent.

Before you open it: `uv run pytest` passes, new behaviour has a test, and any
documentation the change contradicts is updated in the same PR.

Large or speculative work is worth an issue first. Not every good idea belongs
in the built-ins — the plugin system exists so that yours can live in your own
directory and still appear in the CLI and over MCP under the same rules.

## Reporting a bug

Open an [issue](https://github.com/naveenpiedy/projectsummer/issues) with the
command you ran, what you expected, what happened, and your OS and Python
version. If it involves your library, `summer overview` says how big it is
without saying what is in it. Never paste your TMDB token.

[Troubleshooting](docs/troubleshooting.md) covers the failures that look like
bugs and are not — a write-ahead log that will not replay, a locked database,
an assistant that cannot see a tool.

If it is a security problem, do not open an issue: [SECURITY.md](SECURITY.md)
has the private route.
