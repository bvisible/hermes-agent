# //// Neoffice — added file (no upstream equivalent): the rule that keeps amounts and pay
# //// out of the shared company memory (#881).
"""What the shared company bucket may hold: DEFAULT-DENY on numbers.

The company bucket is merged into what every colleague recalls, whatever their rights, and
a stored fact carries neither its author nor its source. A deny-list of amount shapes
leaked twice; the rule now keeps a fact private unless every number in it is recognised as
something other than money (gateway/neoffice_memory_policy.py).

The rule is a verbatim copy of NORA's nora/utils/memory_privacy.py, and
neoffice_memory_privacy_vectors.json a BYTE-IDENTICAL copy of NORA's
nora/utils/memory_privacy_vectors.json: both copies of the rule run the same examples, so
they cannot drift apart again. Change both files together, never one. The lists below are
this gateway's own extra cases.
"""

import json
import time
from pathlib import Path

import pytest

from gateway.neoffice_memory_policy import split_company_facts, stays_private

VECTORS = json.loads(
    (Path(__file__).parent / "neoffice_memory_privacy_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text", VECTORS["private"])
def test_a_shared_private_vector_stays_private(text):
    assert stays_private(text), text


@pytest.mark.parametrize("text", VECTORS["shared"])
def test_a_shared_shareable_vector_may_be_shared(text):
    assert not stays_private(text), text


def test_the_vector_file_holds_both_lists():
    assert set(VECTORS) <= {"about", "private", "shared"}
    assert VECTORS["private"] and VECTORS["shared"]
    assert not set(VECTORS["private"]) & set(VECTORS["shared"])


# The gateway's own extra cases: what reaches memory_retain in other words.
PRIVATE = [
    "Visits: 1.2M",
    "Paul gets 1 250",
    # A year-shaped amount in a money sentence.
    "Le loyer est de 1950",
    "Le devis 2025 est prêt",
    # A money word makes any number private, however small, anywhere in the sentence.
    "Le loyer est de 50",
    "Client X owes 45",
    "Coffee costs CHF 4",
    "Le repas: 45.-",
    "The rent (50) is paid monthly",
    "Paul gets 50 per month",
    # An account word does not hide what follows the account.
    "compte 6000 - Loyer - 50",
    "Konto 1020: 45230",
    "Paid into account 12000 CHF",
    "The rent is 2500 - paid by transfer - every month",
    # A capitalised word after a number is a place only where an address puts one.
    "Paul gets 8000 Net",
    "Paul gets 8000 Every month",
]

SHARED = [
    "Our electricity supplier is the local utility",
    "The main supplier is Fr. Example AG",
    "Office hours 8h30-12h00",
    "Le magasin ouvre à 8h30",
    "The report is due on 2026-03-14",
    "We moved to the new office in March 2015",
    "Support phone 0800 123 456",
    "Le compte bancaire CH93 0076 2011 6238 5295 7 est tenu à la banque cantonale.",
    "Notre numéro IDE est CHE-123.456.789 TVA",
    "Le fournisseur X est habituellement imputé sur le compte 6000 - Loyer (12 factures sur 12, soit 100%).",
    "Fournisseur X : imputé sur le compte 6000 - Loyer - ABC.",
    "The balance of account 1020 is negative",
]


@pytest.mark.parametrize("text", PRIVATE)
def test_an_amount_or_pay_stays_private(text):
    assert stays_private(text), text


@pytest.mark.parametrize("text", SHARED)
def test_a_plain_business_fact_may_be_shared(text):
    assert not stays_private(text), text


def test_split_keeps_the_order_of_each_side():
    shared, private = split_company_facts(["We bill in CHF", "Paul's salary is 7000", "We have 12 employees"])
    assert shared == ["We bill in CHF", "We have 12 employees"]
    assert private == ["Paul's salary is 7000"]


@pytest.mark.parametrize("text", [
    "1 " * 5000,
    "1'" * 5000,
    "9." * 5000,
    "a - " * 2000,
    "compte 6000 - " + "a " * 3000,
    "account 1000" + " - Ab Cd Ef" * 500,
    "CH93 " * 3000,
    "8.30-" * 3000,
    "in 2025 " * 2000,
])
def test_a_long_fact_is_checked_in_linear_time(text):
    started = time.monotonic()
    stays_private(text)
    assert time.monotonic() - started < 0.5
