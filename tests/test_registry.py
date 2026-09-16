"""Registration, validation and discovery."""

from __future__ import annotations

import pytest

from projectsummer.core import registry


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
        "from projectsummer.core.registry import plugin\n"
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
        "from projectsummer.core.registry import plugin\n"
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


# ------------------------------------------- resilience of external loading

def _write_plugin(directory, filename, func_name, returns):
    (directory / filename).write_text(
        f"from projectsummer.core.registry import plugin\n"
        f"\n"
        f"@plugin(category='contrib')\n"
        f"def {func_name}() -> str:\n"
        f"    '''Return {returns}.'''\n"
        f"    return {returns!r}\n",
        encoding="utf-8",
    )


def test_a_broken_plugin_file_does_not_stop_the_others(tmp_path):
    (tmp_path / "broken.py").write_text("this is not valid python\n", encoding="utf-8")
    _write_plugin(tmp_path, "good.py", "still_here", "ok")

    found = registry.discover(extra_dirs=[tmp_path])

    assert "still_here" in found, "a broken sibling must not block a valid plugin"
    assert found["still_here"]() == "ok"


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
    assert registry.get("shared_name")() == "from-a"
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
