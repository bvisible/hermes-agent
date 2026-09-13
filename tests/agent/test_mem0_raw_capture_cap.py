# //// Neoffice — added file (no upstream equivalent).
"""Neoffice — guard the size cap on mem0's RAW per-turn capture.

v2026.9.11 caps every synced message at 450 characters (``_SYNC_MSG_MAX_CHARS``)
because small-window embedders (512 tokens) fail on longer input, and the turn is
then dropped in silence. Our capture stores the RAW turn (``infer=False``) through
qwen3-embedding:8b, measured 2026-09-13: 20 000 characters (4 910 tokens) embed in
9.4 s, 60 000 get a 503 from the shared embedding backend. On osiris the longest of
364 stored memories is 961 characters, and 30 of them exceed 450 — the longest and
most useful ones (worker answers). So the fork keeps real turns whole and only caps
a pasted document, at 8 000 characters, unless mem0.json sets ``sync_max_chars``.

What these tests lock down, in order of importance: a real answer is never cut; a
paste no longer fails the embedding; an explicit configuration still wins.
"""

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider

QUESTION = "Où en est la commande 4521 ?"  # carries a value: never dropped as filler
SENTENCE = "Le client a demandé de reporter la livraison des chaises au mois prochain. "


class RecordingBackend:
    """Minimal mem0 backend: records what the capture would embed and store."""

    def __init__(self):
        self.added = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.added.append(messages)
        return {"status": "PENDING"}


@pytest.fixture(autouse=True)
def hermes_home(monkeypatch, tmp_path):
    # No mem0.json unless a test writes one: the fork's own default applies.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_API_KEY", "test-key")
    return tmp_path


def _capture(user, assistant):
    backend = RecordingBackend()
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id, provider._agent_id, provider._backend = "u123", "hermes", backend
    provider.sync_turn(user, assistant, session_id="s1")
    provider._sync_thread.join(timeout=5)
    assert len(backend.added) == 1, "the turn was not captured at all"
    return provider, {m["role"]: m["content"] for m in backend.added[0]}


def test_the_longest_real_answer_is_stored_whole():
    answer = (SENTENCE * 20)[:961]  # the longest memory stored on osiris
    assert len(answer) > 450  # upstream's default would have cut it
    _, stored = _capture(QUESTION, answer)
    assert stored["assistant"] == answer
    assert stored["user"] == QUESTION


def test_a_pasted_document_is_capped_not_dropped():
    paste = SENTENCE * 300  # ~22 500 chars: beyond what the shared embedder serves
    provider, stored = _capture(QUESTION, paste)
    kept = stored["assistant"]
    assert 7000 < len(kept) <= 8000  # cut near our cap, not at upstream's 450
    assert paste.startswith(kept) and kept.endswith(".")  # at a sentence boundary
    assert provider._consecutive_failures == 0


def test_an_explicit_sync_max_chars_still_wins(hermes_home):
    (hermes_home / "mem0.json").write_text('{"sync_max_chars": 450}')
    _, stored = _capture(QUESTION, SENTENCE * 20)
    assert len(stored["assistant"]) <= 450
