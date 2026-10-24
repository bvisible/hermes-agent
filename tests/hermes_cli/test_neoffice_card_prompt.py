# //// Neoffice — added file (no upstream equivalent): the text of a pole's card turn.
"""Since v0.22.3 the profile owner runs a kanban card (gateway/session_kanban.py) and its first turn already opens on
the card (build_worker_context: title, body, our « already executed » header). The text we add after it is the
header line and, for an accounting question to the compta pole, the protocol of the real chart; repeating the card
there cost the prefill of its body twice (up to 6 000 characters on every card turn)."""

from types import SimpleNamespace

from hermes_cli.kanban_db_dispatch import _neoffice_worker_prompt


def _task(assignee: str, title: str, body: str = "") -> SimpleNamespace:
    return SimpleNamespace(id="t_fixture", assignee=assignee, title=title, body=body)


def test_the_owner_run_card_text_does_not_repeat_the_card():
    text = _neoffice_worker_prompt(_task("ventes", "Combien de devis sont ouverts ?", "Corps de la carte."),
                                   inline_card=False)
    assert text == "work kanban task t_fixture"


def test_an_accounting_question_keeps_its_protocol_without_the_card():
    text = _neoffice_worker_prompt(_task("compta", "Dans quel compte imputer une facture d'électricité ?"),
                                   inline_card=False)
    assert text.startswith("work kanban task t_fixture")
    assert "PROTOCOLE OBLIGATOIRE" in text and "ALREADY EXECUTED" not in text


def test_the_dispatcher_prompt_still_inlines_the_card():
    text = _neoffice_worker_prompt(_task("ventes", "Titre de la carte", "Corps de la carte."))
    assert "ALREADY EXECUTED" in text and "Corps de la carte." in text
