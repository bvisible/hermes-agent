# //// Neoffice — added file (no upstream equivalent): what the shared company memory may
# //// never hold (#881). A VERBATIM copy of NORA's nora/utils/memory_privacy.py (tabs made
# //// spaces), so the gateway's memory_retain holds a signed request to the rule itself
# //// instead of trusting whoever signs it. Only two things differ: this header, and
# //// split_company_facts at the end (the name gateway/platforms/webhook.py imports).
# //// Both copies load the same examples: tests/gateway/neoffice_memory_privacy_vectors.json
# //// is a byte-identical copy of NORA's nora/utils/memory_privacy_vectors.json (the
# //// docstring's "next to this file"). Change the rule and the examples in BOTH repos.
"""What never reaches the shared "company" memory: default-deny (#881).

The company bucket is merged into what EVERY colleague recalls, whatever their rights, and
a stored fact carries neither its author nor the document that justifies it, so it cannot
be filtered by rights once stored. On the development instance a salesperson with no access
to invoices or payslips recalled a salary and a revenue figure through it.

Every write to the company bucket goes through this rule: the nightly consolidation
(nora.api.v2.memory), the supplier habits read from the ERP (nora.api.memory_bootstrap),
the facts of the business contexts (nora.api.suggestion_engine.write_company_memory), and,
last, the gateway client itself (nora.integrations.hermes.client.retain_memory), which
refuses a private fact in the company scope whoever sends it.

Why default-deny. The first two versions described what an amount looks like (a currency, a
rate, a grouping, a money word within three words of a number) and shared everything else.
Each review found amounts going through: "Paul gets 8000", "Stundensatz 120", "Sales 2025
were 850000", and "Paul is paid 7000 - since March - as agreed", which the account-name
exception of the time read as the account "7000 - since March - as". People write money in
more ways than a list can hold, so the rule now lists what a number may be and still be
shared, and keeps everything else private:

  (a) a number of 100 or more, or one written with a grouping, decimals or a magnitude
      (1'200, 1 250 000, 1,200, 45.50, 1.2M, 800k, 3 Mio, "two million"), keeps the fact
      private, unless it is one of the shapes below;
  (b) ANY number, however small, in the same sentence as a money or pay word (revenue,
      sales, rent, rate, doit, schuldet, Stundensatz...) keeps it private;
  (c) as before, a currency or a rate next to a number ("CHF 50", "50.-", "8000 net",
      "3000 per month") and any word about pay (salary, payroll, bonus...) keep it private,
      with or without a number.

The shapes of a number that is no amount, recognised and masked before (a) and (b):

  * a calendar year 1900-2099 introduced as one: "in 1998", "since 2010", "mars 2025",
    "FY 2025". A bare "2000" may be an amount, and stays one;
  * a full date: 31.12.2025, 2025-12-31, 31/12/2025;
  * a clock time with an hour word or "h" next to it: 8h30, "8.30 h", "hours 8.30-17.30";
  * a Swiss phone number (021 555 12 34, +41 21 555 12 34), an IBAN, a Swiss company id
    (CHE-123.456.789);
  * a percentage (8.1%, 7.7 %);
  * a postal code followed by a place name, where an address stands (first, after a comma,
    "CH-" or a place preposition): "1003 Lausanne", "Rue du Lac 12, 1003 Lausanne";
  * an account number right after an account word ("compte 6000", "account no. 6200",
    "Konto 1020"), with the ERPNext name that completes it ("compte 6000 - Loyer - ABC")
    when only punctuation, a parenthesis or the end of the sentence follows that name.

A masked year, date, time, phone number or postal code still counts as a number for (b):
"the rent is 2000" and "revenue in 2025" stay private. An account number does not, so
"Le fournisseur X doit être imputé sur le compte 4200" is shared; nor does a percentage,
so "The VAT rate is 8.1%" is shared, except in a sentence about a margin or a profit
("Our margin is 32.50%").

What this costs, on purpose: a reference, an item code or a street number of 100 or more,
a decimal that is not money ("version 2.1"), a date without its year ("31.12"), a time with
no hour word ("at 8.30") are kept for the person who produced them instead of every
colleague. A recorded imputation choice is shared ("l'utilisateur retient 6000 - Loyer -
ABC"): accounting self-learning is company knowledge. Wrongly private costs recall; wrongly shared
cannot be taken back.

The examples that pin the rule live in memory_privacy_vectors.json next to this file. The
gateway keeps its own copy of the rule (hermes fork, gateway/neoffice_memory_policy.py),
meant to run the same examples, so this module imports nothing but `re`: the copy can be
verbatim.
"""

from __future__ import annotations

import re

# ── (c) Private with or without a number: pay, a currency or a rate next to a number ──

# Pay, in the languages the facts come in (the extraction writes English, people and the
# ERP's own labels do not).
_PAY = re.compile(
    r"\b(?:salar(?:y|ies|ied)|salaires?|wages?|payroll|pay\s?(?:slips?|checks?|days?|rises?|raises?)"
    r"|(?:net|gross)\s+pay|earn(?:s|ed|ing|ings)?|bonus(?:es)?|remunerat\w*|rémunér\w*|compensation"
    r"|fiches?\s+de\s+paie|bulletins?\s+de\s+(?:salaire|paie)|la\s+paie|lohn\w*|gehalt\w*|stipendi\w*"
    r"|13(?:th|e|ème)\s+(?:month|mois|salary|salaire))\b",
    re.IGNORECASE,
)

# A number next to a currency, the Swiss ".-", a magnitude word, or a rate. The digits and
# separators of one amount are bounded: unbounded, a long run of "1 1 1 ..." made the
# search quadratic (3 s on 10 KB).
_MONEY = re.compile(
    r"\b(?:chf|eur|usd|gbp|fr)\.?\s*[-+]?\d"
    r"|[€$£]\s*\d"
    r"|\d[\d'\u2019 ,.]{0,24}\s*(?:(?:chf|eur|usd|gbp|francs?|franken|euros?|dollars?|frs?|centimes?|cts|rappen|cents?"
    r"|mio|millions?|milliards?|billions?|thousands?|tausend|k)\b|[€$£]|\.[-\u2013])"
    r"|\d\s*(?:(?:net|gross|brut|netto|brutto)\s+)?(?:per|a|an|par|/|pro)\s*"
    r"(?:month|year|hour|day|mois|année|an|heure|jour|monat|jahr|stunde|tag)\b"
    r"|\d\s*(?:net|gross|brut|netto|brutto)\b"
    r"|\d\s*(?:monthly|yearly|annually|weekly|hourly|mensuel(?:le)?s?|annuel(?:le)?s?|monatlich|jährlich"
    r"|mensil[ei]|annual[ei])\b",
    re.IGNORECASE,
)

# ── The shapes of a number that is no amount, masked before (a) and (b) ─────────────────

# A masked number that still counts as a number next to a money word (b)...
_NUMBER_MARK = " \u27e6n\u27e7 "
# ...and the two that do not: an account number, and a percentage (see _RATIO_WORD).
_ACCOUNT_MARK = " \u27e6a\u27e7 "
_PERCENT_MARK = " \u27e6p\u27e7 "

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")

_COMPANY_ID = re.compile(
    r"\bCHE[-\s.]?\d{3}[.\s]?\d{3}[.\s]?\d{3}\b(?:\s?(?:MWST|TVA|IVA|VAT|HR)\b)?", re.IGNORECASE
)

# 021 555 12 34, 0215551234, 0800 123 456, +41 21 555 12 34, 0041 (0)21 555 12 34.
_PHONE = re.compile(
    r"(?<![\w.,'\u2019+])(?:(?:\+|00)41[\s./-]?(?:\(0\)[\s./-]?)?[1-9]\d|0[1-9]\d)(?:[\s./-]?\d){7}"
    r"(?!\d|[.,'\u2019]\d)"
)

_DATE = re.compile(
    r"(?<![\d.,'\u2019/-])(?:0?[1-9]|[12]\d|3[01])([./-])(?:0?[1-9]|1[0-2])\1(?:19|20)?\d{2}(?!\d|[.,'\u2019/-]\d)"
    r"|(?<![\d.,'\u2019/-])(?:19|20)\d{2}([./-])(?:0?[1-9]|1[0-2])\2(?:0?[1-9]|[12]\d|3[01])(?!\d|[.,'\u2019/-]\d)"
)

# A time is only a time next to an hour word: "8.30" alone may be CHF 8.30. The word is
# kept, the time masked: "taux horaire: 12.50" keeps its money word.
_CLOCK = r"(?:[01]?\d|2[0-3])[.:][0-5]\d"
# One time or one range, bounded: an unbounded chain of "8.30-8.30-..." made the search quadratic.
_CLOCK_RANGE = rf"(?<![\d.,'\u2019]){_CLOCK}(?:\s*[-\u2013]\s*{_CLOCK})?(?!\d|[.,'\u2019]\d)"
_TIME_WITH_H = re.compile(
    r"(?<![\w.,'\u2019])(?:[01]?\d|2[0-3])\s?h\s?(?:[0-5]\d)?(?!\w|[.,'\u2019]\d)", re.IGNORECASE
)
_TIME_AFTER_WORD = re.compile(
    rf"(\b(?:hours?|heures?|horaires?|uhr|zeiten|öffnungszeiten|orari[oi]?|ore|time)\s*:?\s*)({_CLOCK_RANGE})",
    re.IGNORECASE,
)
_TIME_BEFORE_WORD = re.compile(
    rf"({_CLOCK_RANGE})(\s*(?:h|hrs?|hours?|heures?|uhr|ore|o['\u2019]clock|am|pm|a\.m\.|p\.m\.)(?!\w))",
    re.IGNORECASE,
)

_PERCENT = re.compile(
    r"(?<![\w.,'\u2019])\d{1,3}(?:[.,]\d{1,2})?\s?"
    r"(?:%|\u2030|(?:pour\s?cent|percent|per\s?cent|prozent|per\s?cento)\b)",
    re.IGNORECASE,
)

# "compte 6000", "compte n° 6000", "account #6200", "Konto 1020", with the ERPNext name that
# completes it ("6000 - Loyer", "6000 - Loyer - ABC") only when nothing but punctuation, a
# parenthesis or the end follows: a name followed by more words is a sentence, and keeps
# its money word ("compte 6000 - Loyer de 50" stays private).
_LABEL_WORD = r"[^\W\d_]+"
_ACCOUNT = re.compile(
    r"\b(?:comptes?|cptes?|cpt|accounts?|acct|konten|konto|kto|conti|conto)\.?[ \t]*"
    r"(?:(?:n[°ºo]|nr|no|num(?:éro|ero|ber|mer)?|#)\.?[ \t]*)?"
    r"(?<![\d.,'\u2019])\d{3,6}(?!\d|[.,'\u2019]\d)"
    rf"(?:\s[-\u2013]\s{_LABEL_WORD}(?:[ '\u2019&/-]{{1,3}}{_LABEL_WORD}){{0,6}}(?=\s*(?:[(\[.,;:!?\n]|$)))?",
    re.IGNORECASE,
)
# The account of an imputation choice, in the words our own code writes it (record_choice):
# "l'utilisateur retient 6000 - Loyer - ABC", "NORA proposait 6100 - Entretien - ABC". The
# ERPNext name is required: "l'utilisateur retient 6000 CHF" stays private. Accounting
# self-learning is company knowledge (Jeremy, 2026-09-29), and pass 3 had kept it private.
_CHOICE_ACCOUNT = re.compile(
    r"\b(?:l['\u2019]utilisateur\s+retient|nora\s+proposait)[ \t]+"
    r"(?<![\d.,'\u2019])\d{3,6}(?!\d|[.,'\u2019]\d)"
    rf"\s[-\u2013]\s{_LABEL_WORD}(?:[ '\u2019&/-]{{1,3}}{_LABEL_WORD}){{0,6}}(?=\s*(?:[()\[.,;:!?\n\u2013\u2014]|$))",
    re.IGNORECASE,
)

# A postal code is a postal code when a place name follows it ("1003 Lausanne") AND it
# stands where an address does: first, after a comma or a parenthesis, after "CH-", or
# after a place preposition. German capitalises every noun: "Paul bekommt 8000 Prämie"
# is followed by a capital too, and is no address.
_POSTAL_CODE = re.compile(
    r"(^\s*|[,;(\n]\s*|\b(?:CH|FL|F|D|I|A)-|(?i:\b(?:in|à|a|en|nach|bei|zu|near|at|to|from|di)\s+))"
    r"([1-9]\d{3})(?=\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ])"
)

# A year is a year when something introduces it as one. The cue is kept, the year masked.
_MONTHS = (
    r"january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec"
    r"|janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre"
    r"|decembre|januar|februar|märz|maerz|juni|juli|oktober|dezember"
    r"|gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|dicembre"
)
_YEAR_CUES = (
    r"in|en|im|nel|nell['\u2019]|since|depuis|seit|dès|dal|until|till|bis|jusqu['\u2019]en|before|after"
    r"|avant|après|vor|nach|early|late|mid|fin|début|ende|anfang|years?|années?|l['\u2019]an|jahre?s?"
    r"|anno|exercice|fiscal|fy|geschäftsjahr|esercizio|founded|fondée?s?|gegründet|fondata|established|créée?s?"
)
_YEAR = re.compile(
    rf"(\b(?:{_YEAR_CUES}|{_MONTHS})\.?[ \t-]+|\bfy)"
    r"(?<![\d.,'\u2019])((?:19|20)\d{2}(?:\s?[-\u2013/]\s?(?:19|20)?\d{2})?)(?![\w%]|[.,'\u2019]\d)",
    re.IGNORECASE,
)


def _keep_first_group(mark):
    return lambda match: match.group(1) + mark


def _keep_second_group(mark):
    return lambda match: mark + match.group(2)


# In this order: an IBAN or a date holds what a phone number or a year would match.
_SHAPES = (
    (_IBAN, _NUMBER_MARK),
    (_COMPANY_ID, _NUMBER_MARK),
    (_PHONE, _NUMBER_MARK),
    (_DATE, _NUMBER_MARK),
    (_TIME_WITH_H, _NUMBER_MARK),
    (_TIME_AFTER_WORD, _keep_first_group(_NUMBER_MARK)),
    (_TIME_BEFORE_WORD, _keep_second_group(_NUMBER_MARK)),
    (_PERCENT, _PERCENT_MARK),
    (_ACCOUNT, _ACCOUNT_MARK),
    (_CHOICE_ACCOUNT, _ACCOUNT_MARK),
    (_POSTAL_CODE, _keep_first_group(_NUMBER_MARK)),
    (_YEAR, _keep_first_group(_NUMBER_MARK)),
)

# ── (a) and (b), on what is left ─────────────────────────────────────────────────────────

# (a) A number of 100 or more (and any run of three digits: a zero-padded code, a group of a
# number written "1 050"), a grouping or decimals, a magnitude. Case matters for the
# suffix: "5 m" is five metres, "5 M" five million.
_AMOUNT_SHAPE = re.compile(
    r"\d{3}"
    r"|\d[.,'\u2019\u02bc]\d"
    r"|\d\s?(?:[kK]|M|Mio|Mrd|Mia|bn|Bn)\b"
    r"|(?i:\b(?:hundreds?|hundert|thousands?|tausend|mille|mila|millions?|millionen|milioni?|milliards?"
    r"|milliarden|miliardi?|billions?|mio|mrd)\b)"
)

# (b) A money or pay word. "CA" (chiffre d'affaires) only in capitals: "ca." is "circa".
_MONEY_WORDS = (
    r"revenues?|turnover|chiffres?\s+d['\u2019]\s?affaires|balances?|soldes?|paid|pay"
    r"|gagn(?:e|es|ent|ons|ez|er|é|ée|ées|és)|touch(?:e|es|ent|ons|ez|er|é|ée|ées|és)|verdien\w*"
    r"|salaires?|salar(?:y|ies)|wages?|lohn\w*|gehalt\w*|profits?|margins?|marges?|prices?|priced|prix"
    r"|costs?|co[uû]t(?:s|e|es|ent|é|ée|ées|és)?|montants?|amounts?|budgets?|loyers?|rents?|rented"
    r"|sales|ventes|recettes|b[ée]n[ée]fices?|capital|capitaux|liquidit(?:é|és|y|ies|ät|äten|à)"
    r"|expenses?|expenditures?|spent|spend(?:s|ing)?|d[ée]penses?|d[ée]pens[ée](?:e|es|s)?"
    r"|devis|s['\u2019]\s?él[eè]v\w*|tarif\w*|tariff\w*|rates?|taux\s+horaires?|stundens[aä]tz\w*"
    r"|doi(?:t|vent)|owe[sd]?|owing|schuld\w*|debts?|dettes?|loans?|kredit\w*|acomptes?|deposits?"
    r"|umsatz\w*|verk[aä]uf(?:e|en|szahlen)|einnahmen|ausgaben|kapital\w*|preis\w*|kosten|miete\w*"
    r"|betr[aä]g\w*|gewinn\w*|fatturat[oi]|vendite|ricavi|entrate|spese|capitale|prezz[oi]|cost[oi]"
    r"|affitt[oi]|import[oi]|guadagn\w*|pay(?:é|ée|és|ées)|bezahlt|pagat[oaie]|fees?|honorair\w*"
    r"|honorar\w*|gebühr\w*|worth|income|earnings?|ebitda|ebit|tr[ée]sorerie|treasury|cash\w*"
    r"|kost(?:et|ete|eten)|zahl(?:en|t|te|ten)|allowances?|indemnit\w*|zulage\w*|prämie\w*|primes?"
    r"|gratifi[ck]ation\w*|commissions?|provisionen"
    # A currency is a money word too: "CHF: 50", "Prix en EUR 45".
    r"|chf|eur|usd|gbp|francs?|franken|euros?|dollars?|rappen|centimes?"
)
_MONEY_WORD = re.compile(rf"(?i:\b(?:{_MONEY_WORDS})\b)|[€$£]|\bCA\b")

# What makes a percentage an amount after all: a margin, a profit.
_RATIO_WORD = re.compile(
    r"\b(?:margins?|marges?|margine|profits?|b[ée]n[ée]fices?|gewinn\w*|utile|rendements?|rendite|returns?"
    r"|yield|ebitda|ebit|rentabilit\w*|profitabilit\w*|redditivit\w*)\b",
    re.IGNORECASE,
)

# A number of any kind, masked or not, that counts for (b).
_COUNTS = re.compile(rf"\d|{_NUMBER_MARK.strip()}")

# A sentence ends at a full stop, a question or exclamation mark followed by a capital, or
# a line break: "approx. 800000" and "ca. 30" do not end one.
_SENTENCE_END = re.compile(r"(?<=[.!?\u2026])\s+(?=[\"'\u00ab\u201c\u2018(\[]?[A-ZÀ-ÖØ-Þ])|\n+")

_NOT_A_PERSON = {"", "administrator", "guest"}


def _mask(text: str) -> str:
    """The fact with every number that is no amount replaced by a mark (module docstring)."""
    for pattern, mark in _SHAPES:
        text = pattern.sub(mark, text)
    return text


def _money_next_to_a_number(sentence: str) -> bool:
    """(b): a money word and a number in the same sentence."""
    if not _MONEY_WORD.search(sentence):
        return False
    if _COUNTS.search(sentence):
        return True
    return _PERCENT_MARK.strip() in sentence and bool(_RATIO_WORD.search(sentence))


def stays_private(text: str) -> bool:
    """True when a fact must not reach the shared company bucket (module docstring)."""
    text = str(text or "")
    if _PAY.search(text) or _MONEY.search(text):
        return True
    masked = _mask(text)
    if _AMOUNT_SHAPE.search(masked):
        return True
    return any(_money_next_to_a_number(sentence) for sentence in _SENTENCE_END.split(masked))


def split_private(facts) -> tuple[list[str], list[str]]:
    """(facts the company bucket may hold, facts that stay with a person)."""
    shared, private = [], []
    for fact in facts or []:
        (private if stays_private(fact) else shared).append(fact)
    return shared, private


def is_person(user) -> bool:
    """Whether `user` is someone a private fact can be kept for.

    Administrator and Guest are not: a fact read from the ERP by a job running as
    Administrator belongs to nobody, and is dropped rather than filed under that account."""
    return str(user or "").strip().lower() not in _NOT_A_PERSON


# //// Neoffice — the gateway's name for split_private (gateway/platforms/webhook.py).
def split_company_facts(texts) -> tuple[list[str], list[str]]:
    """Split facts proposed for the company bucket into (shareable, kept private)."""
    return split_private(texts)
