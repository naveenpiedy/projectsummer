"""Plugin registry.

Every feature is written exactly once, as a plain function with type hints and
a docstring, and decorated with :func:`plugin`. The CLI and the MCP server both
build themselves from this registry -- neither one hand-writes a schema, and
neither can drift from the other.

    @plugin(category="discovery")
    def random_watchlist_pick(genre: str | None = None) -> dict:
        '''Pick a random film from the watchlist.'''

The decorator returns the function untouched, so a plugin stays an ordinary
function: importable, directly callable, and testable without the registry.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import os
import pkgutil
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

#: Package scanned for built-in plugins.
BUILTIN_PACKAGE = "projectsummer.core.plugins"

#: Environment variable listing extra directories to load plugins from.
PLUGIN_PATH_ENV = "LETTERBOXD_PLUGIN_PATH"


@dataclass(frozen=True, slots=True)
class Plugin:
    """A registered feature function plus the metadata both frontends need."""

    name: str
    func: Callable[..., Any]
    category: str
    #: First line of the docstring -- a CLI help string / MCP tool summary.
    summary: str
    #: Full docstring, sections and all. What an LLM reads over MCP.
    description: str
    #: Per-parameter help, parsed from the docstring's Args: section.
    #: Becomes `--option` help in the CLI and argument descriptions over MCP.
    param_help: Mapping[str, str]
    #: Docstring with Args:/Returns:/Raises: stripped, for frontends that
    #: render parameters themselves and would otherwise repeat them.
    help_text: str
    #: True if this plugin starts something that must outlive the call -- a
    #: server, say. The CLI has to keep the process alive afterwards or the
    #: thing it started dies the moment the command returns.
    serves: bool = False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)

    @property
    def signature(self) -> inspect.Signature:
        """The function's signature, with string annotations resolved.

        Plugin modules use ``from __future__ import annotations``, so hints
        arrive as strings like ``'str | None'``. Both frontends need real
        types to build a schema, so evaluate them here rather than twice.
        """
        try:
            return inspect.signature(self.func, eval_str=True)
        except (NameError, TypeError):
            # A hint that only exists under TYPE_CHECKING; better an
            # unresolved annotation than no signature at all.
            return inspect.signature(self.func)


@dataclass(frozen=True, slots=True)
class LoadFailure:
    """A plugin file that could not be imported.

    Recorded rather than raised, so that one bad third-party file cannot take
    down the whole tool. Frontends read :func:`load_failures` and decide how to
    surface these; the registry itself stays quiet, as a library should.
    """

    #: Path or module name that failed.
    source: str
    error: Exception

    def __str__(self) -> str:
        return f"{self.source}: {type(self.error).__name__}: {self.error}"


_REGISTRY: dict[str, Plugin] = {}
_LOAD_FAILURES: list[LoadFailure] = []


class PluginError(Exception):
    """Raised when a plugin cannot be registered."""


def plugin(
    name: str | None = None,
    category: str = "general",
    serves: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a plugin.

    Args:
        name: Command/tool name. Defaults to the function's own name.
        category: Grouping used for CLI help and tool organisation.
        serves: Set for a plugin that starts a server or other background
            work which must outlive the call. The CLI keeps running until
            interrupted instead of exiting and tearing it down.

    Returns:
        A decorator that registers the function and returns it unchanged.

    Raises:
        PluginError: If the name is already taken, the function has no
            docstring, or any parameter lacks a type annotation. All three are
            fatal for the auto-generated frontends, so they fail at import
            time rather than producing a subtly broken CLI command or tool.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        plugin_name = name or func.__name__
        _validate(plugin_name, func)

        existing = _REGISTRY.get(plugin_name)
        if existing is not None and not _is_same_definition(existing.func, func):
            raise PluginError(
                f"Plugin name {plugin_name!r} is already registered by "
                f"{existing.func.__module__}.{existing.func.__qualname__}."
            )

        doc = inspect.cleandoc(func.__doc__ or "")
        summary, help_text, param_help = _parse_docstring(doc)
        _REGISTRY[plugin_name] = Plugin(
            name=plugin_name,
            func=func,
            category=category,
            summary=summary,
            description=doc,
            param_help=MappingProxyType(param_help),
            help_text=help_text,
            serves=serves,
        )
        return func

    return decorator


#: Docstring section headers recognised when splitting help from parameters.
_SECTIONS = frozenset(
    {"Args", "Arguments", "Parameters", "Returns", "Yields", "Raises", "Examples", "Example", "Note", "Notes"}
)

_PARAM_RE = re.compile(r"^(?P<name>\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(?P<desc>.*)$")


def _parse_docstring(doc: str) -> tuple[str, str, dict[str, str]]:
    """Split a Google-style docstring into summary, prose and per-parameter help.

    Written once here so the CLI and the MCP server describe a plugin's
    parameters identically, from the same source: the docstring the author
    already wrote.

    Returns:
        ``(summary, help_text, param_help)`` -- the first line, the prose with
        every ``Section:`` block removed, and a name-to-description mapping
        for the parameters documented under ``Args:``.
    """
    lines = doc.splitlines()
    summary = lines[0].strip() if lines else ""

    prose: list[str] = []
    collected: dict[str, list[str]] = {}
    in_args = False
    in_section = False
    base_indent: int | None = None
    current: str | None = None

    for line in lines:
        stripped = line.strip()
        header = stripped[:-1] if stripped.endswith(":") else None

        if header in _SECTIONS and not line[:1].isspace():
            in_args = header in ("Args", "Arguments", "Parameters")
            in_section = True
            base_indent = None
            current = None
            continue

        if in_args:
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            if base_indent is None:
                base_indent = indent
            match = _PARAM_RE.match(stripped) if indent <= base_indent else None
            if match:
                current = match["name"].lstrip("*")
                collected[current] = [match["desc"].strip()]
            elif current is not None:
                collected[current].append(stripped)
            continue

        if not in_section:
            prose.append(line)

    param_help = {
        name: " ".join(part for part in parts if part).strip()
        for name, parts in collected.items()
    }
    return summary, "\n".join(prose).strip(), param_help


def _is_same_definition(old: Callable[..., Any], new: Callable[..., Any]) -> bool:
    """Is `new` a reloaded version of `old`, rather than a rival definition?

    Module reload produces a fresh function object for the same source, so
    identity alone would flag a reload as a name clash.
    """
    return old is new or (
        old.__module__ == new.__module__ and old.__qualname__ == new.__qualname__
    )


def _validate(plugin_name: str, func: Callable[..., Any]) -> None:
    """Fail loudly on the mistakes that break auto-generated frontends."""
    if not func.__doc__ or not func.__doc__.strip():
        raise PluginError(
            f"Plugin {plugin_name!r} needs a docstring: it becomes the CLI "
            f"help text and the description an LLM reads to decide when to "
            f"call the tool."
        )

    unannotated = [
        param.name
        for param in inspect.signature(func).parameters.values()
        if param.annotation is inspect.Parameter.empty
        and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
    ]
    if unannotated:
        raise PluginError(
            f"Plugin {plugin_name!r} has unannotated parameter(s): "
            f"{', '.join(unannotated)}. Type hints are the schema."
        )


def all_plugins() -> Mapping[str, Plugin]:
    """Return every registered plugin, keyed by name (read-only)."""
    return MappingProxyType(_REGISTRY)


def get(name: str) -> Plugin:
    """Look up a single plugin by name.

    Raises:
        KeyError: If no plugin is registered under that name.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"No plugin named {name!r}. Registered: {known}") from None


def by_category() -> dict[str, list[Plugin]]:
    """Group plugins by category, for CLI help and tool listings."""
    grouped: dict[str, list[Plugin]] = {}
    for item in _REGISTRY.values():
        grouped.setdefault(item.category, []).append(item)
    for items in grouped.values():
        items.sort(key=lambda p: p.name)
    return dict(sorted(grouped.items()))


def clear() -> None:
    """Empty the registry. Intended for tests."""
    _REGISTRY.clear()


def discover(extra_dirs: list[str | Path] | None = None) -> Mapping[str, Plugin]:
    """Import every plugin module so its decorators run.

    Scans the built-in plugin package, then any directories given in
    `extra_dirs` or in the ``LETTERBOXD_PLUGIN_PATH`` environment variable.
    That second path is how a user adds a feature without touching `core/`.

    A third-party file that fails to import is recorded in
    :func:`load_failures` and skipped, so one broken or half-installed plugin
    cannot stop the tool from starting. Built-in plugins are not forgiven that
    way: they ship with the package, so a failure there is our bug and raises.

    Returns:
        The registry, after all imports have completed.
    """
    _LOAD_FAILURES.clear()

    package = importlib.import_module(BUILTIN_PACKAGE)
    for module in pkgutil.iter_modules(package.__path__):
        if not module.name.startswith("_"):
            _import_registering(f"{BUILTIN_PACKAGE}.{module.name}")

    for directory in _external_dirs(extra_dirs):
        _load_directory(directory)

    return all_plugins()


def load_failures() -> tuple[LoadFailure, ...]:
    """Plugin files skipped during the last :func:`discover` call."""
    return tuple(_LOAD_FAILURES)


def _import_registering(module_name: str) -> None:
    """Import `module_name`, ensuring its decorators have actually run.

    A plain ``import_module`` is a no-op once a module is in ``sys.modules``,
    so after :func:`clear` the plugins would never come back. Reload in that
    case, so `discover()` always means what its name says.
    """
    module = sys.modules.get(module_name)
    if module is None:
        importlib.import_module(module_name)
        return

    already_registered = any(
        item.func.__module__ == module_name for item in _REGISTRY.values()
    )
    if not already_registered:
        importlib.reload(module)


def _external_dirs(extra_dirs: list[str | Path] | None) -> Iterator[Path]:
    for entry in extra_dirs or []:
        yield Path(entry).expanduser()

    raw = os.environ.get(PLUGIN_PATH_ENV, "")
    for entry in raw.split(os.pathsep):
        if entry.strip():
            yield Path(entry.strip()).expanduser()


def _load_directory(directory: Path) -> None:
    """Import every top-level ``*.py`` file in `directory` as a plugin module."""
    if not directory.is_dir():
        return

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            _load_file(path)
        except Exception as error:  # noqa: BLE001 -- third-party code
            _LOAD_FAILURES.append(LoadFailure(source=str(path), error=error))


def _load_file(path: Path) -> None:
    """Import a single plugin file, registering it in ``sys.modules``."""
    module_name = _contrib_module_name(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path} is not importable as a Python module.")

    module = importlib.util.module_from_spec(spec)
    # Register before executing: a module that inspects sys.modules[__name__]
    # during import -- dataclasses and pickle both do -- otherwise fails.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise


def _contrib_module_name(path: Path) -> str:
    """Derive a module name unique to this file's location.

    Keying on the filename alone would give two plugin directories that both
    contain `extra.py` the same module name. Their functions would then share
    a ``__module__`` and ``__qualname__``, which :func:`_is_same_definition`
    reads as a reload -- so the second would silently replace the first
    instead of being reported as a name clash.
    """
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
    stem = re.sub(r"\W", "_", path.stem)
    return f"lbxd_contrib_{stem}_{digest}"
