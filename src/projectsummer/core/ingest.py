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

Input may be the `.zip` Letterboxd hands you or an already-unzipped
directory. A zip is expanded into a temporary directory that is removed when
the import finishes, successfully or not.
"""

from __future__ import annotations

import csv
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from projectsummer.core import db
from projectsummer.core.errors import LetterboxdError
from projectsummer.core.results import Result

#: Film-scoped exports: (source label, path within the export, has a Rating column).
_FILM_SOURCES: tuple[tuple[str, str, bool], ...] = (
    ("watched", "watched.csv", False),
    ("watchlist", "watchlist.csv", False),
    ("liked", "likes/films.csv", False),
    ("rating", "ratings.csv", True),
)

#: Separator Letterboxd uses inside its multi-value CSV fields.
_TAG_SPLIT = ","

#: Every CSV is read as text and cast explicitly, never sniffed.
#:
#: `read_csv_auto` infers a column's type from its *contents*, so an entirely
#: empty column comes back VARCHAR. A user who has never logged a rewatch gets
#: a VARCHAR `Rewatch`, and `coalesce(Rewatch, FALSE)` then fails to bind --
#: ingestion that works on one person's library crashes on another's. Reading
#: everything as VARCHAR makes the types independent of the data, and
#: `try_cast` turns anything unparseable into NULL instead of an error.
_READ_CSV = "read_csv(?, all_varchar = true, header = true)"

#: Files whose presence identifies a directory as a Letterboxd export.
_EXPECTED_FILES: tuple[str, ...] = (
    "watched.csv",
    "watchlist.csv",
    "likes/films.csv",
    "ratings.csv",
    "diary.csv",
)

#: Columns the film-scoped exports must provide.
_FILM_FILE_COLUMNS = frozenset({"Date", "Name", "Year", "Letterboxd URI"})

#: Columns diary.csv must provide.
_DIARY_FILE_COLUMNS = frozenset(
    {"Date", "Name", "Year", "Letterboxd URI", "Rating", "Rewatch", "Tags", "Watched Date"}
)

#: SQL that strips carriage returns from a text column. Letterboxd exports are
#: CRLF throughout, so a review or description spanning lines arrives with
#: \r\n embedded in the value itself. Left alone, those carriage returns leak
#: into every rendered table, JSON payload and LLM prompt downstream.
_STRIP_CR = "replace({column}, chr(13), '')"


class ExportNotFoundError(LetterboxdError):
    """The given directory does not look like a Letterboxd export."""


class MalformedExportError(LetterboxdError):
    """An export file is missing columns this code depends on."""


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


class IngestResult(Result):
    """What an import loaded."""

    export_dir: str
    """The export that was read."""
    films: int
    """Distinct films across watched, watchlist, ratings and likes."""
    diary_entries: int
    """Diary rows, one per viewing."""
    lists: int
    """Lists in the export."""
    list_entries: int
    """Films across all those lists."""
    profile: str | None
    """The username the export belongs to, if it included a profile."""
    skipped: list[str]
    """Export files that were absent or deliberately not imported."""


# ------------------------------------------------------------------- public

def ingest_export(export_path: str | Path) -> IngestResult:
    """Load every CSV in a Letterboxd export into the database.

    Accepts either the `.zip` Letterboxd hands you or an already-unzipped
    directory. A zip is expanded into a temporary directory that is deleted
    afterwards, so nothing is left lying around next to the archive and a
    failed run cleans up after itself.

    Staging tables and Letterboxd-sourced lists are replaced wholesale, so
    re-running against a newer export drops entries you have since removed
    rather than leaving them behind. Lists imported from elsewhere (AFI Top
    100, a friend's ranked list) are left alone.

    Args:
        export_path: The export `.zip`, or an unzipped directory containing
            `diary.csv` and `watchlist.csv`.

    Returns:
        An :class:`IngestResult` counting what was loaded.

    Raises:
        ExportNotFoundError: If the path is missing or holds no recognisable
            Letterboxd CSVs.
        MalformedExportError: If a zip member would escape the extraction
            directory, or a CSV is missing columns this code reads.
    """
    source = Path(export_path).expanduser()

    if source.is_file() and zipfile.is_zipfile(source):
        # ignore_cleanup_errors because Windows can briefly hold a handle on a
        # file that was just read; a stale temp directory must not turn a
        # successful import into a failure.
        with tempfile.TemporaryDirectory(
            prefix="letterboxd-export-", ignore_cleanup_errors=True
        ) as workspace:
            unpacked = _extract_export(source, Path(workspace))
            return _ingest_directory(unpacked, reported_as=source)

    return _ingest_directory(source, reported_as=source)


def _ingest_directory(directory: Path, *, reported_as: Path) -> IngestResult:
    """Ingest an unzipped export directory.

    Args:
        directory: Where the CSVs actually are, which for a zip is a
            temporary location.
        reported_as: What the user asked for, so the report names the archive
            they passed rather than a temporary directory that no longer
            exists by the time they read it.
    """
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
        try:
            conn.execute("ROLLBACK")
        except duckdb.Error:
            # DuckDB may have aborted the transaction already. Swallowing this
            # keeps the original failure as the one that reaches the caller.
            pass
        raise

    return IngestResult(
        export_dir=str(reported_as),
        films=films,
        diary_entries=diary,
        lists=list_count,
        list_entries=entry_count,
        profile=profile,
        skipped=skipped,
    )


def _extract_export(archive: Path, destination: Path) -> Path:
    """Unpack an export zip into `destination` and return the export root.

    Raises:
        MalformedExportError: If any member would be written outside
            `destination`. A crafted archive can otherwise use `..` segments
            or an absolute path to overwrite files elsewhere on disk, and
            this tool extracts whatever it is pointed at.
    """
    root = destination.resolve()

    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and not target.is_relative_to(root):
                raise MalformedExportError(
                    f"{archive.name} contains an entry that would be written "
                    f"outside the extraction directory: {member.filename!r}. "
                    f"Refusing to unpack it."
                )
        bundle.extractall(destination)

    return _find_export_root(destination)


def _find_export_root(directory: Path) -> Path:
    """Locate the CSVs, whether or not the zip wrapped them in a folder.

    Letterboxd has shipped both layouts: files at the archive root, and files
    inside a single `letterboxd-<user>-<date>` folder.
    """
    if any((directory / name).exists() for name in _EXPECTED_FILES):
        return directory

    subdirectories = [path for path in directory.iterdir() if path.is_dir()]
    if len(subdirectories) == 1:
        return subdirectories[0]

    # Nothing recognisable; let _check_export produce the useful message.
    return directory


def _check_export(directory: Path) -> None:
    if not directory.is_dir():
        raise ExportNotFoundError(f"{directory} is not a directory.")

    if not any((directory / name).exists() for name in _EXPECTED_FILES):
        raise ExportNotFoundError(
            f"{directory} contains no Letterboxd CSVs. Expected to find at "
            f"least one of: {', '.join(_EXPECTED_FILES)}. Point this at your "
            f"export .zip, or at the directory holding diary.csv."
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

        required = _FILM_FILE_COLUMNS | ({"Rating"} if has_rating else set())
        _require_columns(conn, path, required, filename)

        rating = 'try_cast("Rating" AS DOUBLE)' if has_rating else "NULL::DOUBLE"
        selects.append(
            f"""
            SELECT "Letterboxd URI" AS uri,
                   "Name" AS name,
                   try_cast("Year" AS INTEGER) AS year,
                   try_cast("Date" AS DATE) AS on_date,
                   {rating} AS rating,
                   '{source}' AS source
            FROM {_READ_CSV}
            """
        )
        params.append(str(path))

    # Before the early return: a re-ingest whose export no longer has any
    # film-scoped CSVs must still clear what the previous one left behind.
    conn.execute("DELETE FROM staging_films")

    if not selects:
        return 0

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

    _require_columns(conn, diary_path, _DIARY_FILE_COLUMNS, "diary.csv")

    reviews_path = directory / "reviews.csv"
    params: list[str] = [str(diary_path)]

    if reviews_path.exists():
        _require_columns(conn, reviews_path, {"Letterboxd URI", "Review"}, "reviews.csv")
        review_select = _STRIP_CR.format(column='r."Review"')
        review_join = (
            f'LEFT JOIN {_READ_CSV} r ON r."Letterboxd URI" = d."Letterboxd URI"'
        )
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
            try_cast(d."Year" AS INTEGER),
            try_cast(d."Watched Date" AS DATE),
            try_cast(d."Date" AS DATE),
            try_cast(d."Rating" AS DOUBLE),
            -- Letterboxd writes 'Yes' or nothing, never a falsey value.
            coalesce(try_cast(d."Rewatch" AS BOOLEAN), FALSE),
            CASE
                WHEN d."Tags" IS NULL OR trim(d."Tags") = '' THEN NULL
                ELSE list_transform(string_split(d."Tags", '{_TAG_SPLIT}'), t -> trim(t))
            END,
            {review_select}
        FROM {_READ_CSV} d
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

def _require_columns(
    conn: duckdb.DuckDBPyConnection,
    path: Path,
    required: set[str] | frozenset[str],
    label: str,
) -> None:
    """Check an export file has the columns this code reads.

    Without this, a missing column surfaces as a DuckDB binder error naming an
    internal query rather than the file the user needs to look at.

    Raises:
        MalformedExportError: If any required column is absent.
    """
    described = conn.execute(
        f"DESCRIBE SELECT * FROM {_READ_CSV}", [str(path)]
    ).fetchall()
    found = {row[0] for row in described}

    missing = sorted(set(required) - found)
    if missing:
        raise MalformedExportError(
            f"{label} is missing expected column(s): {', '.join(missing)}. "
            f"Found: {', '.join(sorted(found))}. Is this a Letterboxd export?"
        )


def _count(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _csv_has_rows(path: Path) -> bool:
    """True if a CSV has at least one row beyond its header."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return next(reader, None) is not None
