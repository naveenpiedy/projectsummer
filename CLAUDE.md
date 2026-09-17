# Project Summer

Query and explore one person's Letterboxd library from a CLI (`summer`) or an
MCP server (`summer-mcp`). Built for other people to install: one user per
DuckDB file, nothing personal or machine-specific in the repo. See README.md
for what it does; this file is about changing it safely.

## Commands

```bash
uv sync                                   # includes dev deps (pytest, FastMCP)
uv run pytest                             # ~490 tests, ~90s, no network
uv run pytest tests/test_enrich.py -k people
uv run summer --help                      # CLI, generated from the registry
uv run summer-mcp --db PATH               # MCP server over stdio
```

The real library lives in the per-user data directory
(`%LOCALAPPDATA%\projectsummer\letterboxd.duckdb` on Windows). Don't run write
commands (`ingest`, `resolve`, `enrich`, `sync`) against it without asking:
`resolve` reads Letterboxd's site, `enrich` spends the user's TMDB token for
tens of minutes, and a running `enrich` locks the file against their MCP
clients.

## Where things are

| Path | Role |
|---|---|
| `src/projectsummer/core/registry.py` | `@plugin` decorator, validation, discovery |
| `src/projectsummer/core/plugins/` | Built-in features, one decorated function each |
| `src/projectsummer/cli.py` | Typer app generated from the registry |
| `src/projectsummer/mcp_server.py` | FastMCP 4 server generated from the registry |
| `src/projectsummer/core/db.py` | Connections, sessions, schema bootstrap |
| `src/projectsummer/core/schema.sql` | Every table, view and column description |
| `src/projectsummer/core/ingest.py` → `resolve.py` → `enrich.py` | Export → TMDB ids → metadata, credits, people |
| `src/projectsummer/core/credits.py` | Which cast/crew jobs are kept, and their roles |
| `src/projectsummer/core/sync.py` | RSS feed catch-up |
| `src/projectsummer/core/querying.py`, `catalog.py` | `query` and `describe_schema` |

## Rules the code depends on

**Plugins.** A feature is a function with `@plugin(...)`, full type hints and a
Google-style docstring — the `Args:` section becomes CLI help and MCP argument
descriptions. It must return a Pydantic model subclassing
`core.results.Result`, with a docstring under every field. The registry
refuses `dict`, `Any`, bare `list` or `dict[str, Any]` anywhere in a result.
Define result models at module level, above the function.

**Access and MCP exposure are enforced, not advisory.** `access="read"` plugins
run on a read-only DuckDB session over MCP; a plugin that writes the database,
writes files, or needs external access is `access="write"`. Read plugins are
exposed over MCP by default; write plugins only with `mcp=True`; `serves=True`
plugins never. Changing what is exposed must change the pinned lists in
`tests/test_registry.py::test_exactly_these_builtins_are_offered_over_mcp`
and `tests/test_mcp_server.py::EXPOSED`. `destructive`, `idempotent` and
`open_world` only become tool hints.

**Tool discovery.** The MCP tool list holds only `mcp_server.ALWAYS_LISTED`
(`describe_schema`, `overview`, `query`), every write tool, and FastMCP's
BM25 `search_tools` / `call_tool` pair; other read tools are found by search
(`tests/test_mcp_server.py::LISTED` pins this). Write tools stay listed and
our `call_tool` refuses them, because clients decide whether to ask the user
from a tool's own annotations. Search ranks on a tool's name, description and
parameter descriptions, so a new plugin's docstring should use the words a
question would.

**Arguments needed only sometimes.** Raise `errors.MissingArgumentError` with
the argument's name and a question (see `trends` needing `year` by month).
Over MCP the message tells the agent what to pass; the CLI asks at a terminal
and runs the command again.

**Connections.** The CLI uses one process-wide connection. The MCP server opens
a `db.session(...)` per tool call and closes it, because DuckDB lets no other
process open a file held read-write. Sessions don't nest, are per-thread
(ContextVar), and every MCP session passes `external_access=False` — DuckDB
quotes unconvertible values in error messages, so file access on any connection
reachable by an agent's SQL can leak files. Plugins never open connections
themselves; they call `db.get_connection()` / `db.query()`.

**Schema changes.** Everything in `schema.sql` is `CREATE ... IF NOT EXISTS`.
`db.init_schema` re-applies the file only when its SHA-256 fingerprint differs
from the one recorded in `sync_state`, then runs `CHECKPOINT`. Keep it that
way: DuckDB 1.5.5 cannot replay a `COMMENT ON COLUMN` from the write-ahead log,
so a process killed mid-open used to leave a log that stopped the library
opening at all (it happened; `DatabaseRecoveryError` now explains the
recovery). Never make routine opens write. So:
- A **new table** reaches existing databases automatically. Prefer this, and
  give a new table every column it will ever need up front.
- A **new or changed column on an existing table** does not. It needs a
  `SCHEMA_VERSION` bump in `db.py`, and with no migrations that means users
  rebuild — including about 20 minutes of Letterboxd resolution.
- There are deliberately **no foreign keys** (DuckDB rejects LIST-column updates
  on referenced rows in a transaction). Add dangling-reference checks to the
  `integrity_orphans` view instead.
- Every public table and column needs a `COMMENT ON` description (a test
  enforces it); agents read them through `describe_schema`. Register a new
  table in `catalog.PUBLIC_RELATIONS` or `INTERNAL_RELATIONS`.

**Bulk writes.** Load rows as one JSON string with `json_transform` (see
`enrich.store_credits`), not `executemany`, which converts and upserts a row at
a time: 50 films with credits took ~30s that way and take ~0.2s this way.

**TMDB.** All requests go through `TMDBClient._get`. It returns `None` only for
a 404, and callers record `None` as gone for good — so every other failure,
sustained rate limiting included, must raise. `fetch_all` runs requests
`WORKERS` (8) at a time; results are handled and written on the caller's
thread only, never in workers. Enrichment is resumable: films and people are
written in batches, and `pending_ids` / `pending_people` decide what is left.

**People.** `credits.py` is the single place that decides which credits are
kept (top 10 cast plus chosen crew jobs) and each one's `role`. The name lists
on `films` (`directors`, `writers`, ...) are built from the same extraction so
they cannot disagree with `film_credits`. Gender and birthdays have gaps,
especially for non-English crew; NULL means unknown.

**stdout is the MCP protocol channel.** Plugins must not print. Report progress
with `progress.track(...)` / `progress.note(...)`; the CLI renders it and the
server ignores it. A plugin that must ask questions as it runs (`rank`) uses
`asking.choose(...)` / `asking.tell(...)` the same way: the CLI answers with
keypresses at a terminal, and with no asker installed `choose` raises. Such a
plugin is CLI-only.

**Distributable.** No hardcoded paths or usernames; configuration comes from
env vars or `.env` via `config.py`. Never store the email from `profile.csv`.
Never commit `.env`, `*.duckdb` or Letterboxd exports (all gitignored).

## Tests

- No test touches the network. `tests/test_enrich.py` holds `FakeClient`,
  `tmdb_response()` and `person_response()`, shared by the sync tests; TMDB
  client retry behaviour is tested against a scripted session.
- `conn` / `empty_conn` fixtures give an in-memory database; anything using
  `db.session` needs a real file under `tmp_path`.
- `conftest.py` restores the plugin registry after every test, so a throwaway
  plugin can't leak into later CLI or MCP tests.
- Parallel fetching tests pin `enrich.WORKERS` where exact batch counts matter.

## Style

Match the surrounding code: docstrings and comments explain *why*, in full
sentences, with British spelling ("summarise", "serialisation"). Error messages
say what to do next. Commit subjects are imperative and short; bodies explain
the reasoning.

## Next up

Where the last session (2026-09-17) stopped. Nothing here is decided unless it
says so: raise the item and agree the approach before building. The work so
far goes step by step, with a plan approved first and a pause after each step.

1. **File the DuckDB bug.** Draft and verified reproduction in
   `docs/duckdb-wal-comment-on-column-replay.md`. Posting is public: the user
   posts it.
2. **Stop re-requesting films TMDB never had.** A film resolved but never
   stored because TMDB answered 404 is fetched again on every `enrich` and
   counted in `still_pending`. Stored films TMDB drops are already marked done
   via `film_credit_fetches`; these need a small record of their own, since
   they are not in `films`. Offered, not yet taken up.
3. **More analysis plugins** after `trends`: taste (highest- and
   lowest-rated directors, genres, decades, countries, and where you differ
   from TMDB's average) and list overlap.
   `trends` reports original languages as ISO codes; a code-to-name mapping
   would read better but needs a data source.
4. **Docs.** Options discussed, none chosen: a plugin-authoring guide and a
   troubleshooting page (`DatabaseRecoveryError`, `DatabaseBusyError`, TMDB
   token) now; a query cookbook whose SQL runs as tests, and MCP prompts
   ("Year in review"), alongside the analysis plugins; then slim the README.
