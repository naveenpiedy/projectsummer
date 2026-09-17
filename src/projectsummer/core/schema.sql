-- Letterboxd Utility Tools V2 -- DuckDB schema.
--
-- Design notes:
--   * `films` stays deliberately wide. Multi-value fields use DuckDB's native
--     LIST type rather than the pipe-separated strings of the previous version,
--     so `list_contains(genres, 'Horror')` and `UNNEST(genres)` work directly.
--   * `diary_entries` is the one concession to a second table: a film's watch
--     history is genuinely one-to-many, and streaks / rating drift / year-in-
--     review cannot be computed from collapsed first/last dates.
--   * All DDL is idempotent so `init_schema()` is safe to call on every open.
--   * There are deliberately NO foreign keys. DuckDB 1.5 rejects updating a
--     LIST column on a row that a foreign key references, when inside a
--     transaction -- nested-type updates are implemented as delete-and-
--     reinsert, which trips the constraint. Since `films` is the parent of
--     everything AND is built out of LIST columns, enrichment refreshing a
--     film's genres would fail as soon as any diary entry referenced it.
--     Scalar and DATE updates are fine; VARCHAR[] updates are not. The
--     `integrity_orphans` view below reports what the constraints would have
--     prevented, and the test suite asserts against it.

-- ============================================================ films

CREATE TABLE IF NOT EXISTS films (
    -- Identity
    tmdb_id              BIGINT PRIMARY KEY,
    imdb_id              VARCHAR,
    letterboxd_uri       VARCHAR,
    letterboxd_slug      VARCHAR,

    -- Basic info
    title                VARCHAR NOT NULL,
    original_title       VARCHAR,
    year                 INTEGER,
    release_date         DATE,
    runtime              INTEGER,          -- minutes
    overview             VARCHAR,
    tagline              VARCHAR,
    poster_path          VARCHAR,

    -- People (TMDB credits)
    directors            VARCHAR[],
    cast_members         VARCHAR[],        -- `cast` is a reserved word in SQL
    writers              VARCHAR[],
    composers            VARCHAR[],
    cinematographers     VARCHAR[],
    editors              VARCHAR[],

    -- Categories
    genres               VARCHAR[],
    keywords             VARCHAR[],

    -- Production
    original_language    VARCHAR,
    spoken_languages     VARCHAR[],
    production_companies VARCHAR[],
    production_countries VARCHAR[],
    budget               BIGINT,
    revenue              BIGINT,
    status               VARCHAR,          -- 'Released', 'Post Production', ...

    -- TMDB crowd ratings (the comparison point for contrarian picks)
    tmdb_rating          DOUBLE,
    tmdb_vote_count      INTEGER,
    tmdb_popularity      DOUBLE,

    -- Per-film user state. One user per database, so no user_id.
    -- Watch *history* lives in diary_entries; these are the standing facts.
    my_rating            DOUBLE,           -- current rating, from ratings.csv
    rated_on             DATE,
    watched              BOOLEAN NOT NULL DEFAULT FALSE,
    watched_logged_on    DATE,             -- when it was logged, not when seen
    on_watchlist         BOOLEAN NOT NULL DEFAULT FALSE,
    watchlist_added_on   DATE,
    liked                BOOLEAN NOT NULL DEFAULT FALSE,
    liked_on             DATE,

    -- Bookkeeping
    enriched_at          TIMESTAMP,        -- last successful TMDB fetch
    created_at           TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at           TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ============================================================ diary_entries

CREATE SEQUENCE IF NOT EXISTS diary_entry_id_seq;

CREATE TABLE IF NOT EXISTS diary_entries (
    entry_id       BIGINT PRIMARY KEY DEFAULT nextval('diary_entry_id_seq'),
    tmdb_id        BIGINT NOT NULL,
    watched_date   DATE,                   -- the day you saw it
    logged_date    DATE,                   -- the day you logged it
    rating         DOUBLE,                 -- rating *at the time of this watch*
    rewatch        BOOLEAN NOT NULL DEFAULT FALSE,
    tags           VARCHAR[],
    review         VARCHAR,
    letterboxd_uri VARCHAR
);

-- Natural key for the watch. Makes RSS dedup an exact test rather than the
-- old "is this date inside the first..last range?" heuristic.
CREATE UNIQUE INDEX IF NOT EXISTS diary_entries_natural_key
    ON diary_entries (tmdb_id, watched_date, logged_date);

CREATE INDEX IF NOT EXISTS diary_entries_tmdb_id ON diary_entries (tmdb_id);
CREATE INDEX IF NOT EXISTS diary_entries_watched_date ON diary_entries (watched_date);

-- ============================================================ lists

CREATE SEQUENCE IF NOT EXISTS list_id_seq;

CREATE TABLE IF NOT EXISTS lists (
    list_id      BIGINT PRIMARY KEY DEFAULT nextval('list_id_seq'),
    slug         VARCHAR NOT NULL UNIQUE,  -- stable across re-exports
    name         VARCHAR NOT NULL,
    description  VARCHAR,
    tags         VARCHAR[],
    url          VARCHAR,
    created_date DATE,
    ranked       BOOLEAN NOT NULL DEFAULT FALSE,
    -- 'letterboxd' for your own exported lists, 'imported' for canonical ones
    -- (AFI Top 100, a friend's ranked list) to compare completion against.
    source       VARCHAR NOT NULL DEFAULT 'letterboxd'
);

CREATE TABLE IF NOT EXISTS list_entries (
    list_id         BIGINT  NOT NULL,
    entry_position  INTEGER NOT NULL,      -- 1-based order within the list
    tmdb_id         BIGINT,                -- NULL until resolved
    name            VARCHAR NOT NULL,      -- raw title, so unresolved rows survive
    year            INTEGER,
    letterboxd_uri  VARCHAR,
    description     VARCHAR,
    PRIMARY KEY (list_id, entry_position)
);

CREATE INDEX IF NOT EXISTS list_entries_tmdb_id ON list_entries (tmdb_id);

-- ============================================================ profile

-- Exactly one row: whose library this database holds.
-- Note: the email address from profile.csv is deliberately NOT stored.
CREATE TABLE IF NOT EXISTS profile (
    username           VARCHAR PRIMARY KEY,
    date_joined        DATE,
    given_name         VARCHAR,
    family_name        VARCHAR,
    location           VARCHAR,
    website            VARCHAR,
    bio                VARCHAR,
    pronoun            VARCHAR,
    favorite_film_uris VARCHAR[]
);

-- ============================================================ sync_state

-- Small key/value store: schema version, last RSS poll per feed, etc.
CREATE TABLE IF NOT EXISTS sync_state (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR,
    updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ============================================================ staging

-- Letterboxd exports identify films only by a boxd.it short link, while
-- `films` is keyed by tmdb_id. Ingestion therefore lands here first, keyed by
-- URI and needing no network, and enrichment resolves these into `films`.
-- Keeping the tables makes the database self-contained: a failed or partial
-- enrichment can resume without the original export folder.

-- One row per distinct FILM uri, merged from watched / watchlist / ratings /
-- likes. Those four files share a URI namespace, so they join cleanly.
CREATE TABLE IF NOT EXISTS staging_films (
    letterboxd_uri     VARCHAR PRIMARY KEY,
    name               VARCHAR NOT NULL,
    year               INTEGER,
    watched            BOOLEAN NOT NULL DEFAULT FALSE,
    watched_logged_on  DATE,
    on_watchlist       BOOLEAN NOT NULL DEFAULT FALSE,
    watchlist_added_on DATE,
    liked              BOOLEAN NOT NULL DEFAULT FALSE,
    liked_on           DATE,
    my_rating          DOUBLE,
    rated_on           DATE,
    imported_at        TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- One row per diary entry. Note these carry *entry* URIs, one per viewing,
-- which do not match the film URIs above -- diary and reviews live in a
-- separate URI namespace. They are matched to films on (name, year), with
-- URI resolution as the fallback.
CREATE TABLE IF NOT EXISTS staging_diary (
    entry_uri    VARCHAR PRIMARY KEY,
    name         VARCHAR NOT NULL,
    year         INTEGER,
    watched_date DATE,
    logged_date  DATE,
    rating       DOUBLE,
    rewatch      BOOLEAN NOT NULL DEFAULT FALSE,
    tags         VARCHAR[],
    review       VARCHAR,
    imported_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ============================================================ film_identity

-- Letterboxd URI -> slug -> TMDB/IMDb, the expensive part of enrichment.
-- Lives in the database rather than a sidecar JSON file, so it survives and
-- is reusable across re-exports. Slugs are the real identity: several URIs
-- (short link, full link, a diary entry) can resolve to the same film.
CREATE TABLE IF NOT EXISTS film_identity (
    letterboxd_uri  VARCHAR PRIMARY KEY,
    letterboxd_slug VARCHAR,
    tmdb_id         BIGINT,
    imdb_id         VARCHAR,
    resolved_at     TIMESTAMP,
    -- Last failure, so a retry can target only what actually broke.
    error           VARCHAR
);

CREATE INDEX IF NOT EXISTS film_identity_slug ON film_identity (letterboxd_slug);

-- ============================================================ views

-- Dangling references, which foreign keys would have rejected outright had
-- DuckDB allowed them here. Empty is healthy. A row appearing in this view
-- means enrichment left something unresolved, or a merge ran out of order.
CREATE OR REPLACE VIEW integrity_orphans AS
SELECT 'diary_entries' AS source_table, 'tmdb_id' AS source_column,
       d.tmdb_id AS missing_value, count(*) AS affected_rows
FROM diary_entries d
LEFT JOIN films f ON f.tmdb_id = d.tmdb_id
WHERE f.tmdb_id IS NULL
GROUP BY 1, 2, 3
UNION ALL
SELECT 'list_entries', 'list_id', e.list_id, count(*)
FROM list_entries e
LEFT JOIN lists l ON l.list_id = e.list_id
WHERE l.list_id IS NULL
GROUP BY 1, 2, 3
UNION ALL
SELECT 'list_entries', 'tmdb_id', e.tmdb_id, count(*)
FROM list_entries e
LEFT JOIN films f ON f.tmdb_id = e.tmdb_id
WHERE e.tmdb_id IS NOT NULL AND f.tmdb_id IS NULL
GROUP BY 1, 2, 3;

-- Watch history rolled up per film. Derived, never written to -- the old
-- schema's watch_count / first_watched_date / last_watched_date, but always
-- consistent with the underlying diary entries.
CREATE OR REPLACE VIEW film_watch_stats AS
SELECT
    f.tmdb_id,
    count(d.entry_id)                             AS watch_count,
    min(d.watched_date)                           AS first_watched_date,
    max(d.watched_date)                           AS last_watched_date,
    count(d.entry_id) FILTER (d.rewatch)          AS rewatch_count
FROM films f
LEFT JOIN diary_entries d ON d.tmdb_id = f.tmdb_id
GROUP BY f.tmdb_id;

-- ============================================================ descriptions
-- What each table and column means, for whoever queries the library: a
-- person in `summer ui`, or an agent through `describe_schema`. Stored in the
-- database with COMMENT ON, so the descriptions sit beside the schema they
-- describe and are visible to any DuckDB client. Re-applied on every
-- read-write open, like the rest of this file, so an existing database picks
-- up new wording without a schema version bump.

COMMENT ON TABLE films IS 'One row per film in your library: TMDB metadata plus your standing state (rating, watched, watchlist, liked). Keyed by tmdb_id. Viewing history is in diary_entries; watch counts are in film_watch_stats.';
COMMENT ON COLUMN films.tmdb_id IS 'TMDB id. Primary key; joins to diary_entries, list_entries and film_watch_stats.';
COMMENT ON COLUMN films.imdb_id IS 'IMDb id, e.g. tt0266697, when TMDB has one.';
COMMENT ON COLUMN films.letterboxd_uri IS 'The film''s Letterboxd link.';
COMMENT ON COLUMN films.letterboxd_slug IS 'The film''s Letterboxd slug, e.g. kill-bill-vol-1.';
COMMENT ON COLUMN films.title IS 'Title as TMDB gives it, usually the English release title.';
COMMENT ON COLUMN films.original_title IS 'Title in the original language.';
COMMENT ON COLUMN films.year IS 'Release year.';
COMMENT ON COLUMN films.release_date IS 'Original release date.';
COMMENT ON COLUMN films.runtime IS 'Running time in minutes.';
COMMENT ON COLUMN films.overview IS 'TMDB plot summary.';
COMMENT ON COLUMN films.tagline IS 'TMDB tagline.';
COMMENT ON COLUMN films.poster_path IS 'TMDB poster path; prefix https://image.tmdb.org/t/p/w500 to view it.';
COMMENT ON COLUMN films.directors IS 'Directors as TMDB credits them. A list, NULL when unknown: filter with list_contains(directors, ''Name''), which needs the exact spelling.';
COMMENT ON COLUMN films.cast_members IS 'Up to ten leading cast members, in billing order. A list. (Not called cast, which is a SQL keyword.)';
COMMENT ON COLUMN films.writers IS 'Writers, screenplay and story credits. A list.';
COMMENT ON COLUMN films.composers IS 'Composers of the original music. A list.';
COMMENT ON COLUMN films.cinematographers IS 'Directors of photography. A list.';
COMMENT ON COLUMN films.editors IS 'Editors. A list.';
COMMENT ON COLUMN films.genres IS 'TMDB genres, e.g. ''Science Fiction'' (not ''Sci-Fi''). A list: list_contains(genres, ''Horror''), or unnest(genres) to count by genre.';
COMMENT ON COLUMN films.keywords IS 'TMDB keywords, lower case, e.g. ''time travel''. A list.';
COMMENT ON COLUMN films.original_language IS 'ISO 639-1 code of the original language, e.g. en, ta, ja.';
COMMENT ON COLUMN films.spoken_languages IS 'Languages spoken, by English name, e.g. ''Japanese''. A list.';
COMMENT ON COLUMN films.production_companies IS 'Production companies. A list.';
COMMENT ON COLUMN films.production_countries IS 'Production countries by name, e.g. ''United States of America''. A list.';
COMMENT ON COLUMN films.budget IS 'Budget in US dollars. NULL when TMDB does not know, which is common.';
COMMENT ON COLUMN films.revenue IS 'Box office revenue in US dollars. NULL when TMDB does not know.';
COMMENT ON COLUMN films.status IS 'Release status, e.g. Released, Post Production.';
COMMENT ON COLUMN films.tmdb_rating IS 'TMDB average user rating, 0 to 10. Unreliable when tmdb_vote_count is small.';
COMMENT ON COLUMN films.tmdb_vote_count IS 'How many TMDB users rated the film.';
COMMENT ON COLUMN films.tmdb_popularity IS 'TMDB popularity score when enriched. Relative, and changes daily.';
COMMENT ON COLUMN films.my_rating IS 'Your current rating, 0.5 to 5 in half-star steps; NULL if unrated. Your rating at each viewing is diary_entries.rating.';
COMMENT ON COLUMN films.rated_on IS 'When you last set your rating.';
COMMENT ON COLUMN films.watched IS 'Whether you have marked the film watched, with or without a diary entry.';
COMMENT ON COLUMN films.watched_logged_on IS 'When the film was marked watched. Not when you saw it: that is diary_entries.watched_date.';
COMMENT ON COLUMN films.on_watchlist IS 'Whether the film is on your watchlist. A film can be on it and watched.';
COMMENT ON COLUMN films.watchlist_added_on IS 'When the film was added to your watchlist.';
COMMENT ON COLUMN films.liked IS 'Whether you have liked the film.';
COMMENT ON COLUMN films.liked_on IS 'When you liked the film.';
COMMENT ON COLUMN films.enriched_at IS 'When TMDB metadata was last fetched.';
COMMENT ON COLUMN films.created_at IS 'When the row was created. Bookkeeping.';
COMMENT ON COLUMN films.updated_at IS 'When the row last changed. Bookkeeping.';

COMMENT ON TABLE diary_entries IS 'Your viewing history: one row per logged viewing, so a rewatched film has several rows. Joins to films on tmdb_id. Use watched_date for when you watched.';
COMMENT ON COLUMN diary_entries.entry_id IS 'Row id, with no meaning of its own.';
COMMENT ON COLUMN diary_entries.tmdb_id IS 'The film watched. Joins to films.tmdb_id.';
COMMENT ON COLUMN diary_entries.watched_date IS 'The day you watched it.';
COMMENT ON COLUMN diary_entries.logged_date IS 'The day you logged it on Letterboxd, which can be later than watched_date.';
COMMENT ON COLUMN diary_entries.rating IS 'Your rating at this viewing, 0.5 to 5; NULL if not rated then. Can differ from films.my_rating.';
COMMENT ON COLUMN diary_entries.rewatch IS 'Whether you marked this viewing as a rewatch.';
COMMENT ON COLUMN diary_entries.tags IS 'Your tags on this entry, often where you watched, e.g. ''prime''. A list.';
COMMENT ON COLUMN diary_entries.review IS 'Review written with this entry, if any.';
COMMENT ON COLUMN diary_entries.letterboxd_uri IS 'Link to this diary entry on Letterboxd.';

COMMENT ON TABLE lists IS 'Your Letterboxd lists, plus any imported to compare against. Their films are in list_entries.';
COMMENT ON COLUMN lists.list_id IS 'Row id. Joins to list_entries.list_id.';
COMMENT ON COLUMN lists.slug IS 'Stable identifier, from the export filename, e.g. all-time-favorites.';
COMMENT ON COLUMN lists.name IS 'The list''s title.';
COMMENT ON COLUMN lists.description IS 'The list''s description.';
COMMENT ON COLUMN lists.tags IS 'The list''s tags. A list.';
COMMENT ON COLUMN lists.url IS 'The list on Letterboxd.';
COMMENT ON COLUMN lists.created_date IS 'When the list was created.';
COMMENT ON COLUMN lists.ranked IS 'Whether the order is a deliberate ranking. Letterboxd does not export this, so it is set by hand: false can mean not yet marked.';
COMMENT ON COLUMN lists.source IS '''letterboxd'' for your own lists, ''imported'' for lists added to compare against.';

COMMENT ON TABLE list_entries IS 'The films in each list, in list order. Join lists on list_id and films on tmdb_id.';
COMMENT ON COLUMN list_entries.list_id IS 'The list. Joins to lists.list_id.';
COMMENT ON COLUMN list_entries.entry_position IS 'Position in the list, from 1. Meaningful as a ranking only when lists.ranked.';
COMMENT ON COLUMN list_entries.tmdb_id IS 'The film. Joins to films.tmdb_id; NULL if it could not be identified.';
COMMENT ON COLUMN list_entries.name IS 'The film''s title as the export gave it.';
COMMENT ON COLUMN list_entries.year IS 'The film''s year as the export gave it.';
COMMENT ON COLUMN list_entries.letterboxd_uri IS 'The film''s Letterboxd link.';
COMMENT ON COLUMN list_entries.description IS 'Your note on this film within the list.';

COMMENT ON TABLE profile IS 'Whose library this is: one row from the Letterboxd profile. The email address is deliberately not stored.';
COMMENT ON COLUMN profile.username IS 'Letterboxd username.';
COMMENT ON COLUMN profile.date_joined IS 'When the account was created.';
COMMENT ON COLUMN profile.given_name IS 'Given name, as set on the profile.';
COMMENT ON COLUMN profile.family_name IS 'Family name, as set on the profile.';
COMMENT ON COLUMN profile.location IS 'Location, as set on the profile.';
COMMENT ON COLUMN profile.website IS 'Website, as set on the profile.';
COMMENT ON COLUMN profile.bio IS 'Profile bio.';
COMMENT ON COLUMN profile.pronoun IS 'Pronouns, as set on the profile.';
COMMENT ON COLUMN profile.favorite_film_uris IS 'Letterboxd links to the favourite films shown on the profile. A list.';

COMMENT ON VIEW film_watch_stats IS 'Per-film viewing totals, derived from diary_entries so they always agree with it. One row per film in films.';
COMMENT ON COLUMN film_watch_stats.tmdb_id IS 'The film. Joins to films.tmdb_id.';
COMMENT ON COLUMN film_watch_stats.watch_count IS 'Diary entries for the film. 0 for a film marked watched but never logged.';
COMMENT ON COLUMN film_watch_stats.first_watched_date IS 'Earliest watched_date in the diary.';
COMMENT ON COLUMN film_watch_stats.last_watched_date IS 'Latest watched_date in the diary.';
COMMENT ON COLUMN film_watch_stats.rewatch_count IS 'Diary entries marked as rewatches.';

COMMENT ON TABLE staging_films IS 'Internal: film data from the last imported export, before enrichment. Query films instead.';
COMMENT ON TABLE staging_diary IS 'Internal: diary rows from the last imported export, before enrichment. Query diary_entries instead.';
COMMENT ON TABLE film_identity IS 'Internal: cache of Letterboxd link to TMDB id lookups, so each film is looked up once.';
COMMENT ON TABLE sync_state IS 'Internal: schema version and when the RSS feed was last read.';
COMMENT ON VIEW integrity_orphans IS 'Internal: references to rows that do not exist. Empty when the library is healthy.'; 
