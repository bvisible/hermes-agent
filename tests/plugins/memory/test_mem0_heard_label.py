# //// Neoffice — added file (no upstream equivalent): the recall says how often a company
# //// fact that conversations taught was heard (#958).
"""A business fact NORA learned in one person's chat is shared with every colleague as
heard once, and counts one more hearing each time another conversation restates it. The
recall says so, in the prefetch and in mem0_search / mem0_list, so that the model checks a
fact heard once and relies on one confirmed many times. A fact built by code from the ERP,
or a person's own memory, carries no label: it is not hearsay.
"""

import json

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider, _neoffice_heard_label, _neoffice_recall_line

ME = "me@example.test"
COMPANY = "company"

ONCE = {"id": "c1", "memory": "Our electricity supplier is DransGrid", "score": 0.9,
        "user_id": COMPANY, "metadata": {"channel": "webhook", "source": "conversation", "seen": 1}}
OFTEN = {"id": "c2", "memory": "We invoice on the 25th", "score": 0.8,
         "user_id": COMPANY, "metadata": {"source": "conversation", "seen": 4}}
FROM_THE_ERP = {"id": "c3", "memory": "Supplier DransGrid is booked to account 6400", "score": 0.7,
                "user_id": COMPANY, "metadata": {"channel": "webhook"}}
MINE = {"id": "u1", "memory": "I prefer short answers", "score": 0.6, "user_id": ME}


@pytest.mark.parametrize("item,label", [
    (ONCE, "company knowledge heard once in a conversation, not confirmed: check it before relying on it"),
    (OFTEN, "company knowledge confirmed in 4 conversations"),
    ({"metadata": {"source": "conversation", "seen": 2}}, "company knowledge confirmed in 2 conversations"),
    ({"metadata": {"source": "conversation"}}, "company knowledge heard once in a conversation, not confirmed: "
                                               "check it before relying on it"),
    ({"metadata": {"source": "conversation", "seen": "x"}}, "company knowledge heard once in a conversation, "
                                                            "not confirmed: check it before relying on it"),
    (FROM_THE_ERP, None),
    (MINE, None),
    ({"metadata": {"source": "erp", "seen": 9}}, None),
    ({"metadata": "conversation"}, None),
])
def test_the_label_says_how_often_a_fact_was_heard(item, label):
    assert _neoffice_heard_label(item) == label


def test_a_fact_without_label_reads_as_it_is():
    assert _neoffice_recall_line(FROM_THE_ERP) == FROM_THE_ERP["memory"]
    assert _neoffice_recall_line(OFTEN) == "We invoice on the 25th (company knowledge confirmed in 4 conversations)"


class BucketBackend:
    """search / get_all answer per bucket, like the OSS backend filtered by user_id."""

    def __init__(self):
        self.buckets = {ME: [MINE], COMPANY: [ONCE, OFTEN, FROM_THE_ERP]}

    def search(self, query, *, filters, top_k=10, rerank=False):
        return [dict(item) for item in self.buckets.get(filters["user_id"], [])]

    def get_all(self, *, filters, page=1, page_size=100):
        return {"results": [dict(item) for item in self.buckets.get(filters["user_id"], [])]}


def _provider():
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id = ME
    provider._company_id = COMPANY
    provider._company_recall = True
    provider._backend = BucketBackend()
    return provider


def _heard(results):
    return {item["id"]: item.get("heard") for item in results}


def test_mem0_search_tells_how_often_each_company_fact_was_heard():
    out = json.loads(_provider().handle_tool_call("mem0_search", {"query": "electricity"}))
    assert _heard(out["results"]) == {
        "c1": _neoffice_heard_label(ONCE),
        "c2": _neoffice_heard_label(OFTEN),
        "c3": None,
        "u1": None,
    }
    assert all("heard" not in item for item in out["results"] if item["id"] in ("c3", "u1")), \
        "no empty key on a memory that has no label"


def test_mem0_list_tells_it_too():
    out = json.loads(_provider().handle_tool_call("mem0_list", {}))
    assert _heard(out["results"]) == {
        "u1": None,
        "c1": _neoffice_heard_label(ONCE),
        "c2": _neoffice_heard_label(OFTEN),
        "c3": None,
    }


def test_the_prefetch_tells_it_to_the_model():
    body = _provider().prefetch("electricity")
    assert body.startswith("## Mem0 Memory\n")
    lines = body.splitlines()[1:]
    assert "- Our electricity supplier is DransGrid (company knowledge heard once in a conversation, " \
           "not confirmed: check it before relying on it)" in lines
    assert "- We invoice on the 25th (company knowledge confirmed in 4 conversations)" in lines
    assert "- Supplier DransGrid is booked to account 6400" in lines
    assert "- I prefer short answers" in lines
