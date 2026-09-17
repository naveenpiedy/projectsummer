"""Registration, validation and discovery."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import pytest

from projectsummer.core import registry
from projectsummer.core.results import Result


class Echo(Result):
    """A value handed back."""

    value: str
    """What the plugin produced."""


HELLO_PLUGIN = (
    "from projectsummer.core.registry import plugin\n"
    "from projectsummer.core.results import Result\n"
    "\n"
    "class Greeting(Result):\n"
    "    '''A greeting.'''\n"
    "    text: str\n"
    "\n"
    "@plugin(category='contrib')\n"
    "def hello(name: str = 'world') -> Greeting:\n"
    "    '''Say hello.'''\n"
    "    return Greeting(text=f'hello {name}')\n"
)


@pytest.fixture(autouse=True)
def isolated_registry():
    """Give each test an empty registry, then restore the real one."""
    saved = dict(registry.all_plugins())
    registry.clear()
    yield
    registry.clear()
    registry._REGISTRY.update(saved)


def test_registers_under_function_name():
    @registry.plugin()
    def sample_feature(count: int = 1) -> Echo:
        """Do a sample thing."""
        return Echo(value="x" * count)

    assert registry.get("sample_feature").category == "general"
    assert registry.get("sample_feature").summary == "Do a sample thing."


def test_decorated_function_stays_directly_callable():
    @registry.plugin()
    def sample_feature(count: int = 1) -> Echo:
        """Do a sample thing."""
        return Echo(value="x" * count)

    assert sample_feature(3).value == "xxx"          # plain function
    assert registry.get("sample_feature")(3).value == "xxx"  # via the registry


def test_explicit_name_and_category():
    @registry.plugin(name="pick", category="discovery")
    def some_long_internal_name() -> Echo:
        """Pick something."""

    assert "pick" in registry.all_plugins()
    assert registry.by_category()["discovery"][0].name == "pick"


def test_duplicate_name_is_rejected():
    @registry.plugin(name="clash")
    def first() -> Echo:
        """First."""

    with pytest.raises(registry.PluginError, match="already registered"):

        @registry.plugin(name="clash")
        def second() -> Echo:
            """Second."""


def test_missing_docstring_is_rejected():
    with pytest.raises(registry.PluginError, match="needs a docstring"):

        @registry.plugin()
        def undocumented() -> Echo:
            pass


def test_unannotated_parameter_is_rejected():
    with pytest.raises(registry.PluginError, match="unannotated parameter"):

        @registry.plugin()
        def sloppy(genre) -> Echo:
            """Missing a type hint on genre."""


def test_registry_mapping_is_read_only():
    with pytest.raises(TypeError):
        registry.all_plugins()["injected"] = None


def test_unknown_name_lists_what_is_available():
    @registry.plugin(name="known")
    def known() -> Echo:
        """Known."""

    with pytest.raises(KeyError, match="Registered: known"):
        registry.get("nope")


def test_discover_finds_builtin_plugins():
    found = registry.discover()
    assert "random_watchlist_pick" in found


def test_discover_loads_an_external_directory(tmp_path):
    (tmp_path / "contrib.py").write_text(HELLO_PLUGIN, encoding="utf-8")

    found = registry.discover(extra_dirs=[tmp_path])
    assert found["hello"]("there").text == "hello there"
    assert found["hello"].category == "contrib"


def test_discover_is_repeatable():
    first = dict(registry.discover())
    second = dict(registry.discover())
    assert first.keys() == second.keys()
    assert "random_watchlist_pick" in second


def test_discover_reloads_external_directory_without_clashing(tmp_path):
    (tmp_path / "contrib.py").write_text(HELLO_PLUGIN, encoding="utf-8")

    registry.discover(extra_dirs=[tmp_path])
    registry.discover(extra_dirs=[tmp_path])  # must not raise a name clash
    assert "hello" in registry.all_plugins()


# ------------------------------------------- resilience of external loading

def _write_plugin(directory, filename, func_name, returns):
    (directory / filename).write_text(
        f"from projectsummer.core.registry import plugin\n"
        f"from projectsummer.core.results import Result\n"
        f"\n"
        f"class Returned(Result):\n"
        f"    '''What came back.'''\n"
        f"    value: str\n"
        f"\n"
        f"@plugin(category='contrib')\n"
        f"def {func_name}() -> Returned:\n"
        f"    '''Return {returns}.'''\n"
        f"    return Returned(value={returns!r})\n",
        encoding="utf-8",
    )


def test_a_broken_plugin_file_does_not_stop_the_others(tmp_path):
    (tmp_path / "broken.py").write_text("this is not valid python\n", encoding="utf-8")
    _write_plugin(tmp_path, "good.py", "still_here", "ok")

    found = registry.discover(extra_dirs=[tmp_path])

    assert "still_here" in found, "a broken sibling must not block a valid plugin"
    assert found["still_here"]().value == "ok"


def test_a_broken_plugin_file_is_reported(tmp_path):
    (tmp_path / "broken.py").write_text("this is not valid python\n", encoding="utf-8")

    registry.discover(extra_dirs=[tmp_path])

    failures = registry.load_failures()
    assert len(failures) == 1
    assert "broken.py" in failures[0].source
    assert "SyntaxError" in str(failures[0])


def test_a_plugin_importing_a_missing_package_is_skipped(tmp_path):
    (tmp_path / "needy.py").write_text("import a_package_nobody_has\n", encoding="utf-8")
    _write_plugin(tmp_path, "good.py", "still_here", "ok")

    found = registry.discover(extra_dirs=[tmp_path])

    assert "still_here" in found
    assert "ModuleNotFoundError" in str(registry.load_failures()[0])


def test_load_failures_reset_between_discoveries(tmp_path):
    (tmp_path / "broken.py").write_text("nope nope\n", encoding="utf-8")
    registry.discover(extra_dirs=[tmp_path])
    assert registry.load_failures()

    registry.discover()  # builtins only
    assert registry.load_failures() == ()


def test_same_filename_in_two_directories_is_a_real_clash(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _write_plugin(first, "extra.py", "shared_name", "from-a")
    _write_plugin(second, "extra.py", "shared_name", "from-b")

    registry.discover(extra_dirs=[first, second])

    # The first one wins and the second is reported -- it must not silently
    # replace it just because both files happen to be called extra.py.
    assert registry.get("shared_name")().value == "from-a"
    assert any("already registered" in str(f) for f in registry.load_failures())


def test_contrib_modules_land_in_sys_modules(tmp_path):
    import sys

    _write_plugin(tmp_path, "gadget.py", "gadget", "ok")
    registry.discover(extra_dirs=[tmp_path])

    module_name = registry.get("gadget").func.__module__
    assert module_name in sys.modules
    assert sys.modules[module_name].__file__ == str(tmp_path / "gadget.py")


def test_a_broken_builtin_plugin_is_not_forgiven(monkeypatch):
    """Built-ins ship with the package; a failure there is our bug, not theirs."""
    def explode(name):
        raise ImportError("boom")

    monkeypatch.setattr(registry, "_import_registering", explode)
    with pytest.raises(ImportError, match="boom"):
        registry.discover()


# ------------------------------------------------------------- result types
#
# The return type is the tool's output schema. Pydantic will happily build a
# schema from `Any` or `dict[str, Any]`; it just says nothing a caller can use.

class Film(Result):
    """A film."""

    title: str
    genres: list[str] | None


class Detailed(Result):
    """Everything a well-described result may hold."""

    films: list[Film]
    counts: dict[str, int]
    state: Literal["empty", "ready"]
    seen_on: date | None
    ratio: float


class Vague(Result):
    """Holds something unspecified."""

    rows: list[dict[str, Any]]


class Bare(Result):
    """Holds a bare list."""

    items: list


class Outer(Result):
    """Nests something vague."""

    inner: Vague


def test_a_fully_described_result_is_accepted():
    @registry.plugin()
    def detailed() -> Detailed:
        """Return detail."""

    assert registry.get("detailed").result_type is Detailed


@pytest.mark.parametrize("returns", ["str", "dict", "None"])
def test_a_result_that_is_not_a_model_is_rejected(returns):
    namespace: dict[str, Any] = {"registry": registry}
    source = (
        "@registry.plugin()\n"
        f"def loose() -> {returns}:\n"
        "    '''Return something loose.'''\n"
    )
    with pytest.raises(registry.PluginError, match="must return a Pydantic model"):
        exec(source, namespace)


def test_a_missing_return_type_is_rejected():
    with pytest.raises(registry.PluginError, match="returns nothing"):

        @registry.plugin()
        def unsaid():
            """Return who knows."""


def test_any_inside_a_result_is_rejected_and_named():
    with pytest.raises(registry.PluginError, match=r"Vague\.rows is Any"):

        @registry.plugin()
        def vague() -> Vague:
            """Return rows of anything."""


def test_a_bare_list_is_rejected():
    with pytest.raises(registry.PluginError, match=r"Bare\.items is list"):

        @registry.plugin()
        def bare() -> Bare:
            """Return a bare list."""


def test_vagueness_is_found_in_nested_models():
    with pytest.raises(registry.PluginError, match=r"Vague\.rows"):

        @registry.plugin()
        def outer() -> Outer:
            """Return a nested vague result."""


# ----------------------------------------------------------------- exposure
#
# Whether MCP offers a plugin is settled here, once. The server registers only
# plugins with `mcp` set, so this is the whole of the rule.

def test_read_plugins_are_offered_over_mcp_by_default():
    @registry.plugin()
    def reader() -> Echo:
        """Read."""

    assert registry.get("reader").access == "read"
    assert registry.get("reader").mcp is True


def test_write_plugins_are_not_offered_unless_opted_in():
    @registry.plugin(access="write")
    def writer() -> Echo:
        """Write."""

    @registry.plugin(access="write", mcp=True)
    def opted_in() -> Echo:
        """Write, deliberately exposed."""

    assert registry.get("writer").mcp is False
    assert registry.get("opted_in").mcp is True


def test_a_read_plugin_can_be_withheld():
    @registry.plugin(mcp=False)
    def private() -> Echo:
        """Read, but not over MCP."""

    assert registry.get("private").mcp is False


def test_a_serving_plugin_is_never_offered_by_default():
    @registry.plugin(serves=True)
    def server() -> Echo:
        """Serve."""

    assert registry.get("server").mcp is False


def test_a_serving_plugin_cannot_be_opted_in():
    with pytest.raises(registry.PluginError, match="cannot be offered over MCP"):

        @registry.plugin(serves=True, mcp=True)
        def server() -> Echo:
            """Serve."""


def test_an_unknown_access_level_is_rejected():
    with pytest.raises(registry.PluginError, match="access="):

        @registry.plugin(access="admin")
        def odd() -> Echo:
            """Odd."""


def test_exactly_these_builtins_are_offered_over_mcp():
    """Pinned on purpose. Exposing a plugin to agents should take a deliberate
    change to this test, not happen as a side effect of editing a decorator."""
    found = registry.discover()
    offered = {name: item.access for name, item in found.items() if item.mcp}
    assert offered == {
        "describe_schema": "read",
        "lists": "read",
        "overview": "read",
        "query": "read",
        "random_watchlist_pick": "read",
        "set_list_ranked": "write",
        "sync": "write",
        "list_builder": "write",
        "trends": "read",
    }
