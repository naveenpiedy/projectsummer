# Project Summer

Query, analyse and explore your own Letterboxd data — from a CLI, or from an
LLM agent over MCP. Both paths call the same functions, so neither can drift
from the other.

> **Status: complete and in use.** Import, resolve, enrich with cast, crew
> and people, sync, lists, analysis and the MCP server all work end to end,
> covered by ~650 tests that never touch the network.

## Install

Needs Python 3.11+.

```bash
git clone https://github.com/naveenpiedy/projectsummer
cd projectsummer
uv sync
```

That installs a `summer` command. Without [uv](https://docs.astral.sh/uv/),
`pip install -e .` does the same.

You also need a free [TMDB](https://www.themoviedb.org/) API token — the long
**Read Access Token**, not the short v3 key — from
[themoviedb.org/settings/api](https://www.themoviedb.org/settings/api). Copy
`.env.example` to `.env` and put it there.

## Getting started

Download your data from Letterboxd (Settings → Data → Export Your Data), then
point Project Summer at the `.zip`. There is no need to unzip it.

```console
$ summer setup letterboxd-you-2026-09-16.zip
```

That runs the three stages in order: import the CSVs, look each film up to get
its TMDB id, then fetch metadata, cast, crew and the people behind them. For a
thousand-film library expect about forty minutes, most of it waiting on other
people's servers, and only once. Interrupting is safe — every stage writes as
it goes, and running it again carries on from where it stopped.

Then ask it things:

```console
$ summer random-watchlist-pick --genre horror
Title                  The Stepford Wives
Year                   1975
Runtime                117
Genres                 Thriller, Science Fiction, Horror
Directors              Bryan Forbes
Candidates considered  15
```

```console
$ summer query "SELECT year(watched_date) AS year, count(*) AS films
    FROM diary_entries GROUP BY 1 ORDER BY 1"
```

`summer sync` keeps it current afterwards, reading your public RSS feed rather
than the website.

## What it does

| | |
|---|---|
| `overview`, `describe-schema`, `query`, `ui` | See what is there, and ask it anything in SQL |
| `trends` | How your viewing changes, by year or by month |
| `taste` | What you rate highly, and where you differ from the crowd |
| `list-overlap` | How much of a list you have seen, or what two lists share |
| `rank` | Put films in order head to head, for fun |
| `list-builder` | Build a list from filters or SQL, importable into Letterboxd |
| `random-watchlist-pick` | Decide what to watch tonight |
| `ingest`, `resolve`, `enrich`, `sync`, `setup` | The pipeline |

[Using the CLI](docs/using-the-cli.md) has them in full, and
[the cookbook](docs/cookbook.md) has queries worth stealing.

## Using it from an AI assistant

`summer-mcp` serves your library to any [MCP](https://modelcontextprotocol.io/)
client — Claude Desktop, Claude Code, and others — so you can ask about your
films in plain language:

```bash
uv sync --extra mcp
claude mcp add projectsummer -- uv run --directory /path/to/projectsummer --extra mcp summer-mcp
```

What an assistant can do is enforced by the server rather than left to the
model: only the plugins marked for MCP exist at all, reading tools run on a
connection DuckDB keeps read-only, no tool can reach your files through the
database, and files are written only into the output folder. It also offers
prompts for whole questions — a year in review, what to watch tonight, a taste
profile. [Using it from an AI assistant](docs/mcp.md) has the setup for other
clients and the rules in full.

## Configuration

Only `TMDB_API_KEY` is required — by `enrich`, and by `sync` when your feed
contains a film you have never logged before.

| Variable | Purpose |
|---|---|
| `TMDB_API_KEY` | TMDB **API Read Access Token**, for film metadata. |
| `LETTERBOXD_USERNAME` | Username for RSS polling. Defaults to your imported profile. |
| `LETTERBOXD_DB` | Database location. Defaults to a per-user data directory. |
| `LETTERBOXD_PLUGIN_PATH` | Extra directories to load plugins from. |
| `LETTERBOXD_MCP_READ_ONLY` | Set to `true` for an MCP server with no tool that changes anything. |

## How it works

Every feature is one decorated function whose type hints *are* the schema. The
CLI reads the signature to build a command; the MCP server reads the same
signature to build a tool. Nothing is hand-written twice.

```python
@plugin(category="discovery")
def random_watchlist_pick(genre: str | None = None) -> WatchlistPick:
    """Pick a random film from your watchlist.

    Args:
        genre: Only consider films in this genre. Case-insensitive.
    """
```

Drop a file of these into a directory, point `LETTERBOXD_PLUGIN_PATH` at it,
and your own features appear in the CLI beside the built-ins, under the same
rules. [Writing a plugin](docs/writing-a-plugin.md) is the full guide.

## Documentation

- [Using the CLI](docs/using-the-cli.md) — sync, list building, exploring and
  analysing your library.
- [Query cookbook](docs/cookbook.md) — recipes for `query`, every one of them
  run by the test suite.
- [Using it from an AI assistant](docs/mcp.md) — setting up the MCP server,
  the tools and prompts it offers, and the boundary it enforces.
- [Writing a plugin](docs/writing-a-plugin.md) — what the registry enforces,
  access and MCP exposure, progress, questions, errors and testing.
- [Data model](docs/data-model.md) — the tables, and why they are shaped that
  way.
- [Troubleshooting](docs/troubleshooting.md) — a library that will not open, a
  missing TMDB token, empty list comparisons, an assistant that cannot see a
  tool.

## Development

```bash
uv sync
uv run pytest
```

`uv sync` includes the development dependencies, FastMCP among them, so the
MCP server's tests run too.

Tests never touch the network: HTTP is faked at the session boundary, so the
awkward cases — a redirect to a diary entry, a page with no ids, a timeout,
TMDB rate limiting, an interruption mid-run — are exercised deliberately rather
than waited for. The documentation is tested too: every query in the cookbook
and every example in the plugin guide is run.

[Contributing](CONTRIBUTING.md) has the rules the code depends on, and what to
run before opening a pull request. Security problems go through the private
route in [SECURITY.md](SECURITY.md) rather than the issue tracker.

## Roadmap

- [x] DuckDB schema and connection layer
- [x] Plugin registry with auto-discovery
- [x] CLI generated from the registry
- [x] CSV export ingestion, straight from the `.zip`
- [x] Letterboxd URI → TMDB id resolution
- [x] TMDB metadata enrichment
- [x] RSS polling to keep the library current
- [x] List builder: filters or SQL to a Letterboxd-importable list
- [x] MCP server, generated from the same registry
- [x] People and credits: gender, birthdays, roles across cast and crew
- [x] Search-based tool discovery, and prompts, over MCP
- [x] Trends by year and month
- [x] Head-to-head ranking
- [x] Taste: what you rate highly, against the crowd
- [x] List overlap: how much of a list you have seen

## Attribution and affiliation

Project Summer is an independent tool. It is **not affiliated with, endorsed
by, or connected to Letterboxd Limited**. "Letterboxd" is used here only to
describe what the tool reads.

This product uses the TMDB API but is **not endorsed or certified by TMDB**.

## License

[MIT](LICENSE)
