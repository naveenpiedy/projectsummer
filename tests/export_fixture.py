"""Builds miniature Letterboxd exports for tests.

Shared by the ingestion tests and the CLI tests. The default export reproduces
the real format's quirks in miniature: film-scoped and entry-scoped URI
namespaces, a Rewatch column that is 'Yes' or blank but never 'No', review text
containing a comma and a newline, CRLF line endings throughout, and list files
holding two tables each.
"""

from __future__ import annotations

from pathlib import Path

# Film-scoped URIs: shared by watched / ratings / watchlist / likes.
SHINING = "https://boxd.it/film01"
ALIEN = "https://boxd.it/film02"
GODFATHER = "https://boxd.it/film03"
UNSEEN = "https://boxd.it/film04"

# Entry-scoped URIs: one per viewing, deliberately unlike the film URIs.
ENTRY_1 = "https://boxd.it/entryA"
ENTRY_2 = "https://boxd.it/entryB"
ENTRY_3 = "https://boxd.it/entryC"

DIARY_HEADER = "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
FILM_HEADER = "Date,Name,Year,Letterboxd URI\n"


def write_csv(path: Path, text: str) -> None:
    r"""Write a CSV the way Letterboxd does: CRLF everywhere.

    Real exports contain zero bare line feeds, so a review or description
    spanning lines carries \r\n inside the quoted value itself. Ingestion is
    expected to normalise that away, and the tests pin it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")


def write_export(root: Path) -> Path:
    """Build a miniature but faithful Letterboxd export."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "likes").mkdir(exist_ok=True)
    (root / "lists").mkdir(exist_ok=True)

    write_csv(
        root / "watched.csv",
        FILM_HEADER
        + f"2024-01-10,The Shining,1980,{SHINING}\n"
        + f"2024-02-11,Alien,1979,{ALIEN}\n"
        + f"2024-03-12,The Godfather,1972,{GODFATHER}\n",
    )
    write_csv(
        root / "ratings.csv",
        "Date,Name,Year,Letterboxd URI,Rating\n"
        + f"2024-01-10,The Shining,1980,{SHINING},4.5\n"
        + f"2024-03-12,The Godfather,1972,{GODFATHER},5\n",
    )
    write_csv(root / "watchlist.csv", FILM_HEADER + f"2024-05-01,Solaris,1972,{UNSEEN}\n")
    write_csv(
        root / "likes" / "films.csv",
        FILM_HEADER + f"2024-01-11,The Shining,1980,{SHINING}\n",
    )
    # The Shining appears twice: a first viewing and a later rewatch, rated
    # differently. Tags are one quoted, comma-separated field.
    write_csv(
        root / "diary.csv",
        DIARY_HEADER
        + f'2024-01-11,The Shining,1980,{ENTRY_1},4.5,,"horror, halloween",2024-01-10\n'
        + f"2024-02-12,Alien,1979,{ENTRY_2},,,,2024-02-11\n"
        + f"2024-03-13,The Shining,1980,{ENTRY_3},5,Yes,,2024-03-12\n",
    )
    write_csv(
        root / "reviews.csv",
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
        + f'2024-02-12,Alien,1979,{ENTRY_2},,,"Slow, and\nbetter for it.",,2024-02-11\n',
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
        '2021-11-06,My Favourites,"best, rewatchable",https://boxd.it/list1,'
        '"A list,\nover two lines."\n'
        "\n"
        "Position,Name,Year,URL,Description\n"
        f"1,The Shining,1980,{SHINING},\n"
        f"2,Alien,1979,{ALIEN},Still holds up\n",
    )
    return root


def write_minimal_export(root: Path, diary_rows: str) -> Path:
    """An export with only watched.csv and diary.csv, for type-inference tests.

    `diary_rows` is appended to the standard diary header, so a caller can
    leave whole columns blank -- which is exactly what breaks type sniffing.
    """
    root.mkdir(parents=True, exist_ok=True)
    write_csv(root / "watched.csv", FILM_HEADER + f"2024-01-10,A Film,1980,{SHINING}\n")
    write_csv(root / "diary.csv", DIARY_HEADER + diary_rows)
    return root
