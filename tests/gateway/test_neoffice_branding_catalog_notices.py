# //// Neoffice — added file (no upstream equivalent).
"""Upstream's gateway notices never reach a customer, in whatever language the gateway speaks.

v0.21.6 moved the busy notices, the « No reply » prefix and the gateway's error copy into the
catalog of agent/i18n.py, rendered in the profile's display.language. provision.sh sets French
(the clarify prompt was our own French literal until then), and every rule of neoffice_branding
anchored on English words: on the merged tree, seven notices out of eight reached a customer
whole, in French (08.10). These tests render each notice with upstream's own code, in the four
languages NORA speaks, and sweep the catalog, so that a notice upstream adds tomorrow fails
here instead of reaching a chat.
"""
import time
from types import SimpleNamespace

import pytest

from agent import i18n
from neoffice_branding import _CATALOG_NOTICES, REPLIES, _catalog_anchor, reply, strip_internal_mechanics

LANGUAGES = ("fr", "de", "it", "en")


@pytest.fixture
def speak(monkeypatch):
    """Set the gateway's language the way a profile does (HERMES_LANGUAGE before display.language)."""

    def _speak(language):
        monkeypatch.setenv("HERMES_LANGUAGE", language)
        i18n.reset_language_cache()
        assert i18n.get_language() == language

    yield _speak
    i18n.reset_language_cache()


class _Blank(dict):
    def __missing__(self, key):
        return "x"


def _render(template: str) -> str:
    return template.format_map(_Blank())


_BUSY_FLAGS = dict(is_steer_mode=False, is_queue_mode=False, is_redirect_mode=False,
                   demoted_for_subagents=False, demoted_for_compression=False)
_BUSY_MODES = {
    "steered": (dict(is_steer_mode=True), False),
    "steered:subagents": (dict(is_steer_mode=True), True),
    "redirected": (dict(is_redirect_mode=True), False),
    "queued": (dict(is_queue_mode=True), False),
    "queued:subagent": (dict(is_queue_mode=True, demoted_for_subagents=True), False),
    "queued:compression": (dict(is_queue_mode=True, demoted_for_compression=True), False),
    "interrupting": ({}, False),
}


def _busy_notice(name: str) -> str:
    """The busy notice exactly as the gateway composes it (gateway/run_busy.py)."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    flags, subagents = _BUSY_MODES[name]
    runner = object.__new__(GatewayRunner)
    runner._agent_has_active_subagents = lambda _agent: subagents
    event = SimpleNamespace(source=SimpleNamespace(platform=Platform.WEBHOOK, chat_id="webhook:nora_chat:test"))
    return runner._compose_busy_ack_message(event, time.time(), None, None, **{**_BUSY_FLAGS, **flags})


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", list(_BUSY_MODES))
def test_every_busy_notice_reaches_the_customer_as_our_sentence(speak, language, name):
    speak(language)
    raw = _busy_notice(name)
    assert strip_internal_mechanics(raw, language) == reply(name.split(":")[0], language), raw


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_unanswered_turn_never_reaches_the_customer_whatever_the_reason(speak, language):
    """agent/turn_explainers.py prefixes every reason with explainer.no_reply_prefix."""
    speak(language)
    reasons = [key for key in i18n._load_catalog(language)
               if key.startswith(("explainer.exit.", "explainer.persistence."))] + ["explainer.empty_response"]
    assert len(reasons) > 10
    leaks = []
    for key in reasons:
        raw = i18n.t("explainer.no_reply_prefix") + _render(i18n.t(key))
        if strip_internal_mechanics(raw, language) != reply("no_reply", language):
            leaks.append((key, raw[:90]))
    assert not leaks, leaks


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_catalogued_notice_still_exists_and_anchors(speak, language):
    """A key upstream renames or drops would leave its rule anchored on nothing, silently."""
    speak(language)
    for key, reply_key in _CATALOG_NOTICES:
        assert reply_key in REPLIES, key
        text = i18n.t(key)
        assert text != key, f"{key} left the catalog: its rule anchors on nothing"
        assert _catalog_anchor(text) is not None, f"{key} opens on too little to anchor: {text!r}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_catalogued_notice_is_replaced_by_its_sentence(speak, language):
    speak(language)
    wrong = []
    for key, reply_key in _CATALOG_NOTICES:
        raw = _render(i18n.t(key)) + ("x" if key == "explainer.no_reply_prefix" else "")
        out = strip_internal_mechanics(raw, language)
        if out != reply(reply_key, language):
            wrong.append((key, raw[:80], out[:60]))
    assert not wrong, wrong


# What a customer may read as upstream wrote it, or what never reaches a NORA chat. Every other
# notice that opens on a symbol must come out as one of our sentences.
_LEFT_AS_UPSTREAM_WROTE_IT = {
    "gateway.notify.delivery_failed_retry": "says, in plain words, that the answer could not be delivered",
    "gateway.notify.media_delivery_failed": "names an attachment that could not be delivered",
    "gateway.notify.media_delivery_failed_with_name": "the same, with the file name",
    "gateway.notify.plain_fallback_prefix": "precedes the real answer when its formatting failed",
    "gateway.busy.status": "the reply to /busy, an operator command",
    "explainer.file_mutation.header": "write_file and patch only, tools no NORA profile carries",
    "gateway.notify.no_home_channel": "human platforms only: run_turn returns before it for a webhook",
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_gateway_notice_of_upstream_reaches_a_customer(speak, language):
    speak(language)
    ours = {reply(key, language) for key in REPLIES}
    leaks = []
    for key, template in sorted(i18n._load_catalog(language).items()):
        if not key.startswith(("gateway.busy.", "gateway.errors.", "gateway.notify.", "explainer.")):
            continue
        if key in _LEFT_AS_UPSTREAM_WROTE_IT or key.endswith("_tail"):
            continue
        raw = _render(template)
        if not raw.strip() or raw[:1].isalnum() or raw[:1] in " .,":
            continue  # a fragment or a lead-in: never a message on its own
        if strip_internal_mechanics(raw, language) not in ours:
            leaks.append((key, raw[:90]))
    assert not leaks, leaks


@pytest.mark.parametrize("answer", [
    "Pas de réponse du client depuis lundi ; je le relance demain.",
    "Mis en file d'attente : trois factures partiront ce soir.",
    "Exécution en cours redirigée vers l'entrepôt de Lausanne.",
    "Le gateway de paiement TWINT est configuré.",
    "Désolé, j'ai rencontré une erreur en créant la facture : le client n'existe pas.",
])
def test_an_answer_that_opens_on_the_same_words_is_kept(speak, answer):
    """The anchors require upstream's leading symbol, so a business answer passes untouched."""
    speak("fr")
    assert strip_internal_mechanics(answer, "fr") == answer
# //// END Neoffice ////
