# //// Neoffice — added file (no upstream equivalent): routing of a client's contact change.
"""Changing a client's or supplier's e-mail, phone or address routes to the pole that holds the
tool for it (ventes: frappe_party_contact_update), and a request to SEND an e-mail still routes to
support.

Measured on a development instance, 2026-09-24: « … a une nouvelle adresse e-mail » reached
support through the e-mail rule, whose send-verb alternative `adress` matched the noun
« adresse ». Support holds no tool to change a contact: its worker set the Contact's e-mail
field, which the Contact recomputes on save, read it back unchanged and tried again until the
iteration limit.
"""

import pytest

from gateway.nora_chat_router import _keyword_pole

CONTACT_CHANGES = [
    "Change l'adresse e-mail de la société Martin : info@exemple.ch",
    "La société Martin a une nouvelle adresse e-mail : info@exemple.ch",
    "Martin SA a changé d'adresse e-mail, c'est maintenant info@exemple.ch",
    "Mets à jour le téléphone de Martin SA : 079 000 00 00",
    "Nouveau numéro de téléphone pour Martin SA : 079 000 00 00",
    "Martin SA a déménagé : rue du Lac 12, 1003 Lausanne",
    "Remplace l'e-mail du fournisseur Dupont par achats@exemple.ch",
]

SENDS = [
    "Envoie un e-mail à Martin SA pour confirmer la livraison",
    "Écris un courriel à la nouvelle adresse de Martin SA",
    "Adresse-lui un mail avec le devis",
    "Adresse un mail à Martin SA avec le devis",
    "Rédige un e-mail pour la facture FA-2026-0012",
]

LEFT_TO_THE_CLASSIFIER = [
    # the company itself moved: not a client's address
    "Nous avons déménagé le 1er octobre",
    # an employee's details are rh's records
    "Le collaborateur Pierre a une nouvelle adresse e-mail",
    # a question about an address is a read, not a send and not a change
    "Quelle est l'adresse e-mail de Martin SA ?",
]


@pytest.mark.parametrize("message", CONTACT_CHANGES)
def test_a_contact_change_goes_to_ventes(message):
    assert _keyword_pole(message) == "ventes"


@pytest.mark.parametrize("message", SENDS)
def test_a_request_to_send_an_email_stays_support(message):
    assert _keyword_pole(message) == "support"


@pytest.mark.parametrize("message", LEFT_TO_THE_CLASSIFIER)
def test_no_keyword_rule_takes_these(message):
    assert _keyword_pole(message) is None


# ── an employee's record is rh's (#843) ─────────────────────────────────────────────────────
# « <Prénom Nom> a déménagé : sa nouvelle adresse est … Mets sa fiche à jour » reached ventes,
# which searched clients and suppliers seventeen times: the person was an employee. The router
# asks nora whether the name is an employee's, and only for a contact change routed to ventes.

import io
import json

from gateway import nora_chat_router as router

EMPLOYEE_MOVED = "Marie Exemple a déménagé : sa nouvelle adresse est rue du Lac 12, 1003 Lausanne. Mets sa fiche à jour."


def test_a_contact_change_that_names_an_employee_goes_to_rh(monkeypatch):
    monkeypatch.setattr(router, "_employee_named", lambda message, extra: True)
    assert router._pole_for_an_employee_record("ventes", EMPLOYEE_MOVED, {}) == "rh"


def test_a_client_s_contact_change_stays_with_ventes(monkeypatch):
    monkeypatch.setattr(router, "_employee_named", lambda message, extra: False)
    assert router._pole_for_an_employee_record("ventes", "Martin SA a déménagé : rue du Lac 12", {}) == "ventes"


def _never_asked(message, extra):
    raise AssertionError("only a contact change routed to ventes is looked up")


@pytest.mark.parametrize(
    "category, message",
    [
        ("ventes", "Fais un devis pour Marie Exemple : 3 heures de conseil"),
        ("compta", EMPLOYEE_MOVED),
        ("support", "Envoie un e-mail à Marie Exemple pour confirmer le rendez-vous"),
    ],
)
def test_nothing_else_is_looked_up(monkeypatch, category, message):
    monkeypatch.setattr(router, "_employee_named", _never_asked)
    assert router._pole_for_an_employee_record(category, message, {}) == category


def test_without_the_desk_callback_there_is_no_lookup():
    assert router._employee_named(EMPLOYEE_MOVED, {}) is False


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "body, employee",
    [
        ({"message": {"ok": True, "employee": "HR-EMP-00001", "employee_name": "Marie Exemple"}}, True),
        ({"message": {"ok": True}}, False),
        ({"message": {"ok": False, "error": "boom"}}, False),
    ],
)
def test_the_lookup_reads_frappe_s_envelope(monkeypatch, body, employee):
    import urllib.request

    seen = {}

    def urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Answer(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    extra = {
        "callback_url": "https://erp.example.ch/api/method/nora.api.v2.hermes_callback.deliver",
        "callback_token": "t",
    }
    assert router._employee_named(EMPLOYEE_MOVED, extra) is employee
    assert seen["url"].endswith("nora.api.v2.task_router.employee_named")
