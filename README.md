# Project Summer

Query, analyse and explore your own Letterboxd data — from a CLI, or from an
LLM agent over MCP. Both paths call the same functions, so neither can drift
from the other.

> **Status: early development.** The pipeline works end to end — import,
> resolve, enrich with cast, crew and people, sync, build lists — and so does
> the MCP server, all covered by ~490 tests. Most of the analysis plugins are
> still to come. See [Roadmap](#roadmap).

## Install

Needs Python 3.11+.

```bash
git clone https://github.com/naveenpiedy/projectsummer
cd projectsummer
uv sync
```

That installs a `summer` command. Without [uv](https://docs.astral.sh/uv/),
`pip install -e .` does the same.

## Getting started

Download your data from Letterboxd (Settings → Data → Export Your Data). Point
Project Summer at the `.zip` — there is no need to unzip it.

```console
$ summer ingest letterboxd-you-2026-09-16.zip
Export dir     letterboxd-you-2026-09-16.zip
Films          1392
Diary entries  1114
Lists          22
List entries   461
Profile        you
```

The export identifies films only by `boxd.it` links — it contains no TMDB or
IMDb ids anywhere — so the next step looks each one up:

```console
$ summer resolve
```

This is **the only part of the tool that reads Letterboxd's website**, and it
is built to do so once per film, ever. Results are cached, so a re-import or a
later export resolves only genuinely new films. Requests are spaced half a
second apart and carry a User-Agent naming the tool. Expect roughly one film
per second, so about 20 minutes for a thousand-film library, once. It is safe
to interrupt — every result is written as it arrives, and re-running resumes.

Then fetch the metadata, which comes from [TMDB](https://www.themoviedb.org/)
and needs a free API token (see [Configuration](#configuration)):

```console
$ summer enrich
Attempted              1390
Enriched               1390
Diary entries          1114
Unmatched                 0
People fetched        14161
People not on tmdb        0
People pending            0
```

`enrich` works in two parts. First the films — genres, runtime, ratings, and
who made them. Then the people credited on them: birthday, birthplace and
other names, one request per person. A library of about 1,400 films credits
about 14,000 people, so the second part is by far the longer: roughly twenty
minutes, with requests made eight at a time. It happens once — later runs
fetch only people they have not seen — and it comes last and saves as it
goes, so interrupting it is safe and running `enrich` again carries on.

That is the setup done. To do all three in one go instead:

```console
$ summer setup letterboxd-you-2026-09-16.zip
```

It checks for a TMDB token before starting, and like each step it resumes if
interrupted: run it again with the same export.

Now you can ask it things:

```console
$ summer random-watchlist-pick --genre horror
Title                  The Stepford Wives
Year                   1975
Runtime                117
Genres                 Thriller, Science Fiction, Horror
Directors              Bryan Forbes
Candidates considered  15
```

## Keeping it current

Re-importing an export is not needed for day-to-day use:

```console
$ summer sync
Username         you
Feed items       50
New entries      2
Already known    48
```

This reads your public RSS feed, which publishes TMDB ids directly — so
unlike the first import it looks nothing up on Letterboxd's website. It covers
roughly your last fifty diary entries and reviews. Watchlist additions and
likes are not published in any feed, so those still come from a fresh export.
A film you have never logged before arrives with its cast and crew, and the
details of anyone new on it.

### Upgrading from a version without people

Libraries enriched before cast and crew were kept have films but no people.
Run `summer enrich` once: it fetches each film again for its credits, then
the people on them. `summer overview` shows how many films and people are
still waiting.

## Building lists

Build a list from your library and import it into Letterboxd:

```console
$ summer list-builder nolan.csv --director "Christopher Nolan" --status watched
$ summer list-builder aug.csv --watched-from 2026-08-01 --watched-to 2026-08-31 --order-by watched
$ summer list-builder duo.csv --actor "Kamal Haasan" --actor "Nagesh"
$ summer list-builder best-90s.csv --year-from 1990 --year-to 1999 --min-rating 4.5 --order-by rating
```

Filters are director, actor, keyword, genre, watched dates, release years,
your rating and status. Every filter must hold, including repeats: two
`--actor` options means films with both. Names match exactly and ignore case;
a near miss suggests the real name instead of silently returning nothing.

Or write the query yourself. It must be a single `SELECT` returning a
`tmdb_id` column, and its order becomes the list's order:

```console
$ summer list-builder overrated-by-me.csv --sql "SELECT tmdb_id FROM films
    WHERE watched AND tmdb_vote_count > 500 ORDER BY my_rating * 2 - tmdb_rating DESC LIMIT 25"
```

The query is checked by DuckDB's own parser before it runs, so anything other
than one `SELECT` — a `DELETE`, a second statement, a `COPY` — is refused.

On Letterboxd, create a new list and choose **Import**. Films are matched
exactly by TMDB id. The file carries only film identity, never ratings or
dates, because Letterboxd's list importer is also its diary importer and
importing a list should not be able to change your diary. Lists larger than
Letterboxd's 1MB limit are split into numbered files; import them into the
same list in order.

## Looking at the data

```bash
summer overview   # a summary in the terminal
summer lists      # your lists, and how big they are
summer ui         # DuckDB's own web UI: SQL notebook, table browser, column profiles
```

To write your own SQL, find your way around first. `describe-schema` works
one level at a time: the tables, then one table's columns, then what one
column actually holds:

```console
$ summer describe-schema
$ summer describe-schema --table films
$ summer describe-schema --table films --column genres
$ summer describe-schema --table films --column directors --search "ravi kumar"
```

That last level matters more than it looks. TMDB's genre is `Science Fiction`,
not `Sci-Fi`, and a filter on the wrong spelling returns nothing rather than
an error. `--search` finds a value by part of it, ignoring case, spacing and
small misspellings.

Then query:

```console
$ summer query "SELECT year(watched_date) AS year, count(*) AS films
    FROM diary_entries GROUP BY 1 ORDER BY 1"
```

`query` runs a single `SELECT` only, returns at most 100 rows by default
(`--max-rows` up to 1000) and about 20,000 characters of them, and stops
anything running past 30 seconds. The size limit keeps a wide `SELECT *` from
flooding an assistant's context; select the columns you need.

To see how your viewing has changed, by year or by month:

```bash
summer trends                       # one row per year of your diary
summer trends --by month --year 2025
summer trends --by month            # asks which year
```

Each period has viewings, different films, first watches and rewatches, your
average rating at the time, hours watched, and the genres, original languages
(as codes: `en`, `ta`), production countries and release decades you watched
most. `--top` sets how many of each, up to 20.

For fun, rank films head to head. Pick the better of two, again and again,
from the opening round to the final:

```bash
summer rank --from-list favourites
summer rank --sql "SELECT tmdb_id FROM films WHERE list_contains(directors, 'Christopher Nolan')"
summer rank --from-list favourites --mode bracket --save ranked.csv
```

Press `1` or `2` to pick, `u` to undo, `q` to stop. Up to 32 films, drawn in
a random order. The default ranks every film, in at most 129 choices for 32
and usually fewer; `--mode bracket` is a knockout that finds a champion in one
choice per film knocked out, and points out upsets against your own ratings.
`--save` writes the finished order as a list to import into Letterboxd.
Nothing is stored in the library. It needs a terminal, so it is not offered to
AI assistants.

`ui` uses DuckDB's built-in `ui` extension, downloaded once on first use. It
serves until you press Ctrl+C. Its column explorer shows a histogram and
summary statistics for each column of a result, but it has no chart builder:
it profiles one column at a time rather than plotting one against another.

## Using it from an AI assistant

`summer-mcp` serves your library to any [MCP](https://modelcontextprotocol.io/)
client — Claude Desktop, Claude Code, and others — so you can ask about your
films in plain language. Install the optional dependencies first:

```bash
uv sync --extra mcp
```

For Claude Code:

```bash
claude mcp add projectsummer -- uv run --directory /path/to/projectsummer --extra mcp summer-mcp
```

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "projectsummer": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/projectsummer", "--extra", "mcp", "summer-mcp"]
    }
  }
}
```

`--directory` also makes the server find your `.env`. Add `--db PATH` to
serve a library other than the default, or `--read-only` to offer no tool
that changes anything.

The assistant gets these tools:

| Tool | Does |
|---|---|
| `describe_schema` | Tables, then a table's columns, then what a column holds |
| `query` | One read-only `SELECT` |
| `overview`, `lists`, `random_watchlist_pick`, `trends` | The same as the commands |
| `sync` | Fetches your recent diary entries from Letterboxd's feed |
| `set_list_ranked` | Marks a list as ranked |
| `list_builder` | Writes an importable list into the output folder |

Importing, resolving and enriching stay with the CLI: they run for minutes,
and are yours to start.

Not every tool is listed up front. Each listed tool's description is sent
with every message, and the analyses will outnumber what a question usually
needs. So the assistant sees `describe_schema`, `query`, `overview` and every
tool that changes something, plus `search_tools` to find the rest by
describing what it wants and `call_tool` to run what it finds. Tools that
change something are never run through `call_tool`, so your client always
shows them by name before asking you to approve one.

Questions about people — "which women directors do I rate highest?" — work
through the same two tools: `describe_schema` points the assistant to the
`people` and `film_credits` tables.

What an assistant can do is enforced by the server, not left to the model:

- **Only these tools exist.** Anything else is never registered, so no prompt
  can reach it.
- **Reading tools cannot write.** They run on a database connection DuckDB
  itself keeps read-only, whatever SQL they are given.
- **No tool can reach your files through the database.** Every connection the
  server opens, writing ones included, has DuckDB's file access switched off.
  That matters beyond `query`: `list_builder` takes SQL too, and a query that
  reads a file could otherwise leak it, `.env` and all, through an error
  message.
- **Files go only into the output folder** (`output` in the per-user data
  directory). An absolute path, or one that climbs out, is refused.
- **Your own commands are never locked out.** The database is opened for each
  call and closed straight after, so `summer sync` works while your assistant
  is open.
- **Unexpected errors stay private.** A tool's own errors explain themselves;
  anything else is reported without tracebacks or file paths.

## How it works

Every feature is one decorated function:

```python
from projectsummer.core.registry import plugin
from projectsummer.core.results import Result

class WatchlistPick(Result):
    """A film picked from your watchlist."""

    title: str
    """The film's title."""

@plugin(category="discovery")
def random_watchlist_pick(genre: str | None = None) -> WatchlistPick:
    """Pick a random film from your watchlist.

    Args:
        genre: Only consider films in this genre. Case-insensitive.
    """
```

The type hints *are* the schema. The CLI reads the signature to build a
command; the MCP server reads the same signature to build a tool. Nothing is
hand-written twice.

Parameter names become `--options`, type hints become validation, and the
docstring's `Args:` section becomes each option's help text. Add `--json` to
any command for the raw result instead of a table.

Every plugin returns a [Pydantic](https://docs.pydantic.dev/) model, and the
docstring under each field describes it. Over MCP that model is the tool's
output schema, so the registry refuses a plugin whose result says nothing
specific — no `dict`, no `Any`, no bare `list`.

A plugin also declares what it may do. `access="read"` (the default) or
`access="write"`; read plugins are offered over MCP, write plugins only with
`mcp=True`, and a plugin that starts a server never. Over MCP a read plugin
runs on a connection DuckDB will not let write to the database or touch the
filesystem, so the label is enforced rather than trusted. `destructive`,
`idempotent` and `open_world` describe a plugin further; clients may use them
to decide whether to ask before calling it.

### Adding your own features

Drop a `.py` file containing decorated functions into a directory and point
`LETTERBOXD_PLUGIN_PATH` at it. They appear in the CLI alongside the built-ins
with no need to touch `core/`, and in the MCP server under the same rules as
the built-ins. A file that fails to import is reported and skipped rather than
taking the tool down with it.

A plugin is ordinary Python running inside the tool, so only add plugins you
trust: the read-only connection limits what a read plugin can do *through the
database*, not what its own code can do.

## Data model

One wide `films` table. DuckDB `LIST` columns mean `list_contains(genres,
'Horror')` and `UNNEST(genres)` work directly, with no string splitting:

```sql
SELECT genre, count(*) FROM (SELECT unnest(genres) AS genre FROM films WHERE watched)
GROUP BY 1 ORDER BY 2 DESC;
```

Watch history lives in a separate `diary_entries` table, one row per viewing,
because a film can be watched more than once and each viewing carries its own
date, rating and tags. That is what makes viewing streaks, rating drift and
year-in-review answerable at all. Rolled-up counts come from the
`film_watch_stats` view, so they can never disagree with the rows beneath them.

People have tables of their own. `people` holds one row per person, keyed
by TMDB's person id — gender, birthday, birthplace, other names — and
`film_credits` holds one row per part a person played in a film, under a
plain role such as `director`, `writer`, `composer` or `actor`. A name alone
cannot do this: a person's birthday belongs to them rather than to each
film, and two people can share a name.

```sql
SELECT p.name, count(*) AS films, round(avg(f.my_rating), 2) AS avg_rating
FROM film_credits c
JOIN people p USING (person_id)
JOIN films f USING (tmdb_id)
WHERE c.role = 'director' AND p.gender = 'female' AND f.watched
GROUP BY p.person_id, p.name ORDER BY films DESC;
```

Only the ten leading cast and a chosen set of crew roles are kept. Gender,
birthday and birthplace have gaps, thicker for crew and for films outside
English, and a missing value means unknown — so a filter on them quietly
leaves those people out. The name lists on `films` (`directors`, `writers`,
...) stay as the quick path, built from the same credits.

Also stored: `lists` and `list_entries` (with positions, so ranked lists
survive a round trip), a one-row `profile`, and `film_identity` — the resolution
cache that keeps Letterboxd lookups down to one per film.

Your email address, which appears in Letterboxd's `profile.csv`, is
deliberately not stored. One user per database file.

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

Copy `.env.example` to `.env` and fill it in. A free TMDB account provides a
token at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
— take the long **Read Access Token**, not the short v3 key.

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
than waited for.

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
- [x] Search-based tool discovery over MCP
- [x] Trends by year and month
- [x] Head-to-head ranking
- [ ] Plugins: taste, list overlap

## Attribution and affiliation

Project Summer is an independent tool. It is **not affiliated with, endorsed
by, or connected to Letterboxd Limited**. "Letterboxd" is used here only to
describe what the tool reads.

This product uses the TMDB API but is **not endorsed or certified by TMDB**.

## License

[MIT](LICENSE)
