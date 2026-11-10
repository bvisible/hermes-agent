# //// Neoffice — added file (no upstream equivalent): routing of a person's access to Neoffice.
"""A person's ACCESS to Neoffice is the HR pole's: an employee's account, a hire, a subcontractor's login, the licence seats left.

Measured on osiris, 2026-11-10: « crée un compte pour un sous-traitant » reached the sales pole, which proposed a SUPPLIER record (a subcontractor
who must sign in is a person, never a supplier), and « donne un accès à Marie » and « combien de places de licence me reste-t-il ? » were left to
the classifier, which answered 'direct'. The tools are the HR pole's (frappe_access_options / _propose / _give, frappe_employee_hire,
frappe_subcontractor_create); the rule sends the request there before the sales and accounting words it meets («compte», «fournisseur»).
"""

import pytest

from gateway import nora_chat_router as router
from gateway.nora_chat_router import _keyword_pole

TO_RH = [
    "Donne un accès à Marie, c'est un chef de chantier",
    "Crée le compte de l'employé Paul, il est au bureau",
    "Ouvre un accès à l'ouvrier Marc",
    "Donne un accès à la nouvelle secrétaire Sophie Morel",
    "Crée un compte pour un sous-traitant",
    "Karim est sous-traitant, il doit pouvoir se connecter",
    "Je veux qu'un sous-traitant puisse accéder à Neoffice, il a son login",
    "J'engage Marie Dupont",
    "On engage un monteur, Paul Martin",
    "Crée l'employé Paul Martin",
    "Ajoute un nouvel employé",
    "Combien de places de licence me reste-t-il ?",
    # recruitment keeps its own rule: « j'embauche » is the recruitment rule's, the same pole
    "J'embauche une secrétaire",
]

NOT_TO_RH = [
    # an account of a customer, a supplier, a bank, the ledger: not a person's access
    "Crée un compte client pour Dupont SA",
    "Crée le client Dupont SA, info@dupont.ch",
    "Crée un fournisseur Müller AG",
    "Crée le compte bancaire de l'employé",
    "Donne-moi l'accès à la facture de Dupont",
    "Donne-moi les données du compte de Paul",
    # a subcontractor's invoice or balance is accounting's, whatever the word
    "La facture du sous-traitant est à payer",
    "Quel est le solde du compte du sous-traitant ?",
    # software licences are not the seats of Neoffice
    "Quelles sont les licences logicielles à renouveler ?",
]


@pytest.mark.parametrize("message", TO_RH)
def test_a_persons_access_goes_to_rh(message):
    assert _keyword_pole(message) == "rh"


@pytest.mark.parametrize("message", NOT_TO_RH)
def test_what_is_not_a_persons_access_stays_away_from_rh(message):
    assert _keyword_pole(message) != "rh"


def test_the_rule_comes_right_after_recruitment_in_the_table():
    # before every rule a sales or accounting word can meet («compte», «fournisseur», «client»); after recruitment, which excludes the ledger
    poles = [(rx, pole) for rx, pole in router._FAST_PATH_RULES]
    indexes = {id(rx): i for i, (rx, _pole) in enumerate(poles)}
    assert indexes[id(router._PEOPLE_ACCOUNT_RE)] == indexes[id(router._RECRUITMENT_RE)] + 1
    assert poles[indexes[id(router._PEOPLE_ACCOUNT_RE)]][1] == "rh"


def test_the_classifier_prompt_names_the_access_and_keeps_a_subcontractor_off_ventes():
    source = open(router.__file__, encoding="utf-8").read()
    assert "SOUS-TRAITANT" in source and "un sous-traitant n'est pas un fournisseur" in source
