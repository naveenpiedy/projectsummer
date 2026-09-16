"""List plugins: your Letterboxd lists, and canonical lists to compare against."""

from __future__ import annotations

from typing import Any

from letterboxd_utility_tools.core import db
from letterboxd_utility_tools.core.errors import NoResultError
from letterboxd_utility_tools.core.registry import plugin


@plugin(name="lists", category="lists")
def show_lists() -> list[dict[str, Any]]:
    """Show every list, with how many films each holds.

    Returns:
        One row per list, ordered by size.
    """
    rows = db.query(
        """
        SELECT l.slug,
               l.name,
               count(e.entry_position) AS films,
               l.ranked,
               l.source,
               l.created_date
        FROM lists l
        LEFT JOIN list_entries e ON e.list_id = l.list_id
        GROUP BY ALL
        ORDER BY films DESC, l.name
        """
    )
    if not rows:
        raise NoResultError(
            "No lists yet. Import a Letterboxd export, which includes any "
            "lists you have made."
        )
    return rows


@plugin(category="lists")
def set_list_ranked(slug: str, ranked: bool = True) -> dict[str, Any]:
    """Mark a list as ranked, so its positions are treated as an ordering.

    Letterboxd's export gives every list a Position column whether or not the
    list is actually ranked, and exports no flag saying which is which -- all
    list files are structurally identical. So this cannot be detected on
    import; it is something you tell the tool once per list.

    Positions are always stored, so marking a list ranked later loses nothing.

    Args:
        slug: The list's slug, as shown by the `lists` command. This is the
            export's filename, e.g. "wes-anderson-ranked".
        ranked: Whether the list's positions are a deliberate ordering. Pass
            false to unset it again.

    Returns:
        The list, with its new state.

    Raises:
        NoResultError: If no list has that slug.
    """
    updated = db.query(
        "UPDATE lists SET ranked = ? WHERE slug = ? RETURNING slug, name, ranked",
        [ranked, slug],
    )
    if not updated:
        known = db.query("SELECT slug FROM lists ORDER BY slug LIMIT 10")
        suggestions = ", ".join(row["slug"] for row in known) or "none"
        raise NoResultError(
            f"No list with slug {slug!r}. Known slugs include: {suggestions}."
        )
    return updated[0]
