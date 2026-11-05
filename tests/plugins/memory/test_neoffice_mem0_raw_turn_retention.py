# //// Neoffice — added file (no upstream equivalent): see _neoffice_raw_turns_past in plugins/memory/mem0/__init__.py.
"""A raw turn past its window is removed, a fact never (05.11, #2291)."""

import importlib
from datetime import datetime, timezone

mem0 = importlib.import_module("plugins.memory.mem0")
CUTOFF = datetime(2026, 10, 22, tzinfo=timezone.utc)


def test_a_raw_turn_past_its_window_goes():
    assert mem0._neoffice_raw_turns_past([(1, {"channel": "webhook", "created_at": "2026-09-16T03:12:45-07:00"})], CUTOFF) == [1]


def test_a_recent_raw_turn_stays():
    assert mem0._neoffice_raw_turns_past([(1, {"channel": "webhook", "created_at": "2026-11-01T03:12:45-07:00"})], CUTOFF) == []


def test_a_consolidated_fact_stays_whatever_its_age():
    assert mem0._neoffice_raw_turns_past([(1, {"channel": "cli", "created_at": "2026-07-01T03:12:45-07:00"})], CUTOFF) == []


def test_a_row_that_cannot_be_read_stays():
    points = [(1, {"created_at": "2026-07-01T03:12:45-07:00"}), (2, {"channel": "webhook"}), (3, {"channel": "webhook", "created_at": "x"}), (4, None)]
    assert mem0._neoffice_raw_turns_past(points, CUTOFF) == []
