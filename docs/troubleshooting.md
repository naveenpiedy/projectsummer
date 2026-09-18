# Troubleshooting

Every error Project Summer raises deliberately says what to do next. This page
covers the ones worth more explanation, and a few things that are not errors
but look like them.

## The library will not open

### "DuckDB failed to replay letterboxd.duckdb.wal"

A process was stopped part-way through writing — Ctrl+C during a command, or a
chat client restarting its MCP server — and left changes in the write-ahead
log that DuckDB cannot replay. See
[the bug report draft](duckdb-wal-comment-on-column-replay.md) for why.

Everything saved before that point is in the main file. To recover:

1. Close every program using the library, including your chat client.
2. Rename `letterboxd.duckdb.wal` to `letterboxd.duckdb.wal.unreplayable`.
3. Open it again.
4. Keep the renamed file until you have checked that nothing recent is
   missing. `summer overview` is the quickest check.

Current versions no longer write anything during an ordinary open, so this
should not happen again on a library built by them.

### "is in use by another process"

DuckDB lets only one process hold a library read-write. Usually that is an
`enrich` still running, or a chat client's MCP server mid-call. Wait for it to
finish, or close the other program.

The MCP server opens the library per call and closes it straight after, so
your own `summer` commands work while a chat client is open. A long `enrich`
is the exception: it holds the library for its whole run.

### "This database uses schema version X"

The library was built by a version of the tool whose tables differ from this
one's. There is no migration tooling, so re-create it:

```bash
summer setup letterboxd-you-2026-09-16.zip
```

That re-imports, re-resolves and re-enriches. Resolution is the slow part,
about twenty minutes for a thousand films, and it is cached, so a rebuild in
the same file is faster than the first time. Point `LETTERBOXD_DB` at another
path first if you want to keep the old file.

### "No database at ..."

`--db` was given a path that does not exist. That is treated as a typo rather
than an instruction to create one, except for the commands that legitimately
create a library (`ingest`, `setup`, `resolve`, `enrich`, `sync`).

## Nothing to query yet

`summer overview` says how far through the pipeline your library is:

- **empty** — nothing imported. Run `summer ingest your-export.zip`.
- **staged** — an export is imported but no film has metadata yet. Run
  `summer resolve`, then `summer enrich`.
- **ready** — films are enriched and queryable.

`summer setup your-export.zip` does all three in order.

## TMDB

### "No TMDB API token"

`enrich` needs one, and so does `sync` when your feed holds a film you have
never logged. Create a free account, take the long **API Read Access Token**
from <https://www.themoviedb.org/settings/api> — not the short v3 key — and
put it in `.env` beside the project or in your environment:

```
TMDB_API_KEY=eyJhbGciOi...
```

`summer-mcp` finds `.env` through `uv run --directory /path/to/projectsummer`,
which is why the MCP configuration in the README passes it.

### Enrichment stopped part-way

Run `summer enrich` again. Films and people are written in batches as they
arrive, and a second run fetches only what is still missing. People come last,
so an interruption there leaves your films, viewing data and diary complete.

### A film is stuck as pending

A film TMDB has no record of is remembered and not asked for again. If you
think TMDB has since added it:

```bash
summer enrich --retry-missing
```

### Rate limiting

Requests go out eight at a time and the client retries a rate-limited request.
Sustained rate limiting fails the run rather than being silently recorded as
"no such film" — re-run it later.

## Lists

### A list compares as empty

`list_overlap` and `rank --from-list` only count films that have a TMDB id. A
Letterboxd export identifies list films by link alone, so:

```bash
summer resolve   # looks up any list films not already known
summer enrich    # attaches the ids to the list entries
```

Libraries built before this worked need one run of each. A film Letterboxd
could not resolve stays out of the comparison.

## Queries

### "Only the first N rows are shown"

A result stops at `max_rows` (100 by default, up to 1000) and at about 20,000
characters, whichever comes first. The character limit is what a wide
`SELECT *` hits. Select the columns you need, or ask the database for the
answer — a count, an average, a top ten — rather than reading rows to work it
out.

### "The query was stopped after 30 seconds"

Usually a join missing its `ON` condition. Filter earlier, aggregate, or check
the join.

### A filter matches nothing

Values must be spelled exactly as stored. TMDB's genre is `Science Fiction`,
not `Sci-Fi`. Check before filtering:

```bash
summer describe-schema --table films --column genres
summer describe-schema --table films --column directors --search "ravi kumar"
```

## The MCP server

### An assistant cannot see a new tool

Restart the server in your chat client. It builds its tool list at startup, so
a newly installed version is not picked up until then.

### An assistant says it only has a few tools

That is deliberate. It sees `describe_schema`, `query`, `overview` and every
tool that changes something, and finds the rest with `search_tools`. If it is
not finding one, ask it to search for what the tool does rather than for its
name.

### "summer-mcp: command not found", or it exits at once

The MCP dependencies are optional:

```bash
uv sync --extra mcp
```

### An assistant cannot write a list file

Files go only into the output folder in your per-user data directory
(`%LOCALAPPDATA%\projectsummer\output` on Windows). An absolute path, or one
climbing out with `..`, is refused.

## `rank`

### "This needs someone to answer questions as it runs"

`rank` asks you to choose between two films, so it only works at a terminal.
It is not offered over MCP, and it will not run with its input piped in.
