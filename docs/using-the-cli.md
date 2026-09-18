# Using the CLI

Every command takes `--json` for the raw result instead of a table, and
`summer --help` lists them all, grouped by category. The
[quickstart](../README.md#getting-started) covers importing your export;
this page is everything after that.

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

To see how much of a list you have seen, or what two lists share:

```bash
summer list-overlap comic-book-movies              # against what you have watched
summer list-overlap all-time-favorites --second well-made-movies
summer list-overlap tamil-movies --second watchlist
```

Either side can be a list's slug or one of `watched`, `watchlist`, `liked`
and `rated`. Only films resolved to a TMDB id count, so run `resolve` and
`enrich` after importing a new export.

To see what you rate highly and what you do not:

```bash
summer taste                          # every facet
summer taste --facet directors --top 5
```

Your highest- and lowest-rated directors, actors, genres, release decades,
original languages and production countries, each next to TMDB's average for
the same films, so you can see where you differ from the crowd. Directors and
actors are grouped by TMDB person id, so two people of one name stay apart. A
director, genre or decade needs at least 3 rated films to appear (`--min-films`)
and an actor at least 5 (`--actor-min-films`). It also lists the films you and
TMDB disagree about most, ignoring films with fewer than 100 TMDB votes.

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

