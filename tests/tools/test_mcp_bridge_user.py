# //// Neoffice — added file (no upstream equivalent): the gateway, not the model, names the
# //// person a neoffice-* MCP call acts for (#882).
"""`bridge_user` is written by the gateway on every call to a neoffice-* MCP server.

The servers build their body from `params` for kwargs tools and from the top-level
arguments for the others. Stamping only `params` let a model-supplied top-level value
reach a tool whose body is the top level, so the model could choose whose rights applied.
Now the person is written in both places, always (even empty), and a failure to resolve
the person names nobody rather than keeping what the model sent.
"""

import json

import pytest

import tools.mcp_tool_handlers as handlers
from tools.mcp_tool_handlers import _neoffice_stamp_bridge_user

PERSON = "person@example.test"
FORGED = "boss@example.test"


@pytest.fixture
def session(monkeypatch):
    """Bind the answering session's user the way the gateway does, then unbind it."""
    from gateway import session_context

    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    session_context.reset_session_vars()

    def bind(user_id):
        return session_context.set_session_vars(platform="webhook", user_id=user_id)

    yield bind
    session_context.reset_session_vars()


def test_top_level_and_params_both_carry_the_session_user(session):
    session(PERSON)
    args = {"bridge_user": FORGED, "params": {"doctype": "Salary Slip", "bridge_user": FORGED}}
    _neoffice_stamp_bridge_user("neoffice-rh", "list_documents", args)
    assert args["bridge_user"] == PERSON
    assert args["params"]["bridge_user"] == PERSON


def test_a_model_value_at_the_top_level_does_not_survive_next_to_params(session):
    """The #882 case: params is a dict, the model's value sits at the top level."""
    session(PERSON)
    args = {"bridge_user": FORGED, "params": {"doctype": "Salary Slip"}}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args["bridge_user"] == PERSON
    assert args["params"]["bridge_user"] == PERSON


def test_without_params_the_top_level_is_written(session):
    session(PERSON)
    args = {"doctype": "ToDo", "bridge_user": FORGED}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args == {"doctype": "ToDo", "bridge_user": PERSON}


def test_a_params_string_is_left_as_sent_and_the_top_level_is_written(session):
    session(PERSON)
    args = {"params": '{"doctype": "ToDo"}', "bridge_user": FORGED}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args["bridge_user"] == PERSON
    assert args["params"] == '{"doctype": "ToDo"}'


def test_no_session_user_writes_empty_everywhere(session, monkeypatch):
    session("")
    monkeypatch.setattr(handlers, "_kanban_task_user", lambda: "")
    args = {"bridge_user": FORGED, "params": {"bridge_user": FORGED}}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args["bridge_user"] == ""
    assert args["params"]["bridge_user"] == ""


@pytest.mark.parametrize("raw", ["webhook:nora_chat:abc", "not-an-email", "   "])
def test_a_value_that_is_not_a_person_names_nobody(session, monkeypatch, raw):
    session(raw)
    monkeypatch.setattr(handlers, "_kanban_task_user", lambda: "")
    args = {"bridge_user": FORGED, "params": {}}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args["bridge_user"] == ""
    assert args["params"]["bridge_user"] == ""


def test_a_pole_worker_falls_back_to_its_kanban_task(session, monkeypatch):
    session("")
    monkeypatch.setattr(handlers, "_kanban_task_user", lambda: PERSON)
    args = {"params": {"bridge_user": FORGED}}
    _neoffice_stamp_bridge_user("neoffice-projet", "log_time", args)
    assert args["bridge_user"] == PERSON
    assert args["params"]["bridge_user"] == PERSON


def test_a_resolution_failure_fails_closed(monkeypatch):
    """Upstream-style `except: pass` kept the model's value; now the call names nobody."""
    from gateway import session_context

    def boom(*_a, **_k):
        raise RuntimeError("context lost")

    monkeypatch.setattr(session_context, "get_session_env", boom)
    args = {"bridge_user": FORGED, "params": {"bridge_user": FORGED}}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args["bridge_user"] == ""
    assert args["params"]["bridge_user"] == ""


def test_a_kanban_lookup_failure_fails_closed(session, monkeypatch):
    session("")

    def boom():
        raise OSError("kanban.db locked")

    monkeypatch.setattr(handlers, "_kanban_task_user", boom)
    args = {"bridge_user": FORGED}
    _neoffice_stamp_bridge_user("neoffice-frappe", "frappe_get_list", args)
    assert args["bridge_user"] == ""


def test_other_servers_are_untouched(session):
    session(PERSON)
    args = {"bridge_user": "whatever", "params": {"bridge_user": "whatever"}}
    _neoffice_stamp_bridge_user("github", "create_issue", args)
    assert args == {"bridge_user": "whatever", "params": {"bridge_user": "whatever"}}


def test_the_registry_handler_stamps_before_any_transport_work(session, monkeypatch):
    """Through the real handler: the stamp happens before the trust gate, so even a call
    that is then refused never carried the model's identity anywhere."""
    session(PERSON)
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *_a: json.dumps({"error": "stop here"}))
    handler = handlers._make_tool_handler("neoffice-frappe", "frappe_get_list", 10.0)
    args = {"bridge_user": FORGED, "params": {"doctype": "Salary Slip"}}
    assert json.loads(handler(args)) == {"error": "stop here"}
    assert args["bridge_user"] == PERSON
    assert args["params"]["bridge_user"] == PERSON
