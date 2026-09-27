# //// Neoffice — added file (no upstream equivalent): the gateway's memory_retain keeps
# //// amounts and pay out of the shared company bucket (#881).
"""memory_retain accepts scope="company" only for a fact that states no amount and
nothing about pay; any other fact goes to the named user's own bucket.

The company bucket is merged into what every colleague recalls, whatever their rights,
and a stored fact carries neither its author nor its source. NORA's nightly
consolidation already filters (``_stays_private``); the route now holds a signed request
to the same rule itself. No model, no store: the mem0 provider is replaced.
"""

import asyncio
import json

import pytest

pytest.importorskip("aiohttp")

import plugins.memory.mem0 as mem0_plugin  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.neoffice_memory_policy import split_company_facts, stays_private  # noqa: E402
from gateway.platforms.webhook import WebhookAdapter  # noqa: E402

USER = "jean@example.test"

# Same cases as NORA's own test of the filter, with neutral names.
PRIVATE = [
    "Marie earns CHF 8'500 a month",
    "Our 2025 revenue was 1.2 million CHF",
    "The rent is 3'000.- per month",
    "The rent is 3000 per month",
    "Paul's salary is 7000",
    "We charge 120 CHF/hour",
    "Invoice INV-1 is € 500",
    "Revenue reached 800k",
    "Payroll runs on the 25th",
    "The CEO earns more than the CTO",
    "Marie gets a bonus in December",
    "Le salaire de Marie est de 7000",
    "Le 13e salaire est versé en novembre",
    "Die Lohnabrechnung kommt am Monatsende",
]

SHARED = [
    "Our electricity supplier is the local utility",
    "We invoice on the 25th",
    "We book fuel to account 6200.",
    "Client X pays within 30 days",
    "We have 12 employees",
    "We give a 5% discount to resellers",
    "Marie handles the supplier invoices",
    "We bill in CHF",
    "The main supplier is Fr. Example AG",
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


# --- the route ---------------------------------------------------------------------------

class FakeProvider:
    calls = []
    store_fewer_by = 0

    def initialize(self, session_id, **kwargs):
        FakeProvider.calls.append(("init", kwargs.get("user_id")))
        self._user_id = kwargs.get("user_id")
        self._company_id = "company"

    def _own_bucket_is_company(self):
        return self._user_id == self._company_id

    def retain_facts(self, facts, *, scope="user"):
        FakeProvider.calls.append((scope, list(facts)))
        return max(0, len(facts) - FakeProvider.store_fewer_by)


@pytest.fixture
def provider(monkeypatch):
    FakeProvider.calls = []
    FakeProvider.store_fewer_by = 0
    monkeypatch.setattr(mem0_plugin, "Mem0MemoryProvider", FakeProvider)
    return FakeProvider


def _retain(payload):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    resp = asyncio.run(adapter._handle_memory_retain(payload))
    return resp.status, json.loads(resp.body)


def test_an_amount_proposed_for_the_company_lands_in_the_users_own_bucket(provider):
    status, body = _retain({"user": USER, "scope": "company", "facts": [
        {"text": "Marie earns CHF 8'500 a month"},
        {"text": "We invoice on the 25th"},
        {"text": "Payroll runs on the 25th"},
    ]})
    assert status == 200
    assert provider.calls == [
        ("init", USER),
        ("company", ["We invoice on the 25th"]),
        ("user", ["Marie earns CHF 8'500 a month", "Payroll runs on the 25th"]),
    ]
    assert body["stored"] == 3, "the total, as NORA counts it: a short count makes it retry forever"
    assert body["kept_private"] == 2


def test_a_company_batch_with_nothing_to_hold_back_writes_only_the_company(provider):
    status, body = _retain({"user": USER, "scope": "company", "facts": ["We bill in CHF"]})
    assert status == 200
    assert provider.calls == [("init", USER), ("company", ["We bill in CHF"])]
    assert body == {"status": "stored", "user": USER, "stored": 1, "kept_private": 0}


def test_a_company_batch_that_is_all_money_never_touches_the_company(provider):
    _, body = _retain({"user": USER, "scope": "company", "facts": ["Revenue reached 800k"]})
    assert provider.calls == [("init", USER), ("user", ["Revenue reached 800k"])]
    assert body["kept_private"] == 1


def test_a_user_batch_is_unchanged(provider):
    _, body = _retain({"user": USER, "facts": ["Paul's salary is 7000", "I prefer tea"]})
    assert provider.calls == [("init", USER), ("user", ["Paul's salary is 7000", "I prefer tea"])]
    assert body == {"status": "stored", "user": USER, "stored": 2, "kept_private": 0}


@pytest.mark.parametrize("scope", ["Company", " COMPANY "])
def test_the_company_scope_is_read_case_and_space_insensitively(provider, scope):
    _retain({"user": USER, "scope": scope, "facts": ["We bill in CHF", "Paul's salary is 7000"]})
    assert provider.calls[1:] == [("company", ["We bill in CHF"]), ("user", ["Paul's salary is 7000"])]


@pytest.mark.parametrize("scope", ["all", "shared", "companies", None, 1])
def test_any_other_scope_never_widens_the_audience(provider, scope):
    _retain({"user": USER, "scope": scope, "facts": ["We bill in CHF"]})
    assert provider.calls[1:] == [("user", ["We bill in CHF"])]


def test_stored_reports_what_the_store_actually_kept(provider):
    provider.store_fewer_by = 1
    _, body = _retain({"user": USER, "scope": "company", "facts": [
        "We bill in CHF", "We have 12 employees", "Paul's salary is 7000"]})
    assert body["stored"] == 1, "company 2-1 + user 1-1: a short store stays visible to the caller"


def test_a_throwaway_test_user_is_still_skipped(provider):
    _, body = _retain({"user": "probe-1", "scope": "company", "facts": ["Paul's salary is 7000"]})
    assert body["status"] == "skipped_test_user"
    assert provider.calls == []


# --- a `user` that IS the company bucket id ----------------------------------------------
# The company half of such a batch was written and the user half refused: the short count
# kept NORA's rows pending, so every nightly retry wrote the same company facts again.

@pytest.mark.parametrize("scope", ["company", "user"])
def test_a_user_named_like_the_company_is_refused_as_a_whole(provider, scope):
    status, body = _retain({"user": "company", "scope": scope, "facts": [
        "We invoice on the 25th", "Paul's salary is 7000"]})
    assert status == 403
    assert body["status"] == "refused" and body["stored"] == 0
    assert provider.calls == [("init", "company")], "not one write, company half included"


class RecordingBackend:
    def __init__(self):
        self.adds = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.adds.append((user_id, [m["content"] for m in messages]))
        return {"event_id": "ev-1"}


@pytest.fixture
def real_provider(monkeypatch):
    """The real provider, with a recording store and a mem0.json that names the company id."""
    backend = RecordingBackend()
    config = {"company_id": "company"}
    monkeypatch.setattr(mem0_plugin, "_load_config", lambda: dict(config))
    monkeypatch.setattr(mem0_plugin.Mem0MemoryProvider, "_create_backend", lambda self: backend)
    return backend, config


def test_the_real_provider_writes_nothing_for_a_user_named_like_the_company(real_provider):
    backend, _ = real_provider
    status, _ = _retain({"user": "company", "scope": "company", "facts": [
        "We invoice on the 25th", "Paul's salary is 7000"]})
    assert status == 403
    assert backend.adds == []


def test_the_configured_company_id_is_the_one_refused(real_provider):
    backend, config = real_provider
    config["company_id"] = "acme-shared"
    assert _retain({"user": "acme-shared", "facts": ["I prefer tea"]})[0] == 403
    assert backend.adds == []
    status, body = _retain({"user": USER, "scope": "company", "facts": ["We bill in CHF"]})
    assert (status, body["stored"]) == (200, 1)
    assert backend.adds == [("acme-shared", ["We bill in CHF"])]
