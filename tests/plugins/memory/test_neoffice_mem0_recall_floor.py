# //// Neoffice — added file (no upstream equivalent): see _neoffice_above_recall_floor in plugins/memory/mem0/__init__.py.
"""The prefetch injects a memory only from a score worth reading (05.11, #2291)."""

import importlib

mem0 = importlib.import_module("plugins.memory.mem0")


def test_an_unrelated_memory_is_not_injected():
    items = [{"memory": "a", "score": 0.73}, {"memory": "b", "score": 0.47}]
    assert [i["memory"] for i in mem0._neoffice_above_recall_floor(items, 0.55)] == ["a"]


def test_a_memory_at_the_floor_stays():
    assert mem0._neoffice_above_recall_floor([{"memory": "a", "score": 0.55}], 0.55) == [{"memory": "a", "score": 0.55}]


def test_a_result_without_a_score_stays():
    assert mem0._neoffice_above_recall_floor([{"memory": "a"}], 0.55) == [{"memory": "a"}]


def test_no_result_is_no_result():
    assert mem0._neoffice_above_recall_floor(None, 0.55) == []
