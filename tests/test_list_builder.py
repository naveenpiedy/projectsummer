"""Building Letterboxd-importable lists.

The library here is hand-built rather than run through enrichment, so each
film differs in exactly the ways the filters care about -- director, cast,
keywords, status, rating, viewing dates -- and includes the awkward cases the
importer's rules exist for: a title with quotes, one with a comma, one in
another script, and a film with no known release year.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from projectsummer.core import db, list_builder
from projectsummer.core.errors import EmptyDatabaseError, NoResultError
from projectsummer.core.list_builder import (
    IMPORT_COLUMNS,
    InvalidFilterError,
    InvalidQueryError,
    ListFilters,
    build_list,
    parse_date,
    write_import_files,
)

INCEPTION, INTERSTELLAR, TENENBAUMS, DUNDEE, KASETHAN, TEA = 27205, 157336, 9428, 9480, 99999, 1

FILMS = [
    # tmdb_id, imdb_id, title, release_date, directors, cast, genres, keywords,
    # watched, watchlist, liked, my_rating, tmdb_rating
    (INCEPTION, "tt1375666", "Inception", "2010-07-15", ["Christopher Nolan"],
     ["Leonardo DiCaprio", "Tom Hardy"], ["Action", "Science Fiction"], ["dream", "heist"],
     True, False, False, 4.5, 8.4),
    (INTERSTELLAR, "tt0816692", "Interstellar", "2014-11-05", ["Christopher Nolan"],
     ["Matthew McConaughey", "Anne Hathaway"], ["Science Fiction", "Drama"],
     ["time travel", "space"], True, False, False, 5.0, 8.5),
    (TENENBAUMS, "tt0265666", "The Royal Tenenbaums", "2001-10-05", ["Wes Anderson"],
     ["Gene Hackman", "Anjelica Huston"], ["Comedy", "Drama"], ["family"],
     True, False, True, 4.0, 7.4),
    (DUNDEE, "tt0090555", '"Crocodile" Dundee', "1986-09-26", ["Peter Faiman"],
     ["Paul Hogan"], ["Adventure", "Comedy"], ["australia"], False, True, False, None, 6.5),
    (KASETHAN, None, "கசேதான் கடவுளடா", "2023-03-24", ["R. Kannan"],
     ["Shiva", "Priya Anand"], ["Comedy"], None, True, False, False, 1.5, 5.0),
    (TEA, "tt0000001", "Crime, Punishment and Tea", None, ["Anon"],
     None, ["Drama"], None, False, True, False, None, None),
]

DIARY = [
    (INCEPTION, "2024-01-10", 4.5, False),
    (INCEPTION, "2024-06-01", 4.5, True),
    (INTERSTELLAR, "2023-03-05", 5.0, False),
    (TENENBAUMS, "2024-02-20", 4.0, False),
    (KASETHAN, "2026-08-15", 1.5, False),
]


@pytest.fixture
def library(empty_conn):
    empty_conn.executemany(
        """
        INSERT INTO films (tmdb_id, imdb_id, title, release_date, year, directors,
                           cast_members, genres, keywords, watched, on_watchlist, liked,
                           my_rating, tmdb_rating)
        VALUES (?, ?, ?, try_cast(? AS DATE), year(try_cast(? AS DATE)), ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        """,
        [(f[0], f[1], f[2], f[3], f[3], *f[4:]) for f in FILMS],
    )
    empty_conn.executemany(
        "INSERT INTO diary_entries (tmdb_id, watched_date, logged_date, rating, rewatch) "
        "VALUES (?, ?, ?, ?, ?)",
        [(t, d, d, r, rw) for t, d, r, rw in DIARY],
    )
    return empty_conn


def ids(tmp_path, **filters) -> list[int]:
    """Build a list and return the tmdb ids in file order."""
    output = tmp_path / "list.csv"
    build_list(output, filters=ListFilters(**filters))
    return [int(row["tmdbID"]) for row in read_list(output)]


def read_list(path: Path) -> list[dict[str, str]]:
    """Read a written list back using Letterboxd's own dialect."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, doublequote=False, escapechar="\\"))


# ------------------------------------------------------ the file format

def test_the_header_is_exactly_letterboxds_identity_columns(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, filters=ListFilters(genres=("drama",)))
    header = output.read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(f'"{c}"' for c in IMPORT_COLUMNS)


def test_no_column_that_could_alter_the_diary_is_written():
    """The list importer is the diary importer too."""
    for column in ("Rating", "Rating10", "WatchedDate", "Rewatch", "Review", "Tags"):
        assert column not in IMPORT_COLUMNS


def test_quotes_are_backslash_escaped_not_doubled(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, sql=f"SELECT {DUNDEE} AS tmdb_id")
    line = output.read_text(encoding="utf-8").splitlines()[1]

    assert r'"\"Crocodile\" Dundee"' in line
    assert '""Crocodile""' not in line, "that is standard CSV, which Letterboxd does not use"


def test_a_comma_in_a_title_stays_inside_one_field(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, sql=f"SELECT {TEA} AS tmdb_id")
    assert read_list(output)[0]["Title"] == "Crime, Punishment and Tea"


def test_non_latin_titles_survive_as_utf8(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, sql=f"SELECT {KASETHAN} AS tmdb_id")
    assert read_list(output)[0]["Title"] == "கசேதான் கடவுளடா"
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf"), "no byte-order mark"


def test_missing_values_are_empty_not_none(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, sql=f"SELECT {KASETHAN} AS tmdb_id UNION ALL SELECT {TEA}")
    rows = read_list(output)
    assert rows[0]["imdbID"] == ""
    assert rows[1]["Year"] == ""
    assert "None" not in output.read_text(encoding="utf-8")


def test_multiple_directors_share_one_field(library, tmp_path):
    films = [{"tmdb_id": 1, "imdb_id": "tt1", "title": "Co-directed", "year": 2000,
              "directors": ["Joel Coen", "Ethan Coen"]}]
    output = tmp_path / "l.csv"
    write_import_files(films, output)
    assert read_list(output)[0]["Directors"] == "Joel Coen, Ethan Coen"


def test_the_output_must_be_a_csv(library, tmp_path):
    """Pointing it at your export .zip must not overwrite it."""
    with pytest.raises(InvalidFilterError, match=r"\.csv"):
        build_list(tmp_path / "export.zip", filters=ListFilters(genres=("drama",)))


def test_a_list_over_the_size_limit_is_split_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(list_builder, "MAX_FILE_BYTES", 400)
    films = [{"tmdb_id": i, "imdb_id": f"tt{i:07d}", "title": f"Film number {i}",
              "year": 2000, "directors": ["Somebody"]} for i in range(1, 31)]

    paths = write_import_files(films, tmp_path / "big.csv")

    assert len(paths) > 1
    assert [p.name for p in paths] == [f"big-{n}.csv" for n in range(1, len(paths) + 1)]
    written = [int(row["tmdbID"]) for p in paths for row in read_list(p)]
    assert written == list(range(1, 31)), "splitting must not reorder or drop films"
    for path in paths:
        assert path.stat().st_size <= 400
        assert path.read_text(encoding="utf-8").startswith('"tmdbID"'), "every part needs a header"


# --------------------------------------------------------------- filters

def test_director_matches_exactly_regardless_of_case(library, tmp_path):
    assert ids(tmp_path, directors=("CHRISTOPHER nolan",)) == [INCEPTION, INTERSTELLAR]


def test_a_near_miss_name_suggests_the_real_one(library, tmp_path):
    with pytest.raises(NoResultError, match="Did you mean: Christopher Nolan"):
        ids(tmp_path, directors=("nolan",))


def test_a_typo_suggests_the_real_name(library, tmp_path):
    with pytest.raises(NoResultError, match="Christopher Nolan"):
        ids(tmp_path, directors=("christoper nolan",))


def test_a_name_with_no_resemblance_says_so_without_guessing(library, tmp_path):
    with pytest.raises(NoResultError) as error:
        ids(tmp_path, directors=("Akira Kurosawa",))
    assert "Did you mean" not in str(error.value)


def test_repeated_actors_must_all_appear(library, tmp_path):
    assert ids(tmp_path, actors=("Leonardo DiCaprio", "Tom Hardy")) == [INCEPTION]


def test_names_that_exist_but_never_together_give_a_clear_empty_result(library, tmp_path):
    with pytest.raises(NoResultError, match="actor Tom Hardy; actor Gene Hackman"):
        ids(tmp_path, actors=("Tom Hardy", "Gene Hackman"))


def test_keyword_filter(library, tmp_path):
    assert ids(tmp_path, keywords=("Time Travel",)) == [INTERSTELLAR]


def test_genre_filter(library, tmp_path):
    assert set(ids(tmp_path, genres=("comedy",))) == {TENENBAUMS, DUNDEE, KASETHAN}


def test_status_narrows_the_starting_set(library, tmp_path):
    assert set(ids(tmp_path, status="watchlist")) == {DUNDEE, TEA}
    assert ids(tmp_path, status="liked") == [TENENBAUMS]
    assert DUNDEE not in ids(tmp_path, status="watched")


def test_the_default_starts_from_the_whole_library(library, tmp_path):
    assert len(ids(tmp_path)) == len(FILMS)


def test_min_rating_uses_your_rating(library, tmp_path):
    assert set(ids(tmp_path, min_rating=4.5)) == {INCEPTION, INTERSTELLAR}


def test_release_year_range(library, tmp_path):
    assert ids(tmp_path, year_from=2000, year_to=2011) == [TENENBAUMS, INCEPTION]


def test_watched_date_range_matches_any_viewing_in_it(library, tmp_path):
    films = ids(tmp_path, watched_from=parse_date("2024-01-01", "x"),
                watched_to=parse_date("2024-12-31", "x"), order_by="watched")
    # Inception's first 2024 viewing is January, Tenenbaums' is February.
    assert films == [INCEPTION, TENENBAUMS]


def test_an_open_ended_date_range(library, tmp_path):
    assert ids(tmp_path, watched_from=parse_date("2026-01-01", "x")) == [KASETHAN]


def test_order_by_rating_puts_the_best_first(library, tmp_path):
    assert ids(tmp_path, status="watched", order_by="rating")[:2] == [INTERSTELLAR, INCEPTION]


def test_reverse_flips_the_natural_order(library, tmp_path):
    assert ids(tmp_path, directors=("Christopher Nolan",), reverse=True) == [INTERSTELLAR, INCEPTION]


def test_films_without_a_sort_value_go_last(library, tmp_path):
    """TEA has no release date; it must not jump to the front."""
    assert ids(tmp_path, order_by="release")[-1] == TEA


def test_limit(library, tmp_path):
    assert ids(tmp_path, order_by="release", limit=2) == [DUNDEE, TENENBAUMS]


@pytest.mark.parametrize("filters, message", [
    ({"status": "seen"}, "status must be one of"),
    ({"order_by": "popularity"}, "order-by must be one of"),
    ({"year_from": 2020, "year_to": 1990}, "year-from is after year-to"),
    ({"limit": 0}, "limit must be at least 1"),
])
def test_malformed_filters_are_rejected(library, tmp_path, filters, message):
    with pytest.raises(InvalidFilterError, match=message):
        ids(tmp_path, **filters)


def test_dates_must_be_iso(library):
    with pytest.raises(InvalidFilterError, match="2024-01-31"):
        parse_date("01/08/2026", "watched-from")


# ------------------------------------------------------------------ SQL

def test_sql_keeps_the_querys_order(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, sql="SELECT tmdb_id FROM films WHERE my_rating IS NOT NULL "
                           "ORDER BY my_rating ASC")
    assert [int(r["tmdbID"]) for r in read_list(output)][0] == KASETHAN


def test_sql_drops_repeats_but_keeps_first_position(library, tmp_path):
    output = tmp_path / "l.csv"
    build_list(output, sql=f"SELECT tmdb_id FROM (VALUES ({TENENBAUMS}), ({INCEPTION}), "
                           f"({TENENBAUMS})) t(tmdb_id)")
    assert [int(r["tmdbID"]) for r in read_list(output)] == [TENENBAUMS, INCEPTION]


def test_sql_can_include_films_outside_the_library(library, tmp_path):
    """Letterboxd matches on tmdbID exactly; a title is not needed."""
    output = tmp_path / "l.csv"
    report = build_list(output, sql="SELECT 603 AS tmdb_id")
    assert report["not_in_library"] == 1
    assert read_list(output)[0]["tmdbID"] == "603"


@pytest.mark.parametrize("sql, message", [
    ("DELETE FROM films RETURNING tmdb_id", "this is a DELETE"),
    ("UPDATE films SET title = 'x' RETURNING tmdb_id", "this is a UPDATE"),
    ("SELECT tmdb_id FROM films; DROP TABLE films", "found 2 statements"),
    ("COPY films TO 'stolen.csv'", "this is a COPY"),
    ("SELEC tmdb_id FRM films", "does not parse"),
    ("SELECT title FROM films", "must return a tmdb_id column"),
])
def test_sql_that_is_not_a_single_select_is_refused(library, tmp_path, sql, message):
    with pytest.raises(InvalidQueryError, match=message):
        build_list(tmp_path / "l.csv", sql=sql)


def test_a_refused_query_leaves_the_library_untouched(library, tmp_path):
    before = db.query("SELECT count(*) AS n FROM films")[0]["n"]
    for sql in ("DELETE FROM films RETURNING tmdb_id", "SELECT 1; DROP TABLE films"):
        with pytest.raises(InvalidQueryError):
            build_list(tmp_path / "l.csv", sql=sql)
    assert db.query("SELECT count(*) AS n FROM films")[0]["n"] == before


def test_sql_and_filters_together_are_refused(library, tmp_path):
    with pytest.raises(InvalidFilterError, match="either a SQL query or filters"):
        build_list(tmp_path / "l.csv", filters=ListFilters(genres=("drama",)),
                   sql="SELECT tmdb_id FROM films")


def test_sql_returning_nothing_is_reported(library, tmp_path):
    with pytest.raises(NoResultError, match="returned no films"):
        build_list(tmp_path / "l.csv", sql="SELECT tmdb_id FROM films WHERE false")


# ----------------------------------------------------------- side effects

def test_building_a_list_writes_nothing_to_the_database(library, tmp_path):
    tables = ("films", "diary_entries", "lists", "list_entries")
    before = {t: db.query(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables}
    build_list(tmp_path / "l.csv", filters=ListFilters(status="watched"))
    after = {t: db.query(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables}
    assert before == after


def test_an_unenriched_library_says_so(empty_conn, tmp_path):
    empty_conn.execute("INSERT INTO staging_films (letterboxd_uri, name) VALUES ('u', 'X')")
    with pytest.raises(EmptyDatabaseError, match="not enriched yet"):
        build_list(tmp_path / "l.csv", filters=ListFilters())


def test_the_report_previews_the_list(library, tmp_path):
    report = build_list(tmp_path / "l.csv", filters=ListFilters(directors=("Wes Anderson",)))
    assert report["films"] == 1
    assert report["first_films"] == ["The Royal Tenenbaums (2001)"]
    assert report["built_from"] == "director Wes Anderson"


# ------------------------------------------------------------------ CLI

def test_repeated_options_work_through_the_cli(library, tmp_path):
    from typer.testing import CliRunner

    from projectsummer.cli import build_app

    output = tmp_path / "cli.csv"
    result = CliRunner(env={"COLUMNS": "200"}).invoke(build_app(), [
        "list-builder", str(output),
        "--actor", "Leonardo DiCaprio", "--actor", "Tom Hardy", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["films"] == 1
    assert [int(r["tmdbID"]) for r in read_list(output)] == [INCEPTION]
