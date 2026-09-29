# //// Neoffice — added file (no upstream equivalent): who may change a memory, and the
# //// per-site switch for the shared company bucket (#881).
"""mem0_update / mem0_delete act only on the caller's own memories, and `company_recall`
takes the shared company bucket out of every read.

Upstream handed any memory id straight to the backend: a user could rewrite or erase a
colleague's memory, or a fact every colleague recalls, by naming its id — and ids are
shown by mem0_search / mem0_list, company ones included. The company bucket has no notion
of who may manage it, so the chat tools may not touch it; only the gateway's signed
memory_forget route (NORA's nightly pass) says it is trusted.
"""

import asyncio
import json

import pytest

import plugins.memory.mem0 as mem0_plugin
from plugins.memory.mem0 import Mem0MemoryProvider, _config_flag
from plugins.memory.mem0._backend import Mem0Backend, OSSBackend, PlatformBackend, SelfHostedBackend

ME = "me@example.test"
COLLEAGUE = "colleague@example.test"
COMPANY = "company"


class OwnedBackend:
    """A store that knows whose bucket each memory sits in."""

    def __init__(self, rows=None, get_error=None):
        self.rows = {r["id"]: dict(r) for r in (rows or [
            {"id": "mine", "memory": "I prefer tea", "user_id": ME},
            {"id": "theirs", "memory": "my salary is private", "user_id": COLLEAGUE},
            {"id": "shared", "memory": "the office opens at eight", "user_id": COMPANY},
        ])}
        self.get_error = get_error
        self.writes = []
        self.filters = []

    def get(self, memory_id):
        if self.get_error is not None:
            raise self.get_error
        return self.rows.get(memory_id)

    def search(self, query, *, filters, top_k=10, rerank=False):
        self.filters.append(("search", filters))
        return [dict(r, score=0.5) for r in self.rows.values() if r["user_id"] == filters["user_id"]]

    def get_all(self, *, filters, page=1, page_size=100):
        self.filters.append(("get_all", filters))
        rows = [r for r in self.rows.values() if r["user_id"] == filters["user_id"]]
        return {"results": rows, "count": len(rows)}

    def update(self, memory_id, text):
        self.writes.append(("update", memory_id, text))
        return {"result": "Memory updated.", "memory_id": memory_id}

    def delete(self, memory_id):
        self.writes.append(("delete", memory_id))
        return {"result": "Memory deleted.", "memory_id": memory_id}


def _provider(backend, *, company_recall=True):
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id = ME
    provider._company_id = COMPANY
    provider._company_recall = company_recall
    provider._backend = backend
    return provider


def _call(provider, tool, args, **kwargs):
    return json.loads(provider.handle_tool_call(tool, args, **kwargs))


# --- update / delete: own bucket only --------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("mem0_update", {"memory_id": "mine", "text": "I prefer coffee"}),
    ("mem0_delete", {"memory_id": "mine"}),
])
def test_the_caller_changes_their_own_memory(tool, args):
    backend = OwnedBackend()
    out = _call(_provider(backend), tool, args)
    assert "error" not in out
    assert backend.writes and backend.writes[0][1] == "mine"


@pytest.mark.parametrize("tool,args", [
    ("mem0_update", {"memory_id": "theirs", "text": "rewritten"}),
    ("mem0_delete", {"memory_id": "theirs"}),
])
def test_a_colleagues_memory_answers_not_found_and_is_left_alone(tool, args):
    backend = OwnedBackend()
    out = _call(_provider(backend), tool, args)
    assert out["error"] == "Memory not found: theirs", "the same answer as an id that does not exist"
    assert backend.writes == []


@pytest.mark.parametrize("tool,args", [
    ("mem0_update", {"memory_id": "shared", "text": "the office never opens"}),
    ("mem0_delete", {"memory_id": "shared"}),
])
def test_the_chat_tools_cannot_change_a_company_memory(tool, args):
    backend = OwnedBackend()
    out = _call(_provider(backend), tool, args)
    assert "shared with the whole company" in out["error"]
    assert backend.writes == []


def test_a_trusted_server_caller_may_retire_a_company_memory():
    backend = OwnedBackend()
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "shared"}, allow_company_bucket=True)
    assert "error" not in out
    assert backend.writes == [("delete", "shared")]


def test_the_trusted_flag_never_opens_another_users_bucket():
    backend = OwnedBackend()
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "theirs"}, allow_company_bucket=True)
    assert out["error"] == "Memory not found: theirs"
    assert backend.writes == []


@pytest.mark.parametrize("flag", ["yes", 1, "true"])
def test_only_a_literal_true_counts_as_trusted(flag):
    backend = OwnedBackend()
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "shared"}, allow_company_bucket=flag)
    assert "error" in out
    assert backend.writes == []


def test_the_model_cannot_claim_trust_through_its_arguments():
    backend = OwnedBackend()
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "shared", "allow_company_bucket": True})
    assert "error" in out
    assert backend.writes == []


def test_an_unknown_id_is_not_found():
    backend = OwnedBackend()
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "nowhere"})
    assert out["error"] == "Memory not found: nowhere"
    assert backend.writes == []


def test_a_memory_without_an_owner_is_refused():
    backend = OwnedBackend(rows=[{"id": "orphan", "memory": "no owner", "user_id": ""}])
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "orphan"})
    assert "error" in out
    assert backend.writes == []


def test_a_backend_that_cannot_read_one_memory_refuses_the_write():
    class NoGet(OwnedBackend):
        get = None

    backend = NoGet()
    out = _call(_provider(backend), "mem0_update", {"memory_id": "mine", "text": "x"})
    assert "Cannot check who owns this memory" in out["error"]
    assert backend.writes == []


def test_the_base_backend_get_refuses_rather_than_raising_through():
    class Minimal(Mem0Backend):
        def search(self, query, *, filters, top_k=10, rerank=False):
            return []

        def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
            return {}

        def _update(self, memory_id, text):
            raise AssertionError("must not be reached")

        def _delete(self, memory_id):
            raise AssertionError("must not be reached")

    provider = _provider(Minimal())
    out = _call(provider, "mem0_delete", {"memory_id": "mine"})
    assert "Cannot check who owns this memory" in out["error"]
    assert provider._consecutive_failures == 0, "a missing capability is not an outage"


def test_a_failing_ownership_lookup_fails_closed():
    backend = OwnedBackend(get_error=RuntimeError("connection reset"))
    out = _call(_provider(backend), "mem0_update", {"memory_id": "mine", "text": "x"})
    assert "error" in out
    assert backend.writes == []


def test_a_not_found_from_the_lookup_is_answered_like_any_unknown_id():
    backend = OwnedBackend(get_error=RuntimeError("404 Not Found"))
    out = _call(_provider(backend), "mem0_delete", {"memory_id": "gone"})
    assert out["error"] == "Memory not found: gone"
    assert backend.writes == []


# --- company_recall ---------------------------------------------------------------------

def _read_buckets(backend):
    return {f["user_id"] for _, f in backend.filters}


def test_reads_merge_the_company_bucket_by_default():
    backend = OwnedBackend()
    provider = _provider(backend)
    found = _call(provider, "mem0_search", {"query": "office"})
    assert {r["id"] for r in found["results"]} == {"mine", "shared"}
    assert _read_buckets(backend) == {ME, COMPANY}
    listed = _call(provider, "mem0_list", {})
    assert {r["id"] for r in listed["results"]} == {"mine", "shared"}


def test_company_recall_off_reads_only_the_callers_bucket():
    backend = OwnedBackend()
    provider = _provider(backend, company_recall=False)
    found = _call(provider, "mem0_search", {"query": "office"})
    assert [r["id"] for r in found["results"]] == ["mine"]
    listed = _call(provider, "mem0_list", {})
    assert [r["id"] for r in listed["results"]] == ["mine"]
    assert _read_buckets(backend) == {ME}, "the company bucket is never even queried"


def test_company_recall_off_still_stores_a_company_fact():
    """The switch is about reads only: what the bucket holds is a separate decision.
    The company bucket's only writer is the trusted server path (retain_facts, behind the
    gateway's memory_retain); the chat tool no longer writes it (#881)."""
    backend = OwnedBackend()
    added = []
    backend.add = lambda messages, **kw: added.append(kw["user_id"]) or {}
    provider = _provider(backend, company_recall=False)
    assert provider.retain_facts(["the office moves in May"], scope="company") == 1
    assert added == [COMPANY]


def test_company_recall_is_read_from_mem0_json(monkeypatch):
    monkeypatch.setattr(mem0_plugin, "_load_config", lambda: {
        "mode": "platform", "api_key": "", "host": "", "agent_id": "hermes", "oss": {},
        "company_recall": "false"})
    monkeypatch.setattr(Mem0MemoryProvider, "_create_backend", lambda self: None)
    provider = Mem0MemoryProvider()
    provider.initialize("s", user_id=ME)
    assert provider._company_recall is False
    assert provider._scoped_read_buckets() == [{"user_id": ME}]


def test_company_recall_defaults_to_the_current_behaviour(monkeypatch):
    monkeypatch.setattr(mem0_plugin, "_load_config", lambda: {
        "mode": "platform", "api_key": "", "host": "", "agent_id": "hermes", "oss": {}})
    monkeypatch.setattr(Mem0MemoryProvider, "_create_backend", lambda self: None)
    provider = Mem0MemoryProvider()
    provider.initialize("s", user_id=ME)
    assert provider._company_recall is True
    assert provider._scoped_read_buckets() == [{"user_id": ME}, {"user_id": COMPANY}]


@pytest.mark.parametrize("value,expected", [
    (None, True), ("", True), (True, True), (False, False), (0, False), (1, True),
    ("false", False), ("False ", False), ("0", False), ("no", False), ("off", False),
    ("true", True), ("1", True), ("yes", True),
])
def test_config_flag(value, expected):
    assert _config_flag(value, default=True) is expected


# --- the backends' single read ----------------------------------------------------------

def test_platform_backend_get_reads_one_memory():
    calls = []

    class Client:
        def get(self, **kwargs):
            calls.append(kwargs)
            return {"id": kwargs["memory_id"], "user_id": ME}

    backend = PlatformBackend.__new__(PlatformBackend)
    backend._client = Client()
    assert backend.get("m1") == {"id": "m1", "user_id": ME}
    assert calls == [{"memory_id": "m1"}]


def test_oss_backend_get_delegates_to_memory_get():
    class Memory:
        def get(self, memory_id):
            return None if memory_id == "missing" else {"id": memory_id, "user_id": ME}

    backend = OSSBackend.__new__(OSSBackend)
    backend._memory = Memory()
    assert backend.get("m1")["user_id"] == ME
    assert backend.get("missing") is None


def test_selfhosted_backend_get_uses_the_memory_route():
    httpx = pytest.importorskip("httpx")
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/memories/missing":
            return httpx.Response(404, json={"detail": "Memory not found"})
        return httpx.Response(200, json={"id": "m1", "memory": "tea", "user_id": ME})

    backend = SelfHostedBackend("key", "http://sh:8888", transport=httpx.MockTransport(handler))
    assert backend.get("m1")["user_id"] == ME
    assert seen == [("GET", "/memories/m1")]
    with pytest.raises(httpx.HTTPStatusError):
        backend.get("missing")


# --- the gateway's memory_forget is the trusted caller ----------------------------------

def test_memory_forget_webhook_passes_the_trusted_flag(monkeypatch):
    pytest.importorskip("aiohttp")
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    seen = []

    class FakeProvider:
        def initialize(self, session_id, **kwargs):
            seen.append(("init", kwargs.get("user_id")))

        def handle_tool_call(self, tool_name, args, **kwargs):
            seen.append((tool_name, args["memory_id"], kwargs.get("allow_company_bucket")))
            return json.dumps({"result": "Memory deleted."})

    monkeypatch.setattr(mem0_plugin, "Mem0MemoryProvider", FakeProvider)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    resp = asyncio.run(adapter._handle_memory_forget({"user": ME, "ids": ["shared"]}))
    assert resp.status == 200
    assert seen == [("init", ME), ("mem0_delete", "shared", True)]
