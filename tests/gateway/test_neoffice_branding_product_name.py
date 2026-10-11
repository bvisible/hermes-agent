# //// Neoffice — added file (no upstream equivalent): the customer's software has one name.
"""« ERPNext » reaches the customer as « Neoffice »; « frappe » the French word and ERPNextSwiss stay (11.10)."""
from neoffice_branding import strip_internal_mechanics


def test_the_underlying_product_is_named_neoffice():
    raw = "Vous y accédez via le bureau ERPNext, ou dans erpnext directement."
    assert strip_internal_mechanics(raw, "fr") == "Vous y accédez via le bureau Neoffice, ou dans Neoffice directement."


def test_ordinary_words_and_module_names_stay():
    text = "Une faute de frappe dans la référence ; le module ERPNextSwiss lit le relevé."
    assert strip_internal_mechanics(text, "fr") == text
