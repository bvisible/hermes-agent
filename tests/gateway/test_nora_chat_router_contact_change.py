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
