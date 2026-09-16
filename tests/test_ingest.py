"""Ingestion of a Letterboxd CSV export.

The fixture below reproduces the export's real quirks in miniature: film-scoped
and entry-scoped URI namespaces, a Rewatch column that is TRUE or blank but
never FALSE, review text containing a comma and a newline, and list files
holding two tables each.
"""

from __future__ import annotations

import pytest

from letterboxd_utility_tools.core import db
from letterboxd_utility_tools.core.ingest import (
    ExportNotFoundError,
    ingest_export,
    parse_list_csv,
)

# Film-scoped URIs: shared by watched / ratings / watchlist / likes.
SHINING = "https://boxd.it/film01"
ALIEN = "https://boxd.it/film02"
GODFATHER = "https://boxd.it/film03"
UNSEEN = "https://boxd.it/film04"

# Entry-scoped URIs: one per viewing, deliberately unlike the film URIs.
ENTRY_1 = "https://boxd.it/entryA"
ENTRY_2 = "https://boxd.it/entryB"
ENTRY_3 = "https://boxd.it/entryC"


def write_csv(path, text):
    r"""Write a CSV the way Letterboxd does: CRLF everywhere.

    Real exports contain zero bare line feeds, so a review or description
    spanning lines carries \r\n inside the quoted value itself. Ingestion
    is expected to normalise that away, and these tests pin it.
    """
    path.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")


def write_export(root):
    """Build a miniature but faithful Letterboxd export."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "likes").mkdir()
    (root / "lists").mkdir()

    write_csv(
        root / "watched.csv",
        "Date,Name,Year,Letterboxd URI\n"
        f"2024-01-10,The Shining,1980,{SHINING}\n"
        f"2024-02-11,Alien,1979,{ALIEN}\n"
        f"2024-03-12,The Godfather,1972,{GODFATHER}\n",
    )
    write_csv(
        root / "ratings.csv",
        "Date,Name,Year,Letterboxd URI,Rating\n"
        f"2024-01-10,The Shining,1980,{SHINING},4.5\n"
        f"2024-03-12,The Godfather,1972,{GODFATHER},5\n",
    )
    write_csv(
        root / "watchlist.csv",
        "Date,Name,Year,Letterboxd URI\n"
        f"2024-05-01,Solaris,1972,{UNSEEN}\n",
    )
    write_csv(
        root / "likes" / "films.csv",
        "Date,Name,Year,Letterboxd URI\n"
        f"2024-01-11,The Shining,1980,{SHINING}\n",
    )
    # Rewatch is TRUE or blank -- Letterboxd never writes FALSE.
    # The Shining appears twice: a first viewing and a later rewatch, rated
    # differently. Tags are one quoted, comma-separated field.
    write_csv(
        root / "diary.csv",
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
        f'2024-01-11,The Shining,1980,{ENTRY_1},4.5,,"horror, halloween",2024-01-10\n'
        f"2024-02-12,Alien,1979,{ENTRY_2},,,,2024-02-11\n"
        f"2024-03-13,The Shining,1980,{ENTRY_3},5,Yes,,2024-03-12\n",
    )
    # Review text with a comma and an embedded newline, as real exports have.
    write_csv(
        root / "reviews.csv",
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
        f'2024-02-12,Alien,1979,{ENTRY_2},,,"Slow, and\nbetter for it.",,2024-02-11\n',
    )
    write_csv(
        root / "profile.csv",
        "Date Joined,Username,Given Name,Family Name,Email Address,Location,"
        "Website,Bio,Pronoun,Favorite Films\n"
        "2019-10-03,someone,Some,One,secret@example.com,Chennai,"
        f'https://example.com,,They / them,"{SHINING}, {ALIEN}"\n',
    )
    write_csv(
        root / "lists" / "favourites.csv",
        "Letterboxd list export v7\n"
        "Date,Name,Tags,URL,Description\n"
        '2021-11-06,My Favourites,"best, rewatchable",https://boxd.it/list1,"A list,\nover two lines."\n'
        "\n"
        "Position,Name,Year,URL,Description\n"
        f"1,The Shining,1980,{SHINING},\n"
        f"2,Alien,1979,{ALIEN},Still holds up\n",
    )
    return root


@pytest.fixture
def export(tmp_path):
    return write_export(tmp_path / "export")


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
