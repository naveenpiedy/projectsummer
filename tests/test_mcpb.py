"""The Claude Desktop extension: its manifest, and what the bundle carries.

Claude Desktop is not run here, so these pin what it relies on instead: the
command the manifest starts must exist as a console script, every setting it
passes must be one `config.py` reads, and a bundle must never carry a user's
own files.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
import zipfile
from pathlib import Path

import pytest

from projectsummer import config

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

_spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "scripts" / "build_mcpb.py")
build_mcpb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_mcpb)


def test_the_manifest_and_the_package_agree_on_the_version():
    assert MANIFEST["version"] == PROJECT["version"]


def test_the_manifest_starts_the_mcp_console_script_with_its_extra():
    args = MANIFEST["server"]["mcp_config"]["args"]
    assert args[-1] in PROJECT["scripts"]
    assert PROJECT["scripts"][args[-1]] == "projectsummer.mcp_server:main"
    assert args[args.index("--extra") + 1] in PROJECT["optional-dependencies"]
    assert (ROOT / MANIFEST["server"]["entry_point"]).is_file()


def test_every_setting_passed_is_one_the_code_reads():
    env = MANIFEST["server"]["mcp_config"]["env"]
    assert set(env) == {config.TMDB_TOKEN_ENV, config.DB_PATH_ENV, config.MCP_READ_ONLY_ENV}


def test_every_setting_has_a_default():
    """Claude Desktop leaves `${user_config.x}` unreplaced when x has no value,
    so an optional setting with no default would reach us as that literal text
    -- a library path of `${user_config.database}`."""
    used = set()
    for value in MANIFEST["server"]["mcp_config"]["env"].values():
        used.update(re.findall(r"\$\{user_config\.(\w+)\}", value))
    assert used == set(MANIFEST["user_config"])
    for key, option in MANIFEST["user_config"].items():
        assert "default" in option, key


def test_the_bundle_holds_the_project_and_nothing_personal(tmp_path):
    output = build_mcpb.build(tmp_path / "out.mcpb")
    with zipfile.ZipFile(output) as bundle:
        names = set(bundle.namelist())

    assert {"manifest.json", "pyproject.toml", "uv.lock", "README.md", "LICENSE"} <= names
    assert "src/projectsummer/mcp_server.py" in names
    assert "src/projectsummer/core/schema.sql" in names
    assert all("\\" not in name for name in names)
    assert not [name for name in names if "__pycache__" in name or name.endswith(".pyc")]
    personal = re.compile(r"(^|/)\.env$|\.duckdb|\.zip$|(^|/)\.venv/|(^|/)tests/")
    assert not [name for name in names if personal.search(name)]


def test_a_manifest_that_disagrees_with_pyproject_is_not_packed(tmp_path):
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({**MANIFEST, "version": "0.0.1"}), encoding="utf-8")

    with pytest.raises(SystemExit, match="0.0.1"):
        build_mcpb.check_versions(tmp_path)
