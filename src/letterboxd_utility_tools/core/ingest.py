"""Load a Letterboxd CSV export into the database.

This step needs no network. Letterboxd identifies films only by a `boxd.it`
short link, while `films` is keyed by `tmdb_id`, so everything lands in the
staging tables first and `enrich` resolves it from there.

Two things about the export shape drive the design:

* **`watched`, `ratings`, `watchlist` and `likes` share a URI namespace**, one
  URI per film, so they merge into `staging_films` with a plain group-by.
* **`diary` and `reviews` do not.** Their URIs identify a *viewing*, not a
  film, so a URI join between diary and watched matches nothing at all. They
  land in `staging_diary` keyed by entry URI, to be matched to films later.

Lists need a hand-written parser: their CSVs hold two tables in one file and
DuckDB's sniffer rejects them outright.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from letterboxd_utility_tools.core import db
from letterboxd_utility_tools.core.errors import LetterboxdError

#: Film-scoped exports: (source label, path within the export, has a Rating column).
_FILM_SOURCES: tuple[tuple[str, str, bool], ...] = (
    ("watched", "watched.csv", False),
    ("watchlist", "watchlist.csv", False),
    ("liked", "likes/films.csv", False),
    ("rating", "ratings.csv", True),
)

#: Separator Letterboxd uses inside its multi-value CSV fields.
_TAG_SPLIT = ","

#: SQL that strips carriage returns from a text column. Letterboxd exports are
#: CRLF throughout, so a review or description spanning lines arrives with
#: \r\n embedded in the value itself. Left alone, those carriage returns leak
#: into every rendered table, JSON payload and LLM prompt downstream.
_STRIP_CR = "replace({column}, chr(13), '')"


class ExportNotFoundError(LetterboxdError):
    """The given directory does not look like a Letterboxd export."""


@dataclass(frozen=True, slots=True)
class ListExport:
    """One parsed list CSV: its metadata and its entries."""

    slug: str
    name: str
    description: str | None
    tags: list[str]
    url: str | None
    created_date: str | None
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What an ingest run actually loaded."""

    export_dir: str
    films: int
    diary_entries: int
    lists: int
    list_entries: int
    profile: str | None
    skipped: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "export_dir": self.export_dir,
            "films": self.films,
            "diary_entries": self.diary_entries,
            "lists": self.lists,
            "list_entries": self.list_entries,
            "profile": self.profile,
            "skipped": list(self.skipped),
        }


# ------------------------------------------------------------------- public

def ingest_export(export_dir: str | Path) -> IngestReport:
    """Load every CSV in a Letterboxd export into the database.

    Staging tables and Letterboxd-sourced lists are replaced wholesale, so
    re-running against a newer export drops entries you have since removed
    rather than leaving them behind. Lists imported from elsewhere (AFI Top
    100, a friend's ranked list) are left alone.

    Args:
        export_dir: The unzipped export directory, the one containing
            `diary.csv` and `watchlist.csv`.

    Returns:
        An :class:`IngestReport` counting what was loaded.

    Raises:
        ExportNotFoundError: If the directory is missing or holds no
            recognisable Letterboxd CSVs.
    """
    directory = Path(export_dir).expanduser()
    _check_export(directory)

    conn = db.get_connection()
    skipped: list[str] = []

    # Deliberately before the transaction below -- see the function's docstring.
    _remove_stale_lists(conn, directory)

    conn.execute("BEGIN TRANSACTION")
    try:
        films = _ingest_film_sources(conn, directory, skipped)
        diary = _ingest_diary(conn, directory, skipped)
        list_count, entry_count = _ingest_lists(conn, directory, skipped)
        profile = _ingest_profile(conn, directory, skipped)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return IngestReport(
        export_dir=str(directory),
        films=films,
        diary_entries=diary,
        lists=list_count,
        list_entries=entry_count,
        profile=profile,
        skipped=tuple(skipped),
    )


def _check_export(directory: Path) -> None:
    if not directory.is_dir():
        raise ExportNotFoundError(f"{directory} is not a directory.")

    expected = [name for _, name, _ in _FILM_SOURCES] + ["diary.csv"]
    if not any((directory / name).exists() for name in expected):
        raise ExportNotFoundError(
            f"{directory} contains no Letterboxd CSVs. Expected to find at "
            f"least one of: {', '.join(expected)}. Point this at the unzipped "
            f"export directory, the one holding diary.csv."
        )


# ------------------------------------------------------------ film sources

def _ingest_film_sources(
    conn: duckdb.DuckDBPyConnection, directory: Path, skipped: list[str]
) -> int:
    """Merge the four film-scoped CSVs into `staging_films`, one row per film."""
    selects: list[str] = []
    params: list[str] = []

    for source, filename, has_rating in _FILM_SOURCES:
        path = directory / filename
        if not path.exists():
            skipped.append(f"{filename} (not present)")
            continue
        rating = '"Rating"' if has_rating else "NULL::DOUBLE"
        selects.append(
            f"""
            SELECT "Letterboxd URI" AS uri, "Name" AS name, "Year" AS year,
                   "Date" AS on_date, {rating} AS rating, '{source}' AS source
            FROM read_csv_auto(?)
            """
        )
        params.append(str(path))

    if not selects:
        return 0

    conn.execute("DELETE FROM staging_films")
    conn.execute(
        f"""
        INSERT INTO staging_films (
            letterboxd_uri, name, year,
            watched, watched_logged_on,
            on_watchlist, watchlist_added_on,
            liked, liked_on,
            my_rating, rated_on
        )
        SELECT
            uri,
            any_value(name),
            max(year),
            coalesce(bool_or(source = 'watched'), FALSE),
            max(on_date) FILTER (source = 'watched'),
            coalesce(bool_or(source = 'watchlist'), FALSE),
            max(on_date) FILTER (source = 'watchlist'),
            coalesce(bool_or(source = 'liked'), FALSE),
            max(on_date) FILTER (source = 'liked'),
            max(rating) FILTER (source = 'rating'),
            max(on_date) FILTER (source = 'rating')
        FROM ({" UNION ALL ".join(selects)})
        GROUP BY uri
        """,
        params,
    )
    return _count(conn, "staging_films")


# ------------------------------------------------------------------- diary

def _ingest_diary(
    conn: duckdb.DuckDBPyConnection, directory: Path, skipped: list[str]
) -> int:
    """Load `diary.csv` into `staging_diary`, attaching review text.

    `reviews.csv` repeats the diary columns and adds `Review`, and shares the
    diary's entry-URI namespace, so it joins on the URI directly.
    """
    diary_path = directory / "diary.csv"
    if not diary_path.exists():
        skipped.append("diary.csv (not present)")
        return 0

    reviews_path = directory / "reviews.csv"
    params: list[str] = [str(diary_path)]

    if reviews_path.exists():
        review_select = _STRIP_CR.format(column='r."Review"')
        review_join = 'LEFT JOIN read_csv_auto(?) r ON r."Letterboxd URI" = d."Letterboxd URI"'
        params.append(str(reviews_path))
    else:
        skipped.append("reviews.csv (not present)")
        review_select = "NULL::VARCHAR"
        review_join = ""

    conn.execute("DELETE FROM staging_diary")
    conn.execute(
        f"""
        INSERT INTO staging_diary (
            entry_uri, name, year, watched_date, logged_date,
            rating, rewatch, tags, review
        )
        SELECT
            d."Letterboxd URI",
            d."Name",
            d."Year",
            d."Watched Date",
            d."Date",
            d."Rating",
            -- Letterboxd writes TRUE or nothing, never FALSE.
            coalesce(d."Rewatch", FALSE),
            CASE
                WHEN d."Tags" IS NULL OR trim(d."Tags") = '' THEN NULL
                ELSE list_transform(string_split(d."Tags", '{_TAG_SPLIT}'), t -> trim(t))
            END,
            {review_select}
        FROM read_csv_auto(?) d
        {review_join}
        """,
        params,
    )

    for folder in ("deleted", "orphaned"):
        path = directory / folder / "diary.csv"
        if path.exists() and _csv_has_rows(path):
            skipped.append(f"{folder}/diary.csv (not imported)")

    return _count(conn, "staging_diary")


# ------------------------------------------------------------------- lists

def _ingest_lists(
    conn: duckdb.DuckDBPyConnection, directory: Path, skipped: list[str]
) -> tuple[int, int]:
    """Parse every list CSV into `lists` and `list_entries`.

    Lists are upserted on their slug rather than deleted and re-created.
    DuckDB refuses to delete a parent row in the same transaction that deleted
    the children referencing it -- even once none remain -- so a delete-and-
    reload of both tables cannot be atomic. See :func:`_remove_stale_lists`.
    Upserting also keeps `list_id` stable across re-imports.
    """
    lists_dir = directory / "lists"
    if not lists_dir.is_dir():
        skipped.append("lists/ (not present)")
        return 0, 0

    list_count = 0
    entry_count = 0

    for path in sorted(lists_dir.glob("*.csv")):
        try:
            parsed = parse_list_csv(path)
        except (ValueError, UnicodeDecodeError) as error:
            skipped.append(f"lists/{path.name} ({error})")
            continue

        list_id = conn.execute(
            """
            INSERT INTO lists (slug, name, description, tags, url, created_date, source)
            VALUES (?, ?, ?, ?, ?, try_cast(? AS DATE), 'letterboxd')
            ON CONFLICT (slug) DO UPDATE SET
                name         = excluded.name,
                description  = excluded.description,
                tags         = excluded.tags,
                url          = excluded.url,
                created_date = excluded.created_date
            RETURNING list_id
            """,
            [
                parsed.slug,
                parsed.name,
                parsed.description,
                parsed.tags or None,
                parsed.url,
                parsed.created_date,
            ],
        ).fetchone()[0]
        list_count += 1

        # Only children are removed here, never the parent row.
        conn.execute("DELETE FROM list_entries WHERE list_id = ?", [list_id])

        if parsed.entries:
            conn.executemany(
                """
                INSERT INTO list_entries
                    (list_id, entry_position, name, year, letterboxd_uri, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        list_id,
                        entry["position"],
                        entry["name"],
                        entry["year"],
                        entry["url"],
                        entry["description"],
                    )
                    for entry in parsed.entries
                ],
            )
            entry_count += len(parsed.entries)

    return list_count, entry_count


def _remove_stale_lists(conn: duckdb.DuckDBPyConnection, directory: Path) -> None:
    """Drop Letterboxd lists that no longer appear in the export.

    Runs outside the main transaction on purpose. DuckDB's foreign keys reject
    deleting a parent row in the same transaction that deleted the children
    referencing it, even when none remain -- so the two deletes have to commit
    separately. In autocommit they do.

    Lists whose source is not 'letterboxd' (AFI Top 100, a friend's ranked
    list) are never touched: they did not come from the export, so its absence
    says nothing about them.
    """
    lists_dir = directory / "lists"
    if not lists_dir.is_dir():
        return

    present = [path.stem for path in lists_dir.glob("*.csv")]
    stale = conn.execute(
        """
        SELECT list_id FROM lists
        WHERE source = 'letterboxd'
          AND slug NOT IN (SELECT unnest(?::VARCHAR[]))
        """,
        [present],
    ).fetchall()

    for (list_id,) in stale:
        conn.execute("DELETE FROM list_entries WHERE list_id = ?", [list_id])
        conn.execute("DELETE FROM lists WHERE list_id = ?", [list_id])


def parse_list_csv(path: Path) -> ListExport:
    """Parse a Letterboxd list export, which holds two tables in one file.

    The layout is a version banner, a one-row metadata table, a blank line,
    then the entries table::

        Letterboxd list export v7
        Date,Name,Tags,URL,Description
        2021-11-06,Wes Anderson ranked,,https://boxd.it/dWPpg,

        Position,Name,Year,URL,Description
        1,The Royal Tenenbaums,2001,https://boxd.it/1YHU,

    DuckDB's sniffer rejects this outright, so it is tokenised with the `csv`
    module -- which also handles descriptions containing commas or newlines.
    The entries table is located by its header row rather than by line number,
    so a multi-line description cannot shift it out from under us.

    Raises:
        ValueError: If no entries header is found.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    header_index = next(
        (i for i, row in enumerate(rows) if row and row[0] == "Position"), None
    )
    if header_index is None:
        raise ValueError("no 'Position' header row; not a Letterboxd list export")

    metadata = _list_metadata(rows[:header_index])
    entries = [
        {
            "position": int(row[0]),
            "name": row[1],
            "year": int(row[2]) if len(row) > 2 and row[2].strip() else None,
            "url": row[3] if len(row) > 3 and row[3].strip() else None,
            "description": _clean(row[4]) if len(row) > 4 and row[4].strip() else None,
        }
        for row in rows[header_index + 1 :]
        if row and row[0].strip().isdigit()
    ]

    return ListExport(
        slug=path.stem,
        name=metadata.get("Name") or path.stem,
        description=_clean(metadata.get("Description")),
        tags=_split_tags(metadata.get("Tags")),
        url=metadata.get("URL") or None,
        created_date=metadata.get("Date") or None,
        entries=entries,
    )


def _list_metadata(rows: list[list[str]]) -> dict[str, str]:
    """Pull the one-row metadata table out of a list export's preamble."""
    for index, row in enumerate(rows):
        if row and row[0] == "Date" and index + 1 < len(rows):
            return dict(zip(row, rows[index + 1]))
    return {}


def _split_tags(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [tag.strip() for tag in raw.split(_TAG_SPLIT) if tag.strip()]


def _clean(text: str | None) -> str | None:
    r"""Normalise CRLF to LF in a free-text field, and blank to None.

    Letterboxd exports use CRLF, so a description or bio spanning lines
    carries carriage returns inside the value. See :data:`_STRIP_CR`.
    """
    if text is None:
        return None
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    return cleaned or None


# ----------------------------------------------------------------- profile

def _ingest_profile(
    conn: duckdb.DuckDBPyConnection, directory: Path, skipped: list[str]
) -> str | None:
    """Load `profile.csv`. The email address in it is deliberately dropped."""
    path = directory / "profile.csv"
    if not path.exists():
        skipped.append("profile.csv (not present)")
        return None

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        skipped.append("profile.csv (empty)")
        return None

    row = rows[0]
    username = (row.get("Username") or "").strip()
    if not username:
        skipped.append("profile.csv (no username)")
        return None

    conn.execute("DELETE FROM profile")
    conn.execute(
        """
        INSERT INTO profile (username, date_joined, given_name, family_name,
                             location, website, bio, pronoun, favorite_film_uris)
        VALUES (?, try_cast(? AS DATE), ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            username,
            row.get("Date Joined") or None,
            row.get("Given Name") or None,
            row.get("Family Name") or None,
            row.get("Location") or None,
            row.get("Website") or None,
            _clean(row.get("Bio")),
            row.get("Pronoun") or None,
            _split_tags(row.get("Favorite Films")) or None,
        ],
    )
    return username


# ------------------------------------------------------------------ helpers

def _count(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _csv_has_rows(path: Path) -> bool:
    """True if a CSV has at least one row beyond its header."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return next(reader, None) is not None
