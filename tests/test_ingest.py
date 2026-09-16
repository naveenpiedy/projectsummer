"""Ingestion of a Letterboxd CSV export.

The export fixtures live in `export_fixture.py`, which reproduces the real
format's quirks in miniature. The tests below cover what ingestion does with
them, and -- crucially -- what happens for users whose data does *not* look
like the one library this was developed against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from projectsummer.core import db
from projectsummer.core.ingest import (
    ExportNotFoundError,
    MalformedExportError,
    ingest_export,
    parse_list_csv,
)

from export_fixture import (
    ALIEN,
    DIARY_HEADER,
    ENTRY_1,
    ENTRY_2,
    SHINING,
    UNSEEN,
    write_csv,
    write_minimal_export,
)


# ------------------------------------------------------------------ loading

def test_reports_what_it_loaded(empty_conn, export):
    report = ingest_export(export)
    assert report.films == 4
    assert report.diary_entries == 3
    assert report.lists == 1
    assert report.list_entries == 2
    assert report.profile == "someone"


def test_film_sources_merge_into_one_row_per_film(empty_conn, export):
    ingest_export(export)
    shining = db.query(
        "SELECT * FROM staging_films WHERE letterboxd_uri = ?", [SHINING]
    )[0]
    assert shining["watched"] is True
    assert shining["liked"] is True
    assert shining["my_rating"] == 4.5
    assert shining["on_watchlist"] is False


def test_watchlist_only_film_is_not_marked_watched(empty_conn, export):
    ingest_export(export)
    solaris = db.query(
        "SELECT * FROM staging_films WHERE letterboxd_uri = ?", [UNSEEN]
    )[0]
    assert solaris["on_watchlist"] is True
    assert solaris["watched"] is False
    assert solaris["my_rating"] is None


def test_every_viewing_is_kept_not_collapsed(empty_conn, export):
    ingest_export(export)
    shining_entries = db.query(
        "SELECT watched_date, rating, rewatch FROM staging_diary "
        "WHERE name = 'The Shining' ORDER BY watched_date"
    )
    assert len(shining_entries) == 2
    assert shining_entries[0]["rewatch"] is False
    assert shining_entries[1]["rewatch"] is True
    # The rating differs between viewings -- this is what rating drift needs.
    assert shining_entries[0]["rating"] == 4.5
    assert shining_entries[1]["rating"] == 5.0


def test_blank_rewatch_becomes_false_not_null(empty_conn, export):
    ingest_export(export)
    assert db.query("SELECT count(*) AS n FROM staging_diary WHERE rewatch IS NULL")[0]["n"] == 0


def test_tags_become_a_list(empty_conn, export):
    ingest_export(export)
    tags = db.query(
        "SELECT tags FROM staging_diary WHERE entry_uri = ?", [ENTRY_1]
    )[0]["tags"]
    assert tags == ["horror", "halloween"]


def test_untagged_entry_has_null_tags(empty_conn, export):
    ingest_export(export)
    assert db.query("SELECT tags FROM staging_diary WHERE entry_uri = ?", [ENTRY_2])[0]["tags"] is None


def test_review_text_is_attached_to_its_entry(empty_conn, export):
    ingest_export(export)
    review = db.query(
        "SELECT review FROM staging_diary WHERE entry_uri = ?", [ENTRY_2]
    )[0]["review"]
    assert review == "Slow, and\nbetter for it."


def test_diary_and_film_uris_stay_in_separate_namespaces(empty_conn, export):
    """A URI join between the two would match nothing; (name, year) is the link."""
    ingest_export(export)
    overlap = db.query(
        "SELECT count(*) AS n FROM staging_diary d "
        "JOIN staging_films f ON f.letterboxd_uri = d.entry_uri"
    )[0]["n"]
    assert overlap == 0

    matched = db.query(
        "SELECT count(DISTINCT d.name) AS n FROM staging_diary d "
        "WHERE EXISTS (SELECT 1 FROM staging_films f "
        "              WHERE f.name = d.name AND f.year = d.year)"
    )[0]["n"]
    assert matched == 2  # The Shining and Alien


# -------------------------------------------------------------------- lists

def test_list_positions_are_preserved(empty_conn, export):
    ingest_export(export)
    entries = db.query(
        "SELECT entry_position, name FROM list_entries ORDER BY entry_position"
    )
    assert [e["name"] for e in entries] == ["The Shining", "Alien"]


def test_list_metadata_survives_a_multiline_description(export):
    parsed = parse_list_csv(export / "lists" / "favourites.csv")
    assert parsed.name == "My Favourites"
    assert parsed.tags == ["best", "rewatchable"]
    assert parsed.description == "A list,\nover two lines."
    assert len(parsed.entries) == 2


def test_a_file_without_a_position_header_is_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Letterboxd list export v7\nDate,Name\n2024-01-01,Nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Position"):
        parse_list_csv(bad)


# ------------------------------------------------------------------ profile

def test_email_address_is_not_stored(empty_conn, export):
    ingest_export(export)
    columns = {
        row["column_name"]
        for row in db.query("SELECT column_name FROM (DESCRIBE SELECT * FROM profile)")
    }
    assert not any("email" in name.lower() for name in columns)

    dumped = str(db.query("SELECT * FROM profile"))
    assert "secret@example.com" not in dumped


def test_favourite_films_become_a_list(empty_conn, export):
    ingest_export(export)
    assert db.query("SELECT favorite_film_uris AS f FROM profile")[0]["f"] == [SHINING, ALIEN]


# --------------------------------------------------------------- robustness

def test_missing_optional_files_are_reported_not_fatal(empty_conn, tmp_path):
    root = tmp_path / "partial"
    root.mkdir()
    write_csv(
        root / "watched.csv",
        f"Date,Name,Year,Letterboxd URI\n2024-01-10,The Shining,1980,{SHINING}\n",
    )

    report = ingest_export(root)
    assert report.films == 1
    assert report.diary_entries == 0
    assert any("watchlist.csv" in item for item in report.skipped)
    assert any("profile.csv" in item for item in report.skipped)


def test_a_directory_that_is_not_an_export_is_rejected(empty_conn, tmp_path):
    (tmp_path / "random.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(ExportNotFoundError, match="no Letterboxd CSVs"):
        ingest_export(tmp_path)


def test_missing_directory_is_rejected(empty_conn, tmp_path):
    with pytest.raises(ExportNotFoundError, match="not a directory"):
        ingest_export(tmp_path / "nope")


def test_reingesting_replaces_rather_than_duplicates(empty_conn, export):
    first = ingest_export(export)
    second = ingest_export(export)
    assert first.films == second.films
    assert first.diary_entries == second.diary_entries
    assert first.list_entries == second.list_entries


def test_reingesting_drops_entries_removed_from_the_export(empty_conn, export):
    ingest_export(export)
    (export / "watchlist.csv").write_text(
        "Date,Name,Year,Letterboxd URI\n", encoding="utf-8"
    )

    ingest_export(export)
    assert db.query("SELECT count(*) AS n FROM staging_films WHERE on_watchlist")[0]["n"] == 0


def test_imported_lists_survive_a_reingest(empty_conn, export):
    """Canonical lists (AFI Top 100, a friend's) are not ours to delete."""
    empty_conn.execute(
        "INSERT INTO lists (slug, name, source) VALUES ('afi-100', 'AFI Top 100', 'imported')"
    )
    ingest_export(export)

    sources = {row["source"] for row in db.query("SELECT source FROM lists")}
    assert sources == {"letterboxd", "imported"}


# ------------------------------------------------- data-dependent CSV typing
#
# read_csv_auto infers a column's type from its contents, so a column that
# happens to be empty in one person's export comes back VARCHAR instead of
# BOOLEAN or DOUBLE. Every case below crashed ingestion before the reader was
# changed to read text and cast explicitly. None of them are exotic -- they
# are just libraries that differ from the one this was written against.

def test_a_user_who_has_never_rewatched_anything_can_ingest(empty_conn, tmp_path):
    root = write_minimal_export(
        tmp_path / "e", f"2024-01-11,A Film,1980,{ENTRY_1},4,,,2024-01-10\n"
    )
    report = ingest_export(root)

    assert report.diary_entries == 1
    assert db.query("SELECT rewatch FROM staging_diary")[0]["rewatch"] is False


def test_a_user_who_rates_nothing_can_ingest(empty_conn, tmp_path):
    root = write_minimal_export(
        tmp_path / "e", f"2024-01-11,A Film,1980,{ENTRY_1},,,,2024-01-10\n"
    )
    report = ingest_export(root)

    assert report.diary_entries == 1
    assert db.query("SELECT rating FROM staging_diary")[0]["rating"] is None


def test_every_optional_column_blank_still_ingests(empty_conn, tmp_path):
    root = write_minimal_export(
        tmp_path / "e", f"2024-01-11,A Film,,{ENTRY_1},,,,\n"
    )
    report = ingest_export(root)

    row = db.query("SELECT * FROM staging_diary")[0]
    assert report.diary_entries == 1
    assert row["year"] is None
    assert row["rating"] is None
    assert row["watched_date"] is None
    assert row["rewatch"] is False
    assert row["tags"] is None


def test_unparseable_values_become_null_rather_than_failing(empty_conn, tmp_path):
    """try_cast, not cast: one malformed row must not abort the whole import."""
    root = write_minimal_export(
        tmp_path / "e",
        f"2024-01-11,A Film,nineteen-eighty,{ENTRY_1},rubbish,perhaps,,not-a-date\n",
    )
    ingest_export(root)

    row = db.query("SELECT * FROM staging_diary")[0]
    assert row["year"] is None
    assert row["rating"] is None
    assert row["watched_date"] is None
    assert row["rewatch"] is False  # unparseable is not truthy


def test_rewatch_yes_is_understood(empty_conn, tmp_path):
    """Letterboxd writes the literal string 'Yes', not 'true' or '1'."""
    root = write_minimal_export(
        tmp_path / "e", f"2024-01-11,A Film,1980,{ENTRY_1},4,Yes,,2024-01-10\n"
    )
    ingest_export(root)
    assert db.query("SELECT rewatch FROM staging_diary")[0]["rewatch"] is True


def test_ratings_keep_their_half_star_precision(empty_conn, tmp_path):
    root = write_minimal_export(
        tmp_path / "e", f"2024-01-11,A Film,1980,{ENTRY_1},4.5,,,2024-01-10\n"
    )
    ingest_export(root)
    assert db.query("SELECT rating FROM staging_diary")[0]["rating"] == 4.5


# ---------------------------------------------------------- malformed input

def test_a_missing_column_names_the_file_and_the_column(empty_conn, tmp_path):
    root = tmp_path / "e"
    root.mkdir()
    # 'Rewatch' omitted entirely.
    write_csv(
        root / "diary.csv",
        "Date,Name,Year,Letterboxd URI,Rating,Tags,Watched Date\n"
        f"2024-01-11,A Film,1980,{ENTRY_1},4,,2024-01-10\n",
    )

    with pytest.raises(MalformedExportError) as error:
        ingest_export(root)
    assert "diary.csv" in str(error.value)
    assert "Rewatch" in str(error.value)


def test_a_failed_ingest_leaves_no_partial_data(empty_conn, export):
    """The whole import is one transaction, so a late failure rolls it back."""
    ingest_export(export)
    before = db.query("SELECT count(*) AS n FROM staging_diary")[0]["n"]

    # Break diary.csv so the second run fails partway through.
    write_csv(export / "diary.csv", "Date,Name,Letterboxd URI\n2024-01-01,X,u\n")
    with pytest.raises(MalformedExportError):
        ingest_export(export)

    assert db.query("SELECT count(*) AS n FROM staging_diary")[0]["n"] == before


def test_stale_staging_rows_are_cleared_when_film_sources_vanish(empty_conn, export):
    """The 'replaced wholesale' promise has to hold even in the empty case."""
    ingest_export(export)
    assert db.query("SELECT count(*) AS n FROM staging_films")[0]["n"] == 4

    for name in ("watched.csv", "ratings.csv", "watchlist.csv"):
        (export / name).unlink()
    (export / "likes" / "films.csv").unlink()

    report = ingest_export(export)
    assert report.films == 0
    assert db.query("SELECT count(*) AS n FROM staging_films")[0]["n"] == 0


# ------------------------------------------------------------ zipped exports
#
# Letterboxd hands you a .zip, so making people unzip it first is a step that
# buys nothing. A zip is expanded into a temporary directory that must be
# gone afterwards, whether the import succeeded or failed.

def _zip_up(export_dir, archive, prefix=""):
    """Zip a built export, optionally nested inside a wrapper folder."""
    import zipfile

    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(export_dir.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(export_dir)
                bundle.write(path, f"{prefix}{arcname.as_posix()}")
    return archive


def test_a_zip_ingests_exactly_like_a_directory(empty_conn, export, tmp_path):
    from_directory = ingest_export(export)

    archive = _zip_up(export, tmp_path / "export.zip")
    from_zip = ingest_export(archive)

    assert from_zip.films == from_directory.films
    assert from_zip.diary_entries == from_directory.diary_entries
    assert from_zip.lists == from_directory.lists
    assert from_zip.list_entries == from_directory.list_entries
    assert from_zip.profile == from_directory.profile


def test_a_zip_wrapping_its_files_in_a_folder_also_works(empty_conn, export, tmp_path):
    """Letterboxd has shipped both layouts."""
    archive = _zip_up(export, tmp_path / "nested.zip", prefix="letterboxd-someone-2026/")
    report = ingest_export(archive)
    assert report.films == 4
    assert report.diary_entries == 3


def test_the_report_names_the_zip_not_the_temporary_directory(empty_conn, export, tmp_path):
    archive = _zip_up(export, tmp_path / "export.zip")
    report = ingest_export(archive)
    # The temporary directory is gone by the time anyone reads the report, so
    # naming it would be useless; the archive the user passed is the answer.
    assert report.export_dir == str(archive)
    assert report.export_dir.endswith(".zip")


def test_the_zip_is_not_unpacked_next_to_itself(empty_conn, export, tmp_path):
    archive = _zip_up(export, tmp_path / "solo" / "export.zip")
    before = set(archive.parent.iterdir())

    ingest_export(archive)

    assert set(archive.parent.iterdir()) == before


def test_the_temporary_directory_is_removed_afterwards(empty_conn, export, tmp_path, monkeypatch):
    import tempfile as tempfile_module

    created: list[str] = []
    real = tempfile_module.TemporaryDirectory

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self.name)

    monkeypatch.setattr(tempfile_module, "TemporaryDirectory", Recording)

    archive = _zip_up(export, tmp_path / "export.zip")
    ingest_export(archive)

    assert created, "a zip should have been expanded into a temporary directory"
    assert not any(Path(name).exists() for name in created)


def test_a_failed_zip_import_still_cleans_up(empty_conn, export, tmp_path, monkeypatch):
    import tempfile as tempfile_module

    created: list[str] = []
    real = tempfile_module.TemporaryDirectory

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self.name)

    monkeypatch.setattr(tempfile_module, "TemporaryDirectory", Recording)

    write_csv(export / "diary.csv", "Date,Name,Letterboxd URI\n2024-01-01,X,u\n")
    archive = _zip_up(export, tmp_path / "broken.zip")

    with pytest.raises(MalformedExportError):
        ingest_export(archive)

    assert created
    assert not any(Path(name).exists() for name in created)


def test_a_zip_that_would_escape_its_directory_is_refused(empty_conn, tmp_path):
    """Zip-slip: a crafted archive must not write outside the temp directory."""
    import zipfile

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("diary.csv", DIARY_HEADER)
        bundle.writestr("../escaped.txt", "pwned")

    with pytest.raises(MalformedExportError, match="outside the extraction directory"):
        ingest_export(archive)

    assert not (tmp_path / "escaped.txt").exists()


def test_a_zip_that_is_not_an_export_is_rejected(empty_conn, tmp_path):
    import zipfile

    archive = tmp_path / "holiday-photos.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("beach.jpg", "not a csv")

    with pytest.raises(ExportNotFoundError, match="no Letterboxd CSVs"):
        ingest_export(archive)
