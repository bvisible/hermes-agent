# //// Neoffice — added file (no upstream equivalent): NORA's memory provider stays the in-tree copy.
"""NORA's memory is our in-tree mem0 provider, whole, and upstream's catalog migration leaves it alone.

Upstream v0.21.6 removed plugins/memory/mem0 from core (its catalog points at Mem0's own plugin) and
memory_provider_migration installs that copy for any home whose memory.provider is "mem0" when the
provider is not found. Our copy carries NORA's memory (company scope, raw capture, retain_facts,
ownership checks, filler filter), so it stays in the fork. Merging v0.21.6 deleted four of its files
WITHOUT a conflict (we had never modified them): _setup, _oss_providers, _openai_llm and plugin.yaml,
and every test still passed because they use a fake backend. These pin the whole provider.
"""
import importlib
from pathlib import Path

import pytest

from hermes_cli import memory_provider_migration
from plugins.memory import find_provider_dir

MODULES = ("plugins.memory.mem0", "plugins.memory.mem0._backend", "plugins.memory.mem0._setup",
           "plugins.memory.mem0._oss_providers", "plugins.memory.mem0._openai_llm")


def test_mem0_resolves_to_the_bundled_copy():
    found = find_provider_dir("mem0")
    assert found is not None and found.parts[-3:] == ("plugins", "memory", "mem0")
    assert (found / "plugin.yaml").exists()


@pytest.mark.parametrize("module", MODULES)
def test_every_module_of_the_provider_imports(module):
    importlib.import_module(module)


def test_the_catalog_migration_sees_the_provider_present(tmp_path: Path):
    assert memory_provider_migration.provider_present("mem0", tmp_path)
# //// END Neoffice ////
