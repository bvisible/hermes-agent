# //// Neoffice — added file (no upstream equivalent): the person a kanban task acts for, read in one place.
"""The person a kanban task acts for, read from its notify subs.

A NORA pole serves every person of an instance. The gateway names the person behind a worker's calls as
``bridge_user`` (tools/mcp_tool_handlers.py ``_kanban_task_user``): the instance then reads and writes with that
person's rights. The worker's context keeps its recent work to that same person's tasks
(hermes_cli/kanban_db_context.py ``_ctx_role_history``). Both read a sub's person here, so that the rights a worker
acts with and the past work it is shown can never name two different people.

A sub names the LAST ``:user:`` of a ``webhook:nora_chat:`` chat id (a thread's chat id puts the client's thread id
before the person, and a thread named « user » made the first cut name ``user:<person>``), else its ``user_id`` when
it holds an « @ »; '' when it names nobody.

The gateway acts for the person of a task's LATEST webhook sub (``task_person``). Subs are added after a task ran,
though: a delegation that reuses an idempotency key returns the existing task and subscribes the new asker to it, and
a link copies the parent's subs. So a task is someone's past work only while every one of its webhook subs names that
one person (``sole_person``).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

_NORA_CHAT = "webhook:nora_chat:"
_MARKER = ":user:"


def latest_sub(conn: sqlite3.Connection, task_id: str) -> Optional[tuple[str, str]]:
    """``(chat_id, user_id)`` of the task's latest webhook notify sub, or None when it has none."""
    row = conn.execute(
        "SELECT chat_id, user_id FROM kanban_notify_subs "
        "WHERE task_id=? AND platform='webhook' ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return (row[0] or "", row[1] or "") if row else None


def person_of_sub(chat_id: Optional[str], user_id: Optional[str]) -> str:
    """The person a notify sub names, or ''."""
    chat_id, user_id = chat_id or "", user_id or ""
    if chat_id.startswith(_NORA_CHAT) and _MARKER in chat_id:
        return chat_id.rsplit(_MARKER, 1)[1].strip()
    return user_id.strip() if "@" in user_id else ""


def task_person(conn: sqlite3.Connection, task_id: str) -> str:
    """The person the gateway acts for on this task (its latest webhook sub), or '' when it names nobody."""
    sub = latest_sub(conn, task_id)
    return person_of_sub(*sub) if sub else ""


def sole_person(conn: sqlite3.Connection, task_id: str) -> str:
    """The one person every webhook sub of the task names; '' when it has no sub, when a sub names nobody, or when
    its subs name more than one person."""
    rows = conn.execute(
        "SELECT chat_id, user_id FROM kanban_notify_subs WHERE task_id=? AND platform='webhook'",
        (task_id,),
    ).fetchall()
    people = {person_of_sub(row[0], row[1]) for row in rows}
    return next(iter(people)) if len(people) == 1 else ""
