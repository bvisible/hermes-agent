# //// Neoffice — added file (no upstream equivalent): what the shared company memory may
# //// never hold (#881).
"""The shared company bucket never receives an amount of money, nor anything about pay.

That bucket is merged into what EVERY colleague recalls, whatever their rights, and a
stored fact carries neither its author nor the document that justifies it, so it cannot
be filtered by the reader's rights once stored. On the development instance a salesperson
with no access to invoices or payslips recalled a salary and a revenue figure through it.

NORA's nightly consolidation already keeps such facts out of the company bucket
(``nora/api/v2/memory.py``, ``_stays_private``). This is a LOCAL COPY of that filter, so
the gateway's ``memory_retain`` route enforces the rule itself instead of trusting
whoever signs the request. Keep the two patterns in step: a change there is a change
here. Deliberately wide: a business fact wrongly kept private costs nothing, a salary
shared with every colleague cannot be taken back.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

# An amount: a number next to a currency, the Swiss ".-", a magnitude word, a rate
# ("3000 per month"), or a Swiss thousands grouping ("7'500").
_MONEY = re.compile(
    r"(?:\b(?:chf|eur|usd|gbp|fr)\.?\s*[-+]?\d"
    r"|[€$£]\s*\d"
    r"|\d[\d'\u2019 ,.]*\s*(?:(?:chf|eur|usd|gbp|francs?|euros?|dollars?|frs?|mio|millions?|milliards?|billions?|k)\b"
    r"|[€$£]|\.[-\u2013])"
    r"|\d\s*(?:per|a|an|par|/)\s*(?:month|year|hour|day|mois|année|an|heure|jour)\b"
    r"|\b\d{1,3}(?:['\u2019]\d{3})+\b)",
    re.IGNORECASE,
)
# Pay, in the languages the facts come in (the extraction writes English, people do not).
_PAY = re.compile(
    r"\b(?:salar(?:y|ies|ied)|salaires?|wages?|payroll|pay\s?(?:slips?|checks?|days?|rises?|raises?)"
    r"|(?:net|gross)\s+pay|earn(?:s|ed|ing|ings)?|bonus(?:es)?|remunerat\w*|rémunér\w*|compensation"
    r"|fiches?\s+de\s+paie|bulletins?\s+de\s+(?:salaire|paie)|la\s+paie|lohn\w*|gehalt\w*|stipendi\w*"
    r"|13(?:th|e|ème)\s+(?:month|mois|salary|salaire))\b",
    re.IGNORECASE,
)


def stays_private(text: str) -> bool:
    """True when a fact must not reach the shared company bucket: an amount, or pay."""
    return bool(_MONEY.search(text or "") or _PAY.search(text or ""))


def split_company_facts(texts: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Split facts proposed for the company bucket into (shareable, kept private)."""
    shared: List[str] = []
    private: List[str] = []
    for text in texts:
        (private if stays_private(text) else shared).append(text)
    return shared, private
