"""Library plugins: getting your Letterboxd data in and keeping it current."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectsummer.core.ingest import ingest_export
from projectsummer.core.registry import plugin


@plugin(name="ingest", category="library")
def ingest(export_path: Path) -> dict[str, Any]:
    """Import a Letterboxd CSV export.

    Download your export from Letterboxd (Settings -> Data -> Export Your
    Data) and point this straight at the .zip -- there is no need to unzip it
    first. An already-unzipped directory works too.

    This loads the raw data only. Films are identified by Letterboxd URIs at
    this stage; run enrichment afterwards to resolve them to TMDB metadata.

    Args:
        export_path: Your export .zip, or an unzipped directory containing
            diary.csv and watchlist.csv.

    Returns:
        Counts of what was loaded, plus any files that were skipped.

    Raises:
        ExportNotFoundError: If the path holds no Letterboxd CSVs.
    """
    return ingest_export(export_path).as_dict()
