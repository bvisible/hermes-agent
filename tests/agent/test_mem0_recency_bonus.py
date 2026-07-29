"""Neoffice — guard the bounded recency bonus applied at recall time.

When two memories state the same thing with different values, the newer one must
win at RECALL — not only after the nightly consolidation retires the stale one.
Between two nightly runs both are live, so a user who restates a code in the
morning would otherwise still be answered with yesterday's value (llm/09 scores
1/3 when three codes arrive within minutes).

The bonus is deliberately SMALL and BOUNDED so it can only reorder near-ties.
The critical property — the one a naive "newest first" sort would break — is
that a fresh but IRRELEVANT memory must never outrank a relevant older one.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "mem0" / "__init__.py"


class _Scorer:
    """Bind the plugin's scoring method without importing mem0 itself."""

    def __init__(self):
        src = _PLUGIN.read_text(encoding="utf-8")
        start = src.index("    _RECENCY_MAX_BONUS")
        end = src.index("    # //// END Neoffice ////", start)
        body = "\n".join(line[4:] if line.startswith("    ") else line
                         for line in src[start:end].splitlines())
        ns: dict = {"re": re}
        exec(compile(body, str(_PLUGIN), "exec"), ns)  # noqa: S102
        self._score = ns["_recency_ranked_score"]
        # the method reads these off `self`
        self._RECENCY_MAX_BONUS = ns["_RECENCY_MAX_BONUS"]
        self._RECENCY_HALFLIFE_DAYS = ns["_RECENCY_HALFLIFE_DAYS"]
        self.max_bonus = ns["_RECENCY_MAX_BONUS"]

    def __call__(self, item):
        return self._score(self, item)


score = _Scorer()


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_newer_wins_between_equally_relevant_memories():
    """The whole point: same subject, same relevance, newer value wins."""
    old = {"score": 0.90, "created_at": _iso(20), "memory": "code = RT445566"}
    new = {"score": 0.90, "created_at": _iso(0), "memory": "code = RT999888"}
    assert score(new) > score(old)


def test_a_fresh_irrelevant_memory_never_beats_a_relevant_old_one():
    """The property a naive 'newest first' sort would destroy."""
    relevant_old = {"score": 0.90, "created_at": _iso(365)}
    irrelevant_fresh = {"score": 0.40, "created_at": _iso(0)}
    assert score(relevant_old) > score(irrelevant_fresh)


def test_bonus_is_bounded():
    """A brand-new memory gains at most the configured bonus."""
    base = {"score": 1.0, "created_at": _iso(0)}
    assert score(base) <= 1.0 * (1.0 + score.max_bonus) + 1e-9
    assert score(base) > 1.0


def test_old_memories_keep_essentially_their_score():
    old = {"score": 0.80, "created_at": _iso(365)}
    assert abs(score(old) - 0.80) < 0.01


@pytest.mark.parametrize("created", [None, "", "not-a-date", 12345])
def test_undated_or_malformed_is_neutral_not_penalised(created):
    """Never punish a memory for lacking a usable timestamp."""
    item = {"score": 0.75}
    if created is not None:
        item["created_at"] = created
    assert score(item) == 0.75
