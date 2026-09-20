"""Neoffice — the order of the tool list decides the whole fleet's prefix cache.

The engine's template renders the TOOLS FIRST, then the system text, so the tool list
is the prefix. The cache is addressed by content and knows nothing about which instance
is asking — every instance shares the desk prefix, as long as the list comes out in the
same order, serialized the same way, everywhere and on every call.

Measured by the provider on a desk-shaped request (24 tools, ~16k tokens):

    same request again .............. 97.0 % cached, 0.65 s
    only the date differs ........... 88.7 %
    a DIFFERENT instance ............ 91.5 %
    two tools swapped ................ 0.0 %, 18.97 s

Twenty-nine times slower, for an order.

What makes it hold today is one word: ``registry.get_definitions`` iterates
``sorted(tool_names)``. It has to, because what reaches it is a SET —
``_select_tool_names`` builds one — and a set of strings iterates in hash order, which
Python randomizes per process. Without the sort the fleet would lose its shared prefix
on every gateway restart, not merely when a tool is added.

It matters more than it looks: MCP servers connect in PARALLEL
(mcp_tool_registration.py, "ownership pre-check is advisory (servers connect in
parallel)"), so registration order is genuinely non-deterministic. The sort is what
makes that irrelevant.

These guards exist because that is one word in one line, and this session has already
seen a one-line injection disappear twice across ports.
"""
from contextlib import contextmanager
from pathlib import Path

from tools.registry import registry


@contextmanager
def trois_outils():
    """Register three tools in REVERSE alphabetical order, then clean up.

    A context manager rather than a pytest fixture, so these run under any runner —
    including the bare one used to check this branch on the server.
    """
    noms = ["zz_neoffice_order_probe", "mm_neoffice_order_probe", "aa_neoffice_order_probe"]
    for nom in noms:
        registry.register(
            name=nom, toolset="neoffice_order_probe",
            schema={"description": "probe", "parameters": {"type": "object", "properties": {}}},
            handler=lambda **_: "",
        )
    try:
        yield noms
    finally:
        for nom in noms:
            registry.deregister(nom)


def test_the_definitions_come_out_alphabetical_whatever_the_registration_order():
    """Registered z, m, a — served a, m, z. MCP servers connect in parallel, so the
    order they register in is not something we control; this is what makes it moot."""
    with trois_outils() as noms:
        sortis = [d["function"]["name"] for d in registry.get_definitions(set(noms), quiet=True)]
        assert sortis == sorted(noms), sortis


def test_the_input_collection_order_cannot_change_the_output():
    """A set, a reversed list and a sorted list must all yield the same bytes."""
    import json

    with trois_outils() as noms:
        formes = [set(noms), list(reversed(noms)), sorted(noms)]
        rendus = {json.dumps(registry.get_definitions(f, quiet=True)) for f in formes}
        assert len(rendus) == 1, "the caller's collection order leaked into the prefix"


def test_the_sort_is_still_in_the_registry():
    """The behavioural tests above pass even WITHOUT the sort: a set iterates in a
    stable order within one process, so removing it would go unnoticed here and show
    up only as a fleet-wide cache miss after a restart. So read the line."""
    source = Path(registry.__class__.__module__.replace(".", "/") + ".py")
    if not source.exists():  # packaged differently
        import tools.registry as module

        source = Path(module.__file__)
    texte = source.read_text(encoding="utf-8")
    assert "for name in sorted(tool_names):" in texte, (
        "get_definitions no longer sorts: the tool list becomes hash-ordered, which "
        "Python randomizes per process, and every instance loses the shared prefix at "
        "each gateway restart"
    )
