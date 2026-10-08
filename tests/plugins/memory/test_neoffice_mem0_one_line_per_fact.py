# //// Neoffice — added file (no upstream equivalent): the recall injects each fact once.
"""The prefetch injects each FACT once, and a restated fact is not stored again.

Read on osiris on 08.10: the per-turn capture stores NORA's answer whole, so each time she restated
a recalled fact it was stored again (up to four copies of one sentence in a bucket), and every
copy took one of the ten lines the prefetch injects into the prompt. The prefetch now keeps one
line per fact; mem0_search and mem0_list still return every copy, so a correction or a « forget »
reaches them all; and sync_turn does not store an answer that only restates what was recalled.
"""
import json

from plugins.memory.mem0 import Mem0MemoryProvider

FACT = "Notre fiduciaire est la Fiduciaire Exemple SA, à Lausanne."
SAME_FACT = "notre fiduciaire est la fiduciaire exemple SA à Lausanne"
OTHER = "Le client paie ses factures à 30 jours."


class Backend:
    def __init__(self, results):
        self.results = results
        self.adds = []

    def search(self, query, *, filters, top_k=10, rerank=True):
        return [dict(r) for r in self.results]

    def get_all(self, *, filters, page=1, page_size=100):
        return {"results": [dict(r) for r in self.results], "count": len(self.results)}

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.adds.append([m["content"] for m in messages])
        return {"event_id": "ev-1"}


def _provider(results):
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id = "u123"
    provider._agent_id = "hermes"
    provider._backend = Backend(results)
    return provider


RESULTS = [{"id": "m1", "memory": FACT, "score": 0.9}, {"id": "m2", "memory": SAME_FACT, "score": 0.8},
           {"id": "m3", "memory": OTHER, "score": 0.7}]


def test_the_prefetch_injects_each_fact_once():
    body = _provider(RESULTS).prefetch("Comment s'appelle notre fiduciaire ?")
    lines = [line for line in body.splitlines() if line.startswith("- ")]
    assert len(lines) == 2, body
    assert FACT in body and OTHER in body


def test_search_still_returns_every_copy_so_a_correction_reaches_them_all():
    provider = _provider(RESULTS)
    out = json.loads(provider.handle_tool_call("mem0_search", {"query": "fiduciaire"}))
    assert len(out["results"]) == 3


def _sync(provider, user, assistant):
    provider.sync_turn(user, assistant)
    provider._sync_thread.join(timeout=5)
    return provider._backend.adds


def test_an_answer_that_restates_a_recalled_fact_is_not_stored_again():
    provider = _provider(RESULTS)
    provider.prefetch("Où est notre fiduciaire ?")
    adds = _sync(provider, "Où est notre fiduciaire, déjà ?", FACT)
    assert adds == [["Où est notre fiduciaire, déjà ?"]]


def test_a_new_answer_is_stored_as_before():
    provider = _provider(RESULTS)
    provider.prefetch("Où est notre fiduciaire ?")
    adds = _sync(provider, "Et notre banque ?", "Votre banque est la Banque Exemple, à Genève.")
    assert adds == [["Et notre banque ?", "Votre banque est la Banque Exemple, à Genève."]]
# //// END Neoffice ////
