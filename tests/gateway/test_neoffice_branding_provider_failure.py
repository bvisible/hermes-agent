"""A provider failure must not reach the customer as Nora's answer (#450).

During the Olares outage of 2026-09-14 (#449), the desk chat of a customer displayed
"API call failed after 3 retries: HTTP 502 — 502 Bad Gateway" as a reply, in English,
12 s after the question. The delivery chain was fine: only what we let through was
wrong. The single delivery chokepoint now hands the customer one French sentence, and
the technical text stays in the logs.
"""

import logging

from neoffice_branding import PROVIDER_UNAVAILABLE_REPLY, strip_internal_mechanics

RAW_FAILURE = "API call failed after 3 retries: HTTP 502 — 502 Bad Gateway | provider=custom model=nora"


def test_a_failed_turn_is_replaced_by_one_french_sentence():
    assert strip_internal_mechanics(RAW_FAILURE) == PROVIDER_UNAVAILABLE_REPLY


def test_the_replacement_says_what_to_do_and_names_no_machinery():
    for internal in ("API", "HTTP", "502", "provider", "olares", "hermes"):
        assert internal.lower() not in PROVIDER_UNAVAILABLE_REPLY.lower()
    assert "indisponible" in PROVIDER_UNAVAILABLE_REPLY


def test_the_technical_text_stays_in_the_logs(caplog):
    with caplog.at_level(logging.WARNING, logger="neoffice_branding"):
        strip_internal_mechanics(RAW_FAILURE)
    assert "502" in caplog.text


def test_a_real_answer_is_untouched():
    answer = "Votre facture FA-2026-00012 est prête."
    assert strip_internal_mechanics(answer) == answer


def test_internal_vocabulary_is_still_stripped():
    assert "kanban" not in strip_internal_mechanics("J'ai ouvert une tâche kanban pour vous.").lower()


def test_a_reply_that_merely_mentions_an_error_is_kept():
    # The rule anchors on upstream's literal, not on the digits: a human sentence
    # about an incident is an answer and must survive.
    text = "Le fournisseur a renvoyé un code 502 hier soir ; je vous recontacte demain."
    assert strip_internal_mechanics(text) == text


# //// Neoffice — the tool-call guardrail must not reach the customer (#476).
# The raw text below is upstream's own template, from run_agent.py
# _toolguard_controlled_halt_response, reproduced as it was measured in a desk
# chat on 2026-09-16.
RAW_GUARDRAIL = (
    "I stopped retrying frappe_job_quote because it hit the tool-call guardrail "
    "(identical_call_streak_halt) after 5 repeated non-progressing attempts. The "
    "last tool result explains the blocker; the next step is to change strategy "
    "instead of repeating the same call."
)


def test_a_guardrail_halt_is_replaced_whole():
    """It is machinery end to end: stripping words would leave half a sentence."""
    from neoffice_branding import GUARDRAIL_HALT_REPLY, strip_internal_mechanics

    assert strip_internal_mechanics(RAW_GUARDRAIL) == GUARDRAIL_HALT_REPLY


def test_the_guardrail_reply_names_no_machinery_and_is_french():
    from neoffice_branding import GUARDRAIL_HALT_REPLY

    lowered = GUARDRAIL_HALT_REPLY.lower()
    for word in ("guardrail", "tool", "halt", "kanban", "worker", "retry", "streak"):
        assert word not in lowered
    assert "demande" in lowered  # a French sentence, not a translated literal


def test_an_ordinary_answer_mentioning_a_tool_is_untouched():
    """The anchor is the guardrail phrase, not the word 'tool' — a real answer
    that happens to discuss tooling must pass through unchanged."""
    from neoffice_branding import strip_internal_mechanics

    answer = "Vous avez 3 devis en attente. Le dernier date du 12 septembre."
    assert strip_internal_mechanics(answer) == answer


def test_a_task_id_is_removed_from_a_sentence_that_survives():
    from neoffice_branding import strip_internal_mechanics

    out = strip_internal_mechanics("La tâche `t_51f94c26` a bien été prise en compte.")
    assert "t_51f94c26" not in out
    assert "prise en compte" in out  # the sentence is not destroyed with the id
