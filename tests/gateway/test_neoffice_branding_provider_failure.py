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


def test_the_guardrail_halt_upstream_writes_today_is_replaced_whole():
    """Built with upstream's own method: a rewording of the template fails HERE."""
    from types import SimpleNamespace

    from neoffice_branding import GUARDRAIL_HALT_REPLY, strip_internal_mechanics
    from run_agent import AIAgent

    decision = SimpleNamespace(tool_name="mcp__neoffice_compta__get_chart_of_accounts", count=5,
                               code="identical_call_streak_halt")
    raw = AIAgent._toolguard_controlled_halt_response(None, decision)
    assert strip_internal_mechanics(raw) == GUARDRAIL_HALT_REPLY, raw


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


# //// Neoffice — added 20.09. kanban_watchers_notifier keeps a literal copy of the pole
# //// labels because it imports POLE_LABELS lazily and best-effort — a label is never
# //// worth failing a delivery for. A copy kept by hand drifts, and this one had: `rh`
# //// read "RH" where the router says "Ressources Humaines", so on the day the import
# //// failed a customer would have been given a different word for the same desk. A
# //// fallback that disagrees with the thing it stands in for is a fallback that lies.
def test_the_fallback_pole_labels_match_the_source_of_truth():
    from gateway.kanban_watchers_notifier import _NEOFFICE_DOMAINS
    from gateway.nora_chat_router import POLE_LABELS

    assert _NEOFFICE_DOMAINS == POLE_LABELS["fr"], (
        "the notifier fallback drifted from POLE_LABELS['fr']: "
        + repr({p: (POLE_LABELS["fr"].get(p), _NEOFFICE_DOMAINS.get(p))
                for p in set(POLE_LABELS["fr"]) | set(_NEOFFICE_DOMAINS)
                if POLE_LABELS["fr"].get(p) != _NEOFFICE_DOMAINS.get(p)})
    )


def test_every_pole_has_a_customer_facing_label_in_every_language():
    """A pole with no label is a pole the customer hears by its internal key."""
    from gateway.nora_chat_router import POLES, POLE_LABELS

    for langue, labels in POLE_LABELS.items():
        for pole in POLES:
            assert labels.get(pole), f"{pole} has no {langue} label"
            assert labels[pole] != pole, f"{pole} leaks its internal key in {langue}"


# //// Neoffice — added 20.09. Every sentence this module hands a customer was a French
# //// literal, while POLE_LABELS and the canned small talk in the router next door have
# //// carried fr/de/it/en for months. A German-speaking user whose turn failed read a
# //// French apology at the worst possible moment.
def test_a_failed_turn_speaks_the_customers_language():
    from neoffice_branding import strip_internal_mechanics

    brut = "API call failed after 3 retries: HTTP 502 \u2014 502 Bad Gateway"
    rendus = {lang: strip_internal_mechanics(brut, lang) for lang in ("fr", "de", "it", "en")}
    assert len(set(rendus.values())) == 4, "some languages share a sentence: " + repr(rendus)
    assert "KI-Dienst" in rendus["de"]
    assert "intelligenza artificiale" in rendus["it"]
    assert "AI service" in rendus["en"]


def test_an_unknown_or_absent_language_falls_back_to_french():
    """French is the product default; that was the behaviour before this existed."""
    from neoffice_branding import PROVIDER_UNAVAILABLE_REPLY, strip_internal_mechanics

    brut = "API call failed after 3 retries: HTTP 502"
    for lang in (None, "", "   ", "pt", "zz-ZZ"):
        assert strip_internal_mechanics(brut, lang) == PROVIDER_UNAVAILABLE_REPLY, lang


def test_a_regional_code_resolves_to_its_language():
    """The desk may send de-CH or fr_CH; the region is not a different language."""
    from neoffice_branding import strip_internal_mechanics

    brut = "API call failed after 3 retries: HTTP 502"
    assert strip_internal_mechanics(brut, "de-CH") == strip_internal_mechanics(brut, "de")
    assert strip_internal_mechanics(brut, "fr_CH") == strip_internal_mechanics(brut, "fr")
    assert strip_internal_mechanics(brut, "IT") == strip_internal_mechanics(brut, "it")


def test_the_stripping_itself_is_language_independent():
    """The persisted transcript has no language in scope, and must still agree.

    The delivered chat and the saved transcript are allowed to differ on the WORDING of
    a failure sentence; they are not allowed to differ on which machinery was removed.
    That is what the persistence note means by "a transcript that disagrees with the
    chat confuses support".
    """
    from neoffice_branding import strip_internal_mechanics

    for phrase in (
        "Le sp\u00e9cialiste compta va traiter votre t\u00e2che kanban.",
        "Notre expert projet regarde le board.",
        "La t\u00e2che kanban `t_abc123def` est termin\u00e9e.",
    ):
        rendus = {lang: strip_internal_mechanics(phrase, lang) for lang in (None, "fr", "de", "it", "en")}
        assert len(set(rendus.values())) == 1, "stripping drifted by language: " + repr(rendus)


def test_every_reply_exists_in_every_language():
    """A key with a missing language silently falls back — the guard says so instead."""
    from neoffice_branding import REPLIES

    for cle, par_langue in REPLIES.items():
        for lang in ("fr", "de", "it", "en"):
            assert par_langue.get(lang), f"{cle} has no {lang}"
        assert len(set(par_langue.values())) == 4, f"{cle} repeats a sentence across languages"


def test_every_busy_notice_is_keyed_to_a_reply():
    """A notice keyed to nothing would raise at the worst moment — on a busy turn."""
    from neoffice_branding import REPLIES, _BUSY_NOTICES

    for _pattern, cle in _BUSY_NOTICES:
        assert cle in REPLIES, f"busy notice keyed to unknown reply {cle!r}"


# //// Neoffice — upstream's failure copy must not reach the customer (v2026.9.24).
def _every_failure_copy():
    """(name, text) for every template agent/turn_failure_copy.py can hand a chat today."""
    from agent import turn_failure_copy as tfc

    fields = dict(model="nora", label="Olares", attempts=3, detail="HTTP 502", preview="…", limit=25,
                  tokens=1000, window=32000, resume="", home="~/.hermes", prefix_hint="",
                  relogin="hermes auth add custom --type oauth")
    rendered = [(code, tfc.site_copy(code, **fields)) for code in tfc._SITE_COPY]
    rendered += [(f"exhausted:{r}", tfc.exhausted_copy(r, label="Olares", attempts=3, summary="HTTP 502"))
                 for r in [*tfc._EXHAUSTED_LEADS, "unknown"]]
    rendered += [(f"nonretryable:{r}", t.format_map(tfc._Defaults(fields)))
                 for r, t in [*tfc._NONRETRYABLE_COPY.items(), ("default", tfc._NONRETRYABLE_DEFAULT_COPY)]]
    rendered += [(f"auth:{r}", t.format_map(tfc._Defaults(fields))) for r, t in tfc._AUTH_COPY.items()]
    return rendered


def test_no_failure_copy_of_upstream_reaches_the_customer():
    from neoffice_branding import reply, strip_internal_mechanics

    ours = {reply("provider_unavailable"), reply("no_reply")}
    leaks = [(name, text[:90]) for name, text in _every_failure_copy() if strip_internal_mechanics(text) not in ours]
    assert not leaks, leaks


def test_an_engine_outage_reads_as_one_and_a_stuck_turn_as_one():
    from agent.turn_failure_copy import exhausted_copy, site_copy
    from neoffice_branding import reply, strip_internal_mechanics

    outage = exhausted_copy("overloaded", label="Olares", attempts=3, summary="HTTP 503")
    assert strip_internal_mechanics(outage) == reply("provider_unavailable")
    stuck = site_copy("loop_error", detail="boom")
    assert strip_internal_mechanics(stuck) == reply("no_reply")


import pytest  # noqa: E402 — used by the parametrized check below


@pytest.mark.parametrize("answer", [
    "La facture est prête : https://osiris.neoffice.me/app/sales-invoice/new (brouillon).",
    "Vous avez 547 factures impayées pour un total de 17 371,90 CHF.",
])
def test_a_real_answer_is_left_alone(answer):
    from neoffice_branding import strip_internal_mechanics

    assert strip_internal_mechanics(answer) == answer
# //// END Neoffice ////
