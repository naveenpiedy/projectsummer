"""describe_schema: tables, then columns, then what a column actually holds."""

from __future__ import annotations

import pytest

from conftest import SAMPLE_FILMS
from projectsummer.core import catalog, db
from projectsummer.core.catalog import InvalidArgumentError
from projectsummer.core.errors import NoResultError
from projectsummer.core.plugins.explore import describe_schema


# ------------------------------------------------------------------- tables

def test_tables_list_the_public_ones_in_reading_order(conn):
    result = describe_schema()

    assert result.level == "tables"
    assert [t.name for t in result.tables] == list(catalog.PUBLIC_RELATIONS)
    films = result.tables[0]
    assert films.kind == "table"
    assert films.rows == len(SAMPLE_FILMS)
    assert "film_watch_stats" in films.description
    assert result.tables[2].kind == "view"


def test_internal_tables_are_listed_only_on_request(conn):
    hidden = {t.name for t in describe_schema().tables}
    assert not hidden & catalog.INTERNAL_RELATIONS

    shown = describe_schema(include_internal=True).tables
    internal = [t for t in shown if t.internal]
    assert {t.name for t in internal} == catalog.INTERNAL_RELATIONS
    assert shown[-len(internal):] == internal  # after the public ones


def test_every_public_table_and_column_is_described(conn):
    """The descriptions are what make the schema usable without guessing."""
    for name in catalog.PUBLIC_RELATIONS:
        table = describe_schema(table=name).table
        assert table.description, name
        undescribed = [c.name for c in table.columns if not c.description]
        assert undescribed == [], f"{name}: {undescribed}"


# ------------------------------------------------------------------ columns

def test_a_table_shows_its_columns(conn):
    result = describe_schema(table="films")

    assert result.level == "columns"
    columns = {c.name: c for c in result.table.columns}
    assert columns["genres"].is_list is True
    assert columns["genres"].type == "VARCHAR[]"
    assert "Science Fiction" in columns["genres"].description
    assert columns["title"].is_list is False
    assert "column" in result.next_step


def test_an_internal_table_can_still_be_described_by_name(conn):
    result = describe_schema(table="staging_films")
    assert result.table.internal is True


def test_an_unknown_table_suggests_the_real_one(conn):
    with pytest.raises(NoResultError, match="Did you mean: films"):
        describe_schema(table="film")


# ------------------------------------------------------------------- values

def test_a_list_column_shows_its_items_most_common_first(conn):
    values = describe_schema(table="films", column="genres").column

    assert values.is_list is True
    assert values.distinct_values == 7
    assert (values.values[0].value, values.values[0].rows) == ("Horror", 3)
    # Ties are broken alphabetically, so the order is stable.
    assert [v.value for v in values.values[1:4]] == ["Mystery", "Science Fiction", "Thriller"]
    assert values.search is None and values.matching_values is None


def test_a_value_repeated_within_one_list_counts_that_row_once(conn):
    conn.execute(
        "INSERT INTO films (tmdb_id, title, genres) VALUES (1, 'Twice', ['Drama', 'Drama'])"
    )
    values = describe_schema(table="films", column="genres").column
    counts = {v.value: v.rows for v in values.values}
    assert counts["Drama"] == 2  # The Godfather, and Twice once


def test_rows_without_a_value_include_null_and_empty_lists(conn):
    conn.execute("INSERT INTO films (tmdb_id, title, genres) VALUES (1, 'Null', NULL)")
    conn.execute("INSERT INTO films (tmdb_id, title, genres) VALUES (2, 'Empty', [])")

    values = describe_schema(table="films", column="genres").column
    assert values.rows == len(SAMPLE_FILMS) + 2
    assert values.rows_without_value == 2


def test_numbers_and_dates_report_their_range(conn):
    values = describe_schema(table="films", column="year").column
    assert (values.minimum, values.maximum) == (1972, 2010)

    text = describe_schema(table="films", column="title").column
    assert (text.minimum, text.maximum) == (None, None)


def test_more_values_than_shown_is_flagged(conn, monkeypatch):
    monkeypatch.setattr(catalog, "TOP_VALUES", 2)
    values = describe_schema(table="films", column="genres").column
    assert len(values.values) == 2
    assert values.values_truncated is True


def test_next_step_shows_how_to_filter_each_kind_of_column(conn):
    assert "list_contains(genres" in describe_schema(table="films", column="genres").next_step
    assert "year = " in describe_schema(table="films", column="year").next_step


def test_an_unknown_column_suggests_the_real_one(conn):
    with pytest.raises(NoResultError, match="Did you mean: genres"):
        describe_schema(table="films", column="genre")


# ------------------------------------------------------------------- search
#
# The reason search exists: the exact stored spelling of a name is what a
# filter needs, and guessing it silently returns nothing.

@pytest.mark.parametrize(
    ("column", "search", "expected"),
    [
        ("directors", "scorsese", "Martin Scorsese"),         # part of a name
        ("directors", "KUBRICK", "Stanley Kubrick"),          # any case
        ("directors", "stanley kubrik", "Stanley Kubrick"),   # a misspelling
        ("directors", "ridleyscott", "Ridley Scott"),         # spacing
        ("directors", "kubrik", "Stanley Kubrick"),           # a misspelt surname alone
        ("genres", "sci-fi", "Science Fiction"),              # a near synonym
    ],
)
def test_search_finds_the_stored_spelling(conn, column, search, expected):
    values = describe_schema(table="films", column=column, search=search).column
    assert values.values[0].value == expected
    assert values.search == search
    assert values.matching_values >= 1


def test_punctuation_and_spacing_do_not_hide_a_name(conn):
    conn.execute(
        "INSERT INTO films (tmdb_id, title, directors) VALUES (1, 'Muthu', ['K. S. Ravikumar'])"
    )
    values = describe_schema(table="films", column="directors", search="ravi kumar").column
    assert [v.value for v in values.values] == ["K. S. Ravikumar"]


def test_values_containing_the_text_come_before_misspellings(conn):
    conn.execute(
        "INSERT INTO films (tmdb_id, title, directors) VALUES (1, 'x', ['Ridley Scot'])"
    )
    values = describe_schema(table="films", column="directors", search="ridley scott").column
    assert [v.value for v in values.values] == ["Ridley Scott", "Ridley Scot"]


def test_a_search_with_no_match_is_an_empty_answer_not_an_error(conn):
    values = describe_schema(table="films", column="directors", search="zzzzzz").column
    assert values.values == []
    assert values.matching_values == 0


# ------------------------------------------------------------ bad arguments

def test_a_column_needs_its_table(conn):
    with pytest.raises(InvalidArgumentError, match="table"):
        describe_schema(column="genres")


@pytest.mark.parametrize("search", ["", "   ", "--"])
def test_a_search_needs_something_to_look_for(conn, search):
    with pytest.raises(InvalidArgumentError, match="letter or digit"):
        describe_schema(table="films", column="directors", search=search)


def test_a_search_needs_its_column(conn):
    with pytest.raises(InvalidArgumentError, match="column"):
        describe_schema(table="films", search="horror")


# -------------------------------------------------- over a read-only session

def test_every_level_works_read_only(tmp_path):
    db.close_connection()
    path = tmp_path / "library.duckdb"
    try:
        with db.session(path) as connection:
            connection.executemany(
                """
                INSERT INTO films
                    (tmdb_id, title, year, runtime, genres, directors, on_watchlist, watched)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                SAMPLE_FILMS,
            )

        with db.session(path, read_only=True):
            assert describe_schema().tables
            assert describe_schema(table="films").table.columns
            found = describe_schema(table="films", column="directors", search="kubrik")
            assert found.column.values[0].value == "Stanley Kubrick"
    finally:
        db.close_connection()


def test_the_role_description_names_every_role(conn):
    """The description is what tells an agent which roles exist; it must not
    fall behind the mapping in code."""
    from projectsummer.core import credits

    role = next(
        column for column in describe_schema(table="film_credits").table.columns
        if column.name == "role"
    )
    missing = [name for name in credits.roles() if name not in role.description]
    assert missing == []
