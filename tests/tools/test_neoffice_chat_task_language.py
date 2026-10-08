# //// Neoffice — added file (no upstream equivalent): a task the orchestrator creates from a person's
# //// chat tells the pole's worker which language to reply in (08.10, capability bench).
"""kanban_create from a desk/WhatsApp chat carries the reply directive NORA's pre-router puts on its tasks.

« How many open quotes does <a client> have? » was handed on to the orchestrator, which created the
task without the person's language: the ventes worker answered in its SOUL's French.
"""
import json

import pytest

CHAT = "webhook:nora_chat:user:someone@example.test"


@pytest.fixture
def router(monkeypatch, tmp_path):
    """An orchestrator in the gateway: isolated board, no kanban task of its own."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    from gateway import nora_chat_router
    monkeypatch.setattr(nora_chat_router, "_CHAT_LANGUAGE", {})
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", CHAT)
    return nora_chat_router


def _create(body):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    args = {"title": "Open quotes", "assignee": "ventes"}
    if body is not None:
        args["body"] = body
    out = json.loads(kt._handle_create(args))
    assert out["ok"] is True, out
    conn = kbc.connect()
    try:
        return kb.get_task(conn, out["task_id"]).body
    finally:
        conn.close()


@pytest.mark.parametrize("language, name", (("en", "English"), ("de", "German"), ("it", "Italian"),
                                            ("fr", "French")))
def test_the_task_tells_the_worker_the_persons_language(router, language, name):
    router.remember_chat_language(CHAT, language)
    body = _create("Count the open quotes of Martin SA and their total.")
    assert body == ("Count the open quotes of Martin SA and their total.\n\n"
                    + router.worker_reply_directive(language))
    assert f"Reply to the user in {name}." in body


def test_an_empty_body_gets_the_directive_alone(router):
    router.remember_chat_language(CHAT, "en")
    assert _create(None) == router.worker_reply_directive("en")


def test_a_body_that_already_carries_a_directive_is_left_alone(router):
    router.remember_chat_language(CHAT, "en")
    body = "Count the quotes.\n\n" + router.worker_reply_directive("de")
    assert _create(body) == body


def test_a_chat_the_router_never_saw_keeps_its_body(router):
    assert _create("Count the quotes.") == "Count the quotes."


def test_another_platform_keeps_its_body(router, monkeypatch):
    router.remember_chat_language(CHAT, "en")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    assert _create("Count the quotes.") == "Count the quotes."


def test_a_workers_own_child_task_keeps_its_body(router):
    from tools import kanban_tools as kt
    router.remember_chat_language(CHAT, "en")
    assert kt._neoffice_chat_task_body("Count the quotes.", "t_parent") == "Count the quotes."
