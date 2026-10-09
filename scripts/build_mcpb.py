"""Pack the MCP server into a Claude Desktop extension (`.mcpb`).

A `.mcpb` is a zip holding `manifest.json` and what the manifest runs. Ours
uses the uv runtime, so it carries the project's source and lockfile rather
than its dependencies, and Claude Desktop builds the environment on first
start.

The official `mcpb pack` would do, but needs Node; this needs nothing beyond
the standard library. It also packs from a list of what to include rather
than a list of what to leave out, so a user's `.env`, library or Letterboxd
export can never end up inside a bundle they share.

    uv run python scripts/build_mcpb.py            # -> dist/projectsummer-<version>.mcpb
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Top-level files the extension needs: the manifest, and what `uv run`
#: needs to build the project (hatchling reads README and LICENSE).
FILES = ("manifest.json", "pyproject.toml", "uv.lock", "README.md", "LICENSE")

#: Directories packed whole, apart from bytecode.
TREES = ("src/projectsummer",)


def bundle_files(root: Path = ROOT) -> list[Path]:
    """Every file that goes into the bundle, relative to `root`."""
    files = [Path(name) for name in FILES]
    for tree in TREES:
        for path in sorted((root / tree).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files.append(path.relative_to(root))
    missing = [str(path) for path in files if not (root / path).is_file()]
    if missing:
        raise SystemExit(f"Cannot pack: missing {', '.join(missing)}")
    return files


def check_versions(root: Path = ROOT) -> str:
    """Return the version, refusing a manifest that disagrees with pyproject.

    Claude Desktop shows the manifest's version, and pip the package's; two
    numbers for one release would leave nobody sure which they have.
    """
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if manifest["version"] != project["version"]:
        raise SystemExit(
            f"manifest.json says {manifest['version']} but pyproject.toml says "
            f"{project['version']}; make them agree before packing."
        )
    return manifest["version"]


def build(output: Path | None = None, root: Path = ROOT) -> Path:
    """Write the bundle and return its path."""
    version = check_versions(root)
    output = output or root / "dist" / f"projectsummer-{version}.mcpb"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in bundle_files(root):
            # Forward slashes whatever the platform: the zip format requires them.
            bundle.write(root / path, path.as_posix())
    return output


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Pack the MCP server as a Claude Desktop extension.")
    parser.add_argument("-o", "--output", type=Path, help="Where to write the .mcpb. Defaults to dist/.")
    output = build(parser.parse_args(argv).output)
    print(f"Wrote {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
