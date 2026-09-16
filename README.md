# Letterboxd Utility Tools

Query, analyse and explore your own Letterboxd data — from a CLI, or from an
LLM agent over MCP. Both paths call the same functions, so neither can drift
from the other.

> **Status: early development.** The plugin system, database layer and CLI
> work and are tested. Ingestion is not built yet, so there is currently no way
> to load your own data. See [Roadmap](#roadmap).

## How it works

Letterboxd's API is closed to personal projects, so data comes from the export
you can download from your account settings, topped up over time by polling
your public RSS feeds. Films are enriched with TMDB metadata and stored in a
single DuckDB file.

Every feature is one decorated function:

```python
from letterboxd_utility_tools.core.registry import plugin

@plugin(category="discovery")
def random_watchlist_pick(genre: str | None = None) -> dict:
    """Pick a random film from your watchlist."""
    ...
```

The type hints *are* the schema. The CLI reads the signature to build a
command; the MCP server reads the same signature to build a tool. Nothing is
hand-written twice.

That function becomes this, with no CLI code written for it:

```console
$ letterboxd random-watchlist-pick --genre horror
Title                  The Others
Year                   2001
Runtime                101
Genres                 Horror, Mystery
Directors              Alejandro Amenabar
Candidates considered  3
```

Parameter names become `--options`, type hints become validation, and the
docstring's `Args:` section becomes each option's help text. Add `--json` to
any command to get the raw result instead of a table.

## Adding your own features

Drop a `.py` file containing decorated functions into a directory, and point
`LETTERBOXD_PLUGIN_PATH` at it. They show up in the CLI and over MCP alongside
the built-ins — no need to touch `core/`.

## Configuration

All optional; none are needed to query a database that already exists.

| Variable | Purpose |
|---|---|
| `TMDB_API_KEY` | TMDB API read-access token, for metadata enrichment. |
| `LETTERBOXD_USERNAME` | Username for RSS polling. Defaults to your imported profile. |
| `LETTERBOXD_DB` | Database location. Defaults to a per-user data directory. |
| `LETTERBOXD_PLUGIN_PATH` | Extra directories to load plugins from. |

Copy `.env.example` to `.env` to set them.

## Data model

One wide `films` table — DuckDB `LIST` columns mean `list_contains(genres,
'Horror')` and `UNNEST(genres)` work directly, with no string splitting.

Watch history lives in a separate `diary_entries` table, one row per viewing,
because a film can be watched more than once and each viewing carries its own
date, rating and tags. Rolled-up counts come from the `film_watch_stats` view,
so they can never disagree with the entries underneath them.

Also stored: `lists` and `list_entries` (with positions, so ranked lists
survive), and a one-row `profile`. Your email address, which appears in
Letterboxd's `profile.csv`, is deliberately not stored.

One user per database file.

## Development

```
uv sync
uv run pytest
```

## Roadmap

- [x] DuckDB schema and connection layer
- [x] Plugin registry with auto-discovery
- [x] First plugin: random watchlist picker
- [x] CLI
- [ ] CSV export ingestion
- [ ] TMDB enrichment
- [ ] RSS polling
- [ ] MCP server
- [ ] Remaining plugins: trends, taste, lists, query

## License

MIT
