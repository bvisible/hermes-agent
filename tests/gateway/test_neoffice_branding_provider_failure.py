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


# //// Neoffice — upstream's turn notices are written for a developer at a CLI.
# Measured on 2026-09-17 by llm/25_usage_chains on osiris, desk in French: both
# literals below arrived in the chat, in English. Reproduced here verbatim.
RAW_REDIRECT = "\u21aa Redirected current run. I'll adjust using your correction."
RAW_REDIRECT_WITH_DETAIL = (
    "\u21aa Redirected current run (2 min elapsed, running: frappe_list). "
    "I'll adjust using your correction."
)
RAW_NO_REPLY = (
    "\u26a0\ufe0f No reply: the request was cancelled by a new correction on every "
    "attempt, so the turn stopped instead of retrying forever. Your last correction "
    "is queued as the next message."
)


def test_a_turn_notice_keeps_its_meaning_in_french():
    """Typing while the assistant works is ordinary impatience, not an edge case."""
    from neoffice_branding import strip_internal_mechanics

    out = strip_internal_mechanics(RAW_REDIRECT)
    assert out != RAW_REDIRECT
    assert "correction" in out.lower()
    for machinery in ("Redirected", "current run", "I'll adjust"):
        assert machinery.lower() not in out.lower()


def test_the_status_detail_goes_with_it():
    """Elapsed minutes and the running tool are machinery, whatever the head says."""
    from neoffice_branding import strip_internal_mechanics

    out = strip_internal_mechanics(RAW_REDIRECT_WITH_DETAIL)
    assert out == strip_internal_mechanics(RAW_REDIRECT)
    for leak in ("frappe_list", "2 min", "elapsed", "running"):
        assert leak.lower() not in out.lower()


def test_every_busy_head_is_covered():
    """One head left uncovered is one English status line in a customer's chat."""
    from neoffice_branding import strip_internal_mechanics

    heads = (
        "\u23e9 Steered into current run. Your message arrives after the next tool call.",
        "\u23f3 Queued for the next turn. I'll respond once the current task finishes.",
        "\u23f3 Subagent working. Use /stop to interrupt.",
        "\u26a1 Interrupting current task. I'll respond to your message shortly.",
    )
    for raw in heads:
        out = strip_internal_mechanics(raw)
        assert out != raw, f"not covered: {raw}"
        assert "current" not in out.lower()


def test_an_unanswered_turn_never_tells_a_customer_to_send_continue():
    """Every reason in agent/turn_explainers.py addresses whoever RUNS the agent."""
    from neoffice_branding import NO_REPLY_REPLY, strip_internal_mechanics

    assert strip_internal_mechanics(RAW_NO_REPLY) == NO_REPLY_REPLY
    for instruction in ("continue", "switch provider", "No reply", "cancelled"):
        assert instruction.lower() not in NO_REPLY_REPLY.lower()


def test_the_technical_reason_stays_in_the_logs_2(caplog):
    import logging as _logging

    from neoffice_branding import strip_internal_mechanics

    with caplog.at_level(_logging.WARNING, logger="neoffice_branding"):
        strip_internal_mechanics(RAW_NO_REPLY)
    assert "correction" in caplog.text


def test_an_answer_that_quotes_one_of_these_survives():
    """The rules anchor on the START of the text, not on the words anywhere in it."""
    from neoffice_branding import strip_internal_mechanics

    kept = (
        "Le client m'a dit : \u00ab No reply: rien re\u00e7u \u00bb. Je le relance demain.",
        "J'ai redirig\u00e9 la commande vers l'entrep\u00f4t de Lausanne.",
    )
    for text in kept:
        assert strip_internal_mechanics(text) == text


# //// Neoffice — added 20.09, after finding two defects a customer reads. `_ROLE_RE`
# //// carried a hand-written list of poles that had not moved since it was written:
# //// `analyse` and `projet` arrived on 16.09 and the phrase « Le spécialiste projet
# //// va… » came out « Le équipe projet va… ». The list is now tied to POLES, which is
# //// the only place a pole is really declared, so the next pole cannot drift silently.
def test_every_pole_is_softened_into_the_team():
    """Each pole in POLES survives the round trip: no pole name, no « spécialiste »."""
    from gateway.nora_chat_router import POLES

    for pole in POLES:
        phrase = f"Le spécialiste {pole} va s'occuper de votre demande."
        rendu = strip_internal_mechanics(phrase)
        assert "spécialiste" not in rendu.lower(), f"{pole}: « spécialiste » survived → {rendu}"
        assert pole not in rendu.lower(), f"{pole}: the pole is named to the customer → {rendu}"
        assert rendu.startswith("L'équipe "), f"{pole}: mangled determiner → {rendu}"


def test_the_team_takes_the_case_of_where_it_lands():
    """A noun phrase carries the case of its position, not of the words it replaced."""
    assert strip_internal_mechanics(
        "Le spécialiste compta va traiter la facture."
    ).startswith("L'équipe")
    assert "Bonjour. L'équipe" in strip_internal_mechanics(
        "Bonjour. Le spécialiste compta va traiter la facture."
    )
    # French does not capitalise after a colon.
    assert "transmets : l'équipe" in strip_internal_mechanics(
        "Je transmets : le spécialiste ventes vous rappellera."
    )


def test_a_kanban_task_is_a_demande_and_the_determiner_is_not_doubled():
    """« Votre tâche kanban » must not become « Votre ta tâche », and never tutoies."""
    for phrase, attendu in (
        ("Votre tâche kanban est terminée.", "Votre demande est terminée."),
        ("La tâche kanban a été créée.", "La demande a été créée."),
    ):
        rendu = strip_internal_mechanics(phrase)
        assert rendu == attendu, f"{phrase} → {rendu}"
        assert " ta " not in f" {rendu} ", f"this product vouvoies: {rendu}"
