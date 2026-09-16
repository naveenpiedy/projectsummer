"""Library plugins: getting your Letterboxd data in and keeping it current."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from letterboxd_utility_tools.core.ingest import ingest_export
from letterboxd_utility_tools.core.registry import plugin


@plugin(name="ingest", category="library")
def ingest(export_dir: Path) -> dict[str, Any]:
    """Import a Letterboxd CSV export.

    Download your export from Letterboxd (Settings -> Data -> Export Your
    Data), unzip it, and point this at the resulting directory.

    This loads the raw data only. Films are identified by Letterboxd URIs at
    this stage; run enrichment afterwards to resolve them to TMDB metadata.

    Args:
        export_dir: The unzipped export directory, the one containing
            diary.csv and watchlist.csv.

    Returns:
        Counts of what was loaded, plus any files that were skipped.

    Raises:
        ExportNotFoundError: If the directory holds no Letterboxd CSVs.
    """
    return ingest_export(export_dir).as_dict()
