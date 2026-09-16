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
