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
    assert args == {"doctype": "ToDo", "bridge_user": PERSON, "bridge_conversation": ""}


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


def test_the_administrator_is_a_person_the_server_can_name(session):
    """#1508: the Administrator has no « @ », and « mets vingt pour cent dessus » said by it was refused as if nobody asked."""
    session("Administrator")
    args = {"bridge_user": FORGED, "params": {"bridge_user": FORGED}}
    _neoffice_stamp_bridge_user("neoffice-ventes", "frappe_document_discount", args)
    assert args["bridge_user"] == "Administrator"
    assert args["params"]["bridge_user"] == "Administrator"


@pytest.mark.parametrize("raw", ["administrator", "ADMINISTRATOR", "Administrator2", "Guest", "root", "admin"])
def test_only_the_administrator_itself_is_a_person_without_an_at(session, monkeypatch, raw):
    session(raw)
    monkeypatch.setattr(handlers, "_kanban_task_user", lambda: "")
    args = {"bridge_user": FORGED, "params": {}}
    _neoffice_stamp_bridge_user("neoffice-ventes", "frappe_document_discount", args)
    assert args["bridge_user"] == ""


def test_a_model_cannot_pass_itself_off_as_the_administrator(session, monkeypatch):
    session("")
    monkeypatch.setattr(handlers, "_kanban_task_user", lambda: "")
    args = {"bridge_user": "Administrator", "params": {"bridge_user": "Administrator"}}
    _neoffice_stamp_bridge_user("neoffice-ventes", "frappe_document_discount", args)
    assert args["bridge_user"] == "" and args["params"]["bridge_user"] == ""


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


# //// Neoffice — the person of a worker's task is after the LAST `:user:` of its chat id (22.10): a
# //// conversation thread's chat id puts the client's thread id before the person, and a thread named
# //// « user » made the first cut name `user:<person>`.
@pytest.mark.parametrize("chat_id", [
    f"webhook:nora_chat:user:{PERSON}",
    f"webhook:nora_chat:thread:nora-0929bee8086342b8:user:{PERSON}",
    f"webhook:nora_chat:thread:user:user:{PERSON}",
])
def test_the_task_user_is_the_person_after_the_last_user_marker(tmp_path, monkeypatch, chat_id):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, user_id TEXT)")
    conn.execute("INSERT INTO kanban_notify_subs VALUES ('t_1', 'webhook', ?, 'webhook:nora_chat')", (chat_id,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(handlers, "_TASK_USER_CACHE", {})
    monkeypatch.setattr(handlers, "_TASK_THREAD_CACHE", {})
    assert handlers._kanban_task_user() == PERSON


# //// Neoffice — the conversation thread of the turn travels with the person (24.10): a person with NeoDoc and
# //// NeoGrille open has two threads, and the instance took the LATEST one for an editor's tool, so a NeoDoc turn
# //// read the workbook of a message sent from NeoGrille meanwhile.
THREAD_A = "nora-a45214e20dbc4dcb"
THREAD_B = "nora-aa6720e902e6468e"


@pytest.fixture
def chat_session(monkeypatch):
    """Bind the answering session's user AND chat id the way the gateway does, then unbind them."""
    from gateway import session_context

    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    session_context.reset_session_vars()

    def bind(user_id, chat_id):
        return session_context.set_session_vars(platform="webhook", user_id=user_id, chat_id=chat_id)

    yield bind
    session_context.reset_session_vars()


def test_the_turn_s_thread_is_written_beside_the_person(chat_session):
    chat_session(PERSON, f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}")
    args = {"bridge_conversation": THREAD_B, "params": {"bridge_conversation": THREAD_B}}
    _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_observe", args)
    assert args["bridge_conversation"] == THREAD_A
    assert args["params"]["bridge_conversation"] == THREAD_A
    assert args["bridge_user"] == PERSON


@pytest.mark.parametrize("chat_id", [
    f"webhook:nora_chat:user:{PERSON}",  # a per-person chat id names no thread
    "webhook:v2:bm9yYS0xMjM6dGhyZWFkOng",  # nor a per-delivery one
    "whatsapp:41790000000",
    "webhook:whatsapp_inbox:thread:chat:phone:41790000000",  # a WhatsApp chat's thread is the hub's marker
    f"webhook:nora_chat:thread:a b:user:{PERSON}",  # not an id
    f"webhook:nora_chat:thread::user:{PERSON}",
])
def test_a_chat_id_without_a_thread_names_none_and_the_model_s_value_does_not_survive(chat_session, chat_id):
    chat_session(PERSON, chat_id)
    args = {"bridge_conversation": THREAD_B, "params": {"bridge_conversation": THREAD_B}}
    _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_execute", args)
    assert args["bridge_conversation"] == ""
    assert args["params"]["bridge_conversation"] == ""


def test_two_turns_at_once_each_name_their_own_thread(monkeypatch):
    """The 24.10 case: the session lives in a ContextVar, so two turns answered at the same time never hand each other
    their thread (an os.environ mirror would)."""
    import contextvars

    from gateway import session_context

    def call(chat_id):
        session_context.set_session_vars(platform="webhook", user_id=PERSON, chat_id=chat_id)
        args = {}
        _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_observe", args)
        return args["bridge_conversation"]

    first = contextvars.copy_context().run(call, f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}")
    second = contextvars.copy_context().run(call, f"webhook:nora_chat:thread:{THREAD_B}:user:{PERSON}")
    assert (first, second) == (THREAD_A, THREAD_B)


def test_a_pole_worker_names_its_task_s_thread(tmp_path, monkeypatch, chat_session):
    import sqlite3

    chat_session("", "")
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, user_id TEXT)")
    conn.execute(
        "INSERT INTO kanban_notify_subs VALUES ('t_2', 'webhook', ?, 'webhook:nora_chat')",
        (f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}",),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_2")
    monkeypatch.setattr(handlers, "_TASK_USER_CACHE", {})
    monkeypatch.setattr(handlers, "_TASK_THREAD_CACHE", {})
    args = {"params": {"bridge_conversation": THREAD_B}}
    _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_observe", args)
    assert args["bridge_user"] == PERSON
    assert args["bridge_conversation"] == THREAD_A
    assert args["params"]["bridge_conversation"] == THREAD_A


def test_a_thread_resolution_failure_fails_closed(chat_session, monkeypatch):
    chat_session(PERSON, "")

    def boom():
        raise OSError("kanban.db locked")

    monkeypatch.setattr(handlers, "_kanban_task_thread", boom)
    args = {"bridge_conversation": THREAD_B}
    _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_observe", args)
    assert args["bridge_conversation"] == ""
    assert args["bridge_user"] == PERSON


def test_other_servers_get_no_thread(chat_session):
    chat_session(PERSON, f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}")
    args = {"bridge_conversation": "whatever"}
    _neoffice_stamp_bridge_user("github", "create_issue", args)
    assert args == {"bridge_conversation": "whatever"}


@pytest.mark.parametrize("server", ["neoffice-wiki", "neoffice-whatsapp", "neoffice-nora"])
def test_a_server_that_takes_no_thread_gets_none_and_loses_the_model_s(chat_session, server):
    """A FastMCP server rejects an argument its function does not declare (the wiki failed every call when
    `bridge_user` came): it gets no thread, and loses any the model wrote."""
    chat_session(PERSON, f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}")
    args = {"query": "TVA", "bridge_conversation": THREAD_B, "params": {"bridge_conversation": THREAD_B}}
    _neoffice_stamp_bridge_user(server, "wiki_search", args)
    assert "bridge_conversation" not in args and "bridge_conversation" not in args["params"]
    assert args["bridge_user"] == PERSON


@pytest.mark.parametrize("server", ["neoffice-frappe", "neoffice-tasks", "neoffice-read", "neoffice-support", "neoffice-ventes"])
def test_every_server_generated_from_the_instance_s_tools_gets_the_thread(chat_session, server):
    """neoffice-frappe (the orchestrator's) holds the document tools too: a server left out would fall back to the
    person's latest conversation, the bug itself."""
    chat_session(PERSON, f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}")
    args = {"bridge_conversation": THREAD_B}
    _neoffice_stamp_bridge_user(server, "nora_doc_observe", args)
    assert args["bridge_conversation"] == THREAD_A


def test_a_task_whose_notify_sub_comes_later_finds_its_person_and_thread(tmp_path, monkeypatch, chat_session):
    """A miss is not kept: the sub written after the worker's first call is read at the next one."""
    import sqlite3

    chat_session("", "")
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, user_id TEXT)")
    conn.commit()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_3")
    monkeypatch.setattr(handlers, "_TASK_USER_CACHE", {})
    monkeypatch.setattr(handlers, "_TASK_THREAD_CACHE", {})
    first = {}
    _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_observe", first)
    assert (first["bridge_user"], first["bridge_conversation"]) == ("", "")
    conn.execute(
        "INSERT INTO kanban_notify_subs VALUES ('t_3', 'webhook', ?, 'webhook:nora_chat')",
        (f"webhook:nora_chat:thread:{THREAD_A}:user:{PERSON}",),
    )
    conn.commit()
    conn.close()
    later = {}
    _neoffice_stamp_bridge_user("neoffice-tasks", "nora_doc_observe", later)
    assert (later["bridge_user"], later["bridge_conversation"]) == (PERSON, THREAD_A)
