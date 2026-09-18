"""The plugin guide's examples, loaded and run as real plugins.

A guide that no longer works is worse than no guide: someone follows it and
blames their own code. Every Python block in `docs/writing-a-plugin.md` that
defines a plugin is written to a directory, discovered the way a third-party
plugin is, and called.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from projectsummer.core import db, registry
from projectsummer.core.results import Result

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "writing-a-plugin.md"

#: Arguments for guide plugins that cannot be called bare. None so far, but
#: the hook keeps a new example from going unrun.
SAMPLE_ARGUMENTS: dict[str, dict[str, object]] = {}


def examples() -> list[str]:
    """Every Python block in the guide that registers a plugin."""
    blocks = re.findall(r"```python\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.S)
    return [block for block in blocks if "@plugin" in block and "def " in block]


def test_the_guide_has_examples_to_run():
    """Guards the extraction: a rename here would otherwise pass silently."""
    assert len(examples()) >= 2


@pytest.mark.parametrize("source", examples(), ids=lambda source: source.split("def ")[1].split("(")[0])
def test_an_example_from_the_guide_registers_and_runs(source, tmp_path, conn):
    (tmp_path / "example.py").write_text(source, encoding="utf-8")

    found = registry.discover(extra_dirs=[tmp_path])
    assert registry.load_failures() == (), registry.load_failures()

    name = source.split("def ")[1].split("(")[0]
    item = found[name]
    result = item.func(**SAMPLE_ARGUMENTS.get(name, {}))
    assert isinstance(result, Result)
    assert isinstance(result, item.result_type)


def test_the_example_reads_what_the_sample_library_holds(tmp_path, conn):
    """Not just "it ran": the shortest example returns the real answer."""
    (tmp_path / "example.py").write_text(examples()[0], encoding="utf-8")
    longest = registry.discover(extra_dirs=[tmp_path])["longest_films"].func(limit=2)

    watched = db.query(
        "SELECT title FROM films WHERE watched AND runtime IS NOT NULL "
        "ORDER BY runtime DESC LIMIT 2"
    )
    assert longest.films == [row["title"] for row in watched]


# -------------------------------------------------------------- links

ROOT = GUIDE.parent.parent

#: Every Markdown file that ships with the project.
PAGES = sorted([ROOT / "README.md", *(ROOT / "docs").glob("*.md")])


def _links(page: Path) -> list[tuple[str, str]]:
    """(text, target) for every link that points at a file in the project."""
    return [
        (text, target)
        for text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", page.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_every_link_between_the_docs_resolves(page):
    """A moved page or a renamed heading should fail here, not in someone's
    browser."""
    for text, target in _links(page):
        path, _, anchor = target.partition("#")
        destination = (page.parent / path).resolve() if path else page
        assert destination.exists(), f"{page.name}: {text!r} -> {target}"

        if anchor:
            headings = {
                re.sub(r"[^a-z0-9 -]", "", line.lstrip("# ").strip().lower()).replace(" ", "-")
                for line in destination.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")
            }
            assert anchor in headings, f"{page.name}: {text!r} -> {target} (no such heading)"
