# //// Neoffice — added file (no upstream equivalent): the chat model may not write the
# //// shared company bucket (#881).
"""mem0_add / mem0_conclude never write the shared company bucket.

Everything in that bucket is recalled by every colleague whatever their rights, and a
fact carries neither its author nor its source. On the development instance a
salesperson recalled a salary and a revenue figure the chat model had stored there. A
scope="company" from the model is not an error (the fact is worth keeping): it lands in
the caller's own bucket, and the tool result says so. The company bucket's only writer
is the trusted server path, retain_facts, behind the gateway's signed memory_retain.
"""

import json

import pytest

from plugins.memory.mem0 import TOOL_SCHEMAS, Mem0MemoryProvider

ME = "me@example.test"
COMPANY = "company"


class RecordingBackend:
    def __init__(self):
        self.adds = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.adds.append((user_id, [m["content"] for m in messages], infer))
        return {"event_id": "ev-1"}


def _provider(backend):
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id = ME
    provider._company_id = COMPANY
    provider._backend = backend
    return provider


def _call(provider, tool, args, **kwargs):
    return json.loads(provider.handle_tool_call(tool, args, **kwargs))


@pytest.mark.parametrize("tool,args", [
    ("mem0_add", {"content": "Our revenue was 1.2 million", "scope": "company"}),
    ("mem0_conclude", {"conclusion": "Our revenue was 1.2 million", "scope": "company"}),
    ("mem0_add", {"content": "Our revenue was 1.2 million", "scope": " Company "}),
    ("mem0_add", {"content": "Our revenue was 1.2 million", "scope": "COMPANY"}),
])
def test_a_company_scope_from_the_model_is_stored_in_the_callers_own_bucket(tool, args):
    backend = RecordingBackend()
    out = _call(_provider(backend), tool, args)
    assert backend.adds == [(ME, ["Our revenue was 1.2 million"], False)]
    assert "error" not in out
    assert out["scope"] == "user"
    assert "own memory" in out["note"], "the model is told where the fact went"


def test_a_plain_business_fact_is_not_shared_either():
    """The tool has no notion of what may be shared: no company write at all."""
    backend = RecordingBackend()
    _call(_provider(backend), "mem0_add", {"content": "We invoice on the 25th", "scope": "company"})
    assert [user for user, _, _ in backend.adds] == [ME]


def test_no_trust_flag_opens_the_company_bucket_to_the_tool():
    backend = RecordingBackend()
    _call(_provider(backend), "mem0_add", {"content": "the office moves in May", "scope": "company"},
          allow_company_bucket=True)
    assert [user for user, _, _ in backend.adds] == [ME]


@pytest.mark.parametrize("args", [
    {"content": "I prefer tea"},
    {"content": "I prefer tea", "scope": "user"},
    {"content": "I prefer tea", "scope": ""},
])
def test_a_personal_fact_answers_as_before(args):
    backend = RecordingBackend()
    out = _call(_provider(backend), "mem0_add", args)
    assert backend.adds == [(ME, ["I prefer tea"], False)]
    assert set(out) == {"result", "event_id"}, "no note when nothing was redirected"


def test_the_write_schema_no_longer_offers_a_company_scope():
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "mem0_add")
    assert set(schema["parameters"]["properties"]) == {"content"}
    assert "company" not in json.dumps(schema).lower()


def test_the_trusted_server_path_still_writes_the_company_bucket():
    backend = RecordingBackend()
    provider = _provider(backend)
    assert provider.retain_facts(["We invoice on the 25th"], scope="company") == 1
    assert provider.retain_facts(["I prefer tea"]) == 1
    assert [user for user, _, _ in backend.adds] == [COMPANY, ME]
