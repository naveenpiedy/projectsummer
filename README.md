# Project Summer

Query, analyse and explore your own Letterboxd data — from a CLI, or from an
LLM agent over MCP. Both paths call the same functions, so neither can drift
from the other.

> **Status: early development.** The pipeline works end to end — import,
> resolve, enrich, sync, build lists — and is covered by ~270 tests. The MCP server
> is not built yet, and most of the analysis plugins are still to come. See
> [Roadmap](#roadmap).

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
Attempted       1390
Enriched        1390
Diary entries   1114
Unmatched          0
```

That is the setup done. Now you can ask it things:

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

`ui` uses DuckDB's built-in `ui` extension, downloaded once on first use. It
serves until you press Ctrl+C. Its column explorer shows a histogram and
summary statistics for each column of a result, but it has no chart builder:
it profiles one column at a time rather than plotting one against another.

## How it works

Every feature is one decorated function:

```python
from projectsummer.core.registry import plugin

@plugin(category="discovery")
def random_watchlist_pick(genre: str | None = None) -> dict:
    """Pick a random film from your watchlist.

    Args:
        genre: Only consider films in this genre. Case-insensitive.
    """
```

The type hints *are* the schema. The CLI reads the signature to build a
command; the MCP server will read the same signature to build a tool. Nothing
is hand-written twice.

Parameter names become `--options`, type hints become validation, and the
docstring's `Args:` section becomes each option's help text. Add `--json` to
any command for the raw result instead of a table.

### Adding your own features

Drop a `.py` file containing decorated functions into a directory and point
`LETTERBOXD_PLUGIN_PATH` at it. They appear in the CLI alongside the built-ins
with no need to touch `core/`. A file that fails to import is reported and
skipped rather than taking the tool down with it.

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

Copy `.env.example` to `.env` and fill it in. A free TMDB account provides a
token at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
— take the long **Read Access Token**, not the short v3 key.

## Development

```bash
uv sync
uv run pytest
```

Tests never touch the network: HTTP is faked at the session boundary, so the
awkward cases — a redirect to a diary entry, a page with no ids, a timeout, an
interruption mid-run — are exercised deliberately rather than waited for.

## Roadmap

- [x] DuckDB schema and connection layer
- [x] Plugin registry with auto-discovery
- [x] CLI generated from the registry
- [x] CSV export ingestion, straight from the `.zip`
- [x] Letterboxd URI → TMDB id resolution
- [x] TMDB metadata enrichment
- [x] RSS polling to keep the library current
- [x] List builder: filters or SQL to a Letterboxd-importable list
- [ ] MCP server
- [ ] Plugins: trends, taste, list overlap, ranking

## Attribution and affiliation

Project Summer is an independent tool. It is **not affiliated with, endorsed
by, or connected to Letterboxd Limited**. "Letterboxd" is used here only to
describe what the tool reads.

This product uses the TMDB API but is **not endorsed or certified by TMDB**.

## License

[MIT](LICENSE)
