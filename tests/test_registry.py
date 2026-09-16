"""Registration, validation and discovery."""

from __future__ import annotations

import pytest

from letterboxd_utility_tools.core import registry


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
    def sample_feature(count: int = 1) -> str:
        """Do a sample thing."""
        return "x" * count

    assert registry.get("sample_feature").category == "general"
    assert registry.get("sample_feature").summary == "Do a sample thing."


def test_decorated_function_stays_directly_callable():
    @registry.plugin()
    def sample_feature(count: int = 1) -> str:
        """Do a sample thing."""
        return "x" * count

    assert sample_feature(3) == "xxx"          # plain function
    assert registry.get("sample_feature")(3) == "xxx"  # via the registry


def test_explicit_name_and_category():
    @registry.plugin(name="pick", category="discovery")
    def some_long_internal_name() -> None:
        """Pick something."""

    assert "pick" in registry.all_plugins()
    assert registry.by_category()["discovery"][0].name == "pick"


def test_duplicate_name_is_rejected():
    @registry.plugin(name="clash")
    def first() -> None:
        """First."""

    with pytest.raises(registry.PluginError, match="already registered"):

        @registry.plugin(name="clash")
        def second() -> None:
            """Second."""


def test_missing_docstring_is_rejected():
    with pytest.raises(registry.PluginError, match="needs a docstring"):

        @registry.plugin()
        def undocumented() -> None:
            pass


def test_unannotated_parameter_is_rejected():
    with pytest.raises(registry.PluginError, match="unannotated parameter"):

        @registry.plugin()
        def sloppy(genre) -> None:
            """Missing a type hint on genre."""


def test_registry_mapping_is_read_only():
    with pytest.raises(TypeError):
        registry.all_plugins()["injected"] = None


def test_unknown_name_lists_what_is_available():
    @registry.plugin(name="known")
    def known() -> None:
        """Known."""

    with pytest.raises(KeyError, match="Registered: known"):
        registry.get("nope")


def test_discover_finds_builtin_plugins():
    found = registry.discover()
    assert "random_watchlist_pick" in found


def test_discover_loads_an_external_directory(tmp_path):
    (tmp_path / "contrib.py").write_text(
        "from letterboxd_utility_tools.core.registry import plugin\n"
        "\n"
        "@plugin(category='contrib')\n"
        "def hello(name: str = 'world') -> str:\n"
        "    '''Say hello.'''\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )

    found = registry.discover(extra_dirs=[tmp_path])
    assert found["hello"]("there") == "hello there"
    assert found["hello"].category == "contrib"


def test_discover_is_repeatable():
    first = dict(registry.discover())
    second = dict(registry.discover())
    assert first.keys() == second.keys()
    assert "random_watchlist_pick" in second


def test_discover_reloads_external_directory_without_clashing(tmp_path):
    (tmp_path / "contrib.py").write_text(
        "from letterboxd_utility_tools.core.registry import plugin\n"
        "\n"
        "@plugin(category='contrib')\n"
        "def hello(name: str = 'world') -> str:\n"
        "    '''Say hello.'''\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )

    registry.discover(extra_dirs=[tmp_path])
    registry.discover(extra_dirs=[tmp_path])  # must not raise a name clash
    assert "hello" in registry.all_plugins()
