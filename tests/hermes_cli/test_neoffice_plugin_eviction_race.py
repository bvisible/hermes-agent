# //// Neoffice — added file (no upstream equivalent): regression test for the sys.modules race of
# //// hermes_cli.plugins_loader._evict_modules (see the marked block there).
"""A plugin load must not fail because another thread imports a module at the same moment.

On osiris, after the v0.21.6 merge, a different plugin failed to load one pole start in nine with
« dictionary changed size during iteration »: _evict_modules walked sys.modules with a comprehension while
another thread (the memory prefetch, the MCP discovery) imported modules. Here a thread imports hundreds of
small modules, so sys.modules only grows, while the main thread evicts as a plugin load does.
"""

import importlib
import sys
import threading

import pytest

from hermes_cli.plugins_loader import _evict_modules

MODULES = 600


@pytest.fixture
def importing_thread(tmp_path, monkeypatch):
    for i in range(MODULES):
        (tmp_path / f"_neoffice_race_{i}.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # a thread switch inside the walk, as often as the interpreter allows
    thread = threading.Thread(
        target=lambda: [importlib.import_module(f"_neoffice_race_{i}") for i in range(MODULES)], daemon=True
    )
    yield thread
    thread.join(timeout=30)
    sys.setswitchinterval(before)
    for name in [n for n in list(sys.modules) if n.startswith("_neoffice_race_")]:
        sys.modules.pop(name, None)


def test_evicting_a_plugin_while_another_thread_imports_never_raises(importing_thread):
    raised, calls = [], 0
    importing_thread.start()
    while importing_thread.is_alive() and not raised:
        try:
            _evict_modules("hermes_plugins.neoffice_race_target")
        except RuntimeError as exc:
            raised.append(exc)
        calls += 1
    assert raised == [], f"raised after {calls} evictions: {raised[0]}"
    assert calls > 20, "the importing thread finished before the walk was really exercised"


def test_evicting_drops_the_module_and_its_submodules_only():
    import types

    keep = types.ModuleType("hermes_plugins.neoffice_race_keep")
    drop = types.ModuleType("hermes_plugins.neoffice_race_t")
    sub = types.ModuleType("hermes_plugins.neoffice_race_t.sub")
    same_prefix = types.ModuleType("hermes_plugins.neoffice_race_tt")
    for module in (keep, drop, sub, same_prefix):
        sys.modules[module.__name__] = module
    try:
        _evict_modules("hermes_plugins.neoffice_race_t")
        assert "hermes_plugins.neoffice_race_t" not in sys.modules
        assert "hermes_plugins.neoffice_race_t.sub" not in sys.modules
        assert "hermes_plugins.neoffice_race_tt" in sys.modules
        assert "hermes_plugins.neoffice_race_keep" in sys.modules
    finally:
        for name in ("hermes_plugins.neoffice_race_keep", "hermes_plugins.neoffice_race_tt"):
            sys.modules.pop(name, None)
