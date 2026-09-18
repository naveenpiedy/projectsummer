# Data model


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

