# Query cookbook

Recipes for `summer query`, DuckDB's web UI (`summer ui`), or an assistant
through the `query` tool. Every query on this page is run by the test suite
against a small library, so none of them can rot silently.

Run one from the terminal:

```bash
summer query "SELECT genre, count(*) AS films FROM (SELECT unnest(genres) AS genre FROM films WHERE watched) GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

Two habits worth keeping. **Ask for the answer, not the rows:** a count or an
average costs a few hundred tokens where a hundred rows of `SELECT *` costs
fifty thousand, and results are capped. **Check spellings before filtering:**
`summer describe-schema --table films --column genres` shows what a column
actually holds, and a wrong spelling matches nothing rather than erroring.

## Your viewing

### Films watched each year

```sql
SELECT year(watched_date) AS year,
       count(*)                AS viewings,
       count(DISTINCT tmdb_id) AS films
FROM diary_entries
WHERE watched_date IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

### The genres you watch most

A film counts once for each of its genres, so the numbers add up to more than
your film count.

```sql
SELECT genre, count(*) AS films
FROM (SELECT unnest(genres) AS genre FROM films WHERE watched)
GROUP BY 1
ORDER BY films DESC, genre
LIMIT 10;
```

### What you watch in each language

```sql
SELECT original_language       AS language,
       count(*)                AS films,
       round(avg(my_rating), 2) AS average_rating
FROM films
WHERE watched
GROUP BY 1
ORDER BY films DESC, language;
```

### Films you rewatch

`film_watch_stats` is derived from the diary, so it can never disagree with it.

```sql
SELECT f.title, s.watch_count, s.first_watched_date, s.last_watched_date
FROM film_watch_stats s
JOIN films f USING (tmdb_id)
WHERE s.watch_count > 1
ORDER BY s.watch_count DESC, f.title
LIMIT 10;
```

### The reviews you have written

```sql
SELECT d.watched_date, f.title, d.review
FROM diary_entries d
JOIN films f USING (tmdb_id)
WHERE d.review IS NOT NULL
ORDER BY d.watched_date DESC
LIMIT 10;
```

## Your ratings

### How you rate each decade

```sql
SELECT CAST(year // 10 * 10 AS VARCHAR) || 's' AS decade,
       count(*)                 AS films,
       round(avg(my_rating), 2) AS average_rating
FROM films
WHERE watched AND my_rating IS NOT NULL AND year IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

### Where you disagree with everyone else

TMDB rates out of 10 and you out of 5, so halve theirs to compare. The vote
count keeps out films only a handful of people have rated.

```sql
SELECT title,
       my_rating,
       round(tmdb_rating / 2, 2)             AS crowd_rating,
       round(my_rating - tmdb_rating / 2, 2) AS gap
FROM films
WHERE watched AND my_rating IS NOT NULL AND tmdb_vote_count >= 100
ORDER BY gap DESC
LIMIT 10;
```

Swap `DESC` for `ASC` to see the films you liked least of those everyone else
loved.

### Your five-star films, newest first

```sql
SELECT title, year, rated_on
FROM films
WHERE my_rating = 5
ORDER BY rated_on DESC NULLS LAST, title
LIMIT 20;
```

## People

Questions about people go through `film_credits` and `people`, never the name
lists on `films`: two people can share a name, and `person_id` keeps them
apart.

### The directors you rate highest

```sql
SELECT p.name,
       count(*)                   AS films,
       round(avg(f.my_rating), 2) AS average_rating
FROM film_credits c
JOIN people p USING (person_id)
JOIN films f USING (tmdb_id)
WHERE c.role = 'director' AND f.my_rating IS NOT NULL
GROUP BY p.person_id, p.name
HAVING count(*) >= 3
ORDER BY average_rating DESC, films DESC
LIMIT 10;
```

### Women directors in your library

`gender` is NULL where TMDB does not record it, which is commoner for crew and
for films outside English — so this leaves out anyone unrecorded rather than
counting them as men.

```sql
SELECT p.name,
       count(*)                   AS films,
       round(avg(f.my_rating), 2) AS average_rating
FROM film_credits c
JOIN people p USING (person_id)
JOIN films f USING (tmdb_id)
WHERE c.role = 'director' AND p.gender = 'female' AND f.watched
GROUP BY p.person_id, p.name
ORDER BY films DESC, p.name;
```

### The actors you have seen most

```sql
SELECT p.name, count(*) AS films
FROM film_credits c
JOIN people p USING (person_id)
JOIN films f USING (tmdb_id)
WHERE c.role = 'actor' AND f.watched
GROUP BY p.person_id, p.name
ORDER BY films DESC, p.name
LIMIT 10;
```

Only the ten leading cast of each film are stored, so this is "seen in a
leading role", not every appearance.

### Where the people you watch come from

`place_of_birth` is free text, so match part of it rather than testing for
equality.

```sql
SELECT p.place_of_birth, count(DISTINCT p.person_id) AS people
FROM people p
WHERE p.place_of_birth IS NOT NULL
GROUP BY 1
ORDER BY people DESC, 1
LIMIT 10;
```

### Everyone who both wrote and directed the same film

```sql
SELECT p.name, f.title
FROM film_credits c
JOIN people p USING (person_id)
JOIN films f USING (tmdb_id)
WHERE c.role IN ('director', 'writer')
GROUP BY p.person_id, p.name, f.title
HAVING count(DISTINCT c.role) = 2
ORDER BY p.name, f.title;
```

## Deciding what to watch

### The shortest things on your watchlist

```sql
SELECT title, year, runtime, genres
FROM films
WHERE on_watchlist AND NOT watched AND runtime IS NOT NULL
ORDER BY runtime
LIMIT 10;
```

### Watchlist films by a director you already like

```sql
SELECT f.title, f.year, p.name AS director
FROM films f
JOIN film_credits c USING (tmdb_id)
JOIN people p USING (person_id)
WHERE f.on_watchlist AND NOT f.watched AND c.role = 'director'
  AND p.person_id IN (
      SELECT c2.person_id
      FROM film_credits c2
      JOIN films f2 USING (tmdb_id)
      WHERE c2.role = 'director' AND f2.my_rating >= 4
  )
ORDER BY f.title;
```

### How much of each list you have seen

```sql
SELECT l.name,
       count(*)                                                  AS films,
       count(*) FILTER (WHERE f.watched)                         AS watched,
       round(100.0 * count(*) FILTER (WHERE f.watched) / count(*), 1) AS percent
FROM lists l
JOIN list_entries e USING (list_id)
JOIN films f ON f.tmdb_id = e.tmdb_id
GROUP BY l.list_id, l.name
ORDER BY percent DESC, l.name;
```

`summer list-overlap <slug>` answers this for one list, with the films.

### Keywords you keep coming back to

```sql
SELECT keyword, count(*) AS films
FROM (SELECT unnest(keywords) AS keyword FROM films WHERE watched)
GROUP BY 1
ORDER BY films DESC, keyword
LIMIT 15;
```

## Checking the library

### Anything left to fetch

```sql
SELECT (SELECT count(*) FROM films)                                 AS films,
       (SELECT count(*) FROM films WHERE enriched_at IS NULL)        AS unenriched,
       (SELECT count(*) FROM people WHERE details_fetched_at IS NULL) AS people_pending,
       (SELECT count(*) FROM list_entries WHERE tmdb_id IS NULL)      AS list_entries_unlinked;
```

A list entry with no `tmdb_id` was never resolved, so it cannot be compared
with anything; see [Troubleshooting](troubleshooting.md#lists).

### Dangling references

Empty is healthy. A row here means enrichment left something behind.

```sql
SELECT * FROM integrity_orphans;
```
