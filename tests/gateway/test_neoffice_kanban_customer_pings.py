# //// Neoffice — added file (no upstream equivalent): what a NORA customer chat receives from the
# //// kanban notifier, and what every other platform keeps from upstream.
"""The branded kanban pings speak only to NORA's customer chats (the webhook platform).

Every subscription NORA creates is on the webhook platform (desk Quick Chat, WhatsApp through the
router). There a raw board id, a task id or an English operator ping would reach a customer, so
our French formatters answer; any other platform keeps upstream's pings, wake handoff and review
wording (17 upstream tests broke while ours replaced them for every platform). Found on 08.10 in
the same pass: since v0.21.6 upstream's ``_clip`` takes a catalog key, and our block-loop template
passed there reached the chat braces included, « (bloquée {} fois pour la même raison) : {} ».
"""
from types import SimpleNamespace

import gateway.kanban_watchers_notifier as notifier


class _Event:
    def __init__(self, payload):
        self.payload = payload


def _chat(platform="webhook"):
    task = SimpleNamespace(assignee="compta", body="Combien de factures en retard ?", status="blocked",
                           result="", title="Factures en retard")
    return SimpleNamespace(platform_str=platform, task=task, head="H-t_0001", title="Factures en retard",
                           d={}, task_id="t_0001")


def _ping(kind, payload, platform="webhook"):
    msg, _, _ = notifier._EVENT_FORMATTERS[kind](_Event(payload), _chat(platform))
    return msg


def test_a_block_that_asks_the_person_reads_as_a_decision_without_braces():
    msg = _ping("block_loop_detected", {"kind": "needs_input", "recurrences": 3,
                                        "reason": "Quel compte utiliser pour cette facture ?"})
    assert "{" not in msg and "}" not in msg
    assert msg.startswith("🛑 Comptabilité — j'ai besoin de votre décision")
    assert "(bloquée 3 fois pour la même raison)" in msg
    assert msg.endswith(": Quel compte utiliser pour cette facture ?")


def test_a_technical_block_asks_nothing_and_keeps_its_reason_out_of_the_chat():
    msg = _ping("block_loop_detected", {"kind": "transient", "recurrences": 2,
                                        "reason": "MCP server neoffice-compta timed out"})
    assert "{" not in msg and "décision" not in msg and "MCP" not in msg
    assert "je bute sur cette demande (bloquée 2 fois pour la même raison)" in msg


def test_a_block_without_figures_says_neither_count_nor_colon():
    msg = _ping("block_loop_detected", {"kind": "needs_input"})
    assert msg == "🛑 Comptabilité — j'ai besoin de votre décision pour continuer"


def test_the_customer_never_sees_an_intermediate_transition():
    for kind in ("status", "review_requested", "changes_requested"):
        assert notifier._EVENT_FORMATTERS[kind](_Event({}), _chat()) == (None, None, None)


def test_a_crash_reads_as_nora_with_no_task_id():
    msg = _ping("crashed", {})
    assert msg == "✋ Comptabilité — incident technique, je réessaie."
    assert "t_0001" not in msg


def test_any_other_platform_keeps_upstreams_formatter(monkeypatch):
    calls = []

    def upstream(ev, n):
        calls.append(n.platform_str)
        return "upstream ping", "handoff", None

    for kind in ("completed", "blocked", "block_loop_detected", "review_requested", "status"):
        monkeypatch.setitem(notifier._NEOFFICE_UPSTREAM_FORMATTERS, kind, upstream)
        assert notifier._EVENT_FORMATTERS[kind](_Event({}), _chat("telegram")) == ("upstream ping", "handoff", None)
    assert calls == ["telegram"] * 5
# //// END Neoffice ////
