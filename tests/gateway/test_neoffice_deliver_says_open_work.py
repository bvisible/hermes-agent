# //// Neoffice — added file (no upstream equivalent): a reply delivered to Nora's chat says whether work still goes on
# //// for its conversation (24.10).
"""The agent's answer may only hand over to a pole: the pole's card and its result come minutes later, and an editor's
pane that stopped reading 1.5 s after the answer never showed them. Each delivery now carries `open_tasks`, the kanban
tasks still working for the conversation's chat (todo, ready, running), and `open_since`, when the newest was created;
the instance keeps the pane reading while the turn under way has work open. A task blocked on a question, in review or scheduled holds no pane open, and a board that cannot be read
sends nothing (the instance then reads the delivery as the end, as before)."""

import asyncio
import sqlite3

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter

CHAT = "webhook:nora_chat:thread:nora-a45214e20dbc4dcb:user:person@example.test"
OTHER = "webhook:nora_chat:thread:nora-aa6720e902e6468e:user:person@example.test"


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT DEFAULT '')")
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))

    def task(task_id, status, chat_id=CHAT, created_at=1_000):
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (task_id, status, created_at))
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id) VALUES (?, 'webhook', ?)", (task_id, chat_id))
        conn.commit()

    yield task
    conn.close()


def test_a_pole_at_work_keeps_the_conversation_open(board):
    board("t_1", "running", created_at=1_000)
    board("t_2", "todo", created_at=1_050)
    board("t_3", "running", chat_id=OTHER, created_at=2_000)  # another pane's
    assert WebhookAdapter._neoffice_open_tasks(CHAT) == (2, 1_050), "how many, and when the newest was created"


@pytest.mark.parametrize("status", ["done", "archived", "blocked", "review", "scheduled", "triage"])
def test_a_task_that_waits_on_someone_else_holds_nothing_open(board, status):
    board("t_1", status)
    assert WebhookAdapter._neoffice_open_tasks(CHAT) == (0, None)


def test_no_board_is_no_work(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing" / "kanban.db"))
    assert WebhookAdapter._neoffice_open_tasks(CHAT) == (0, None)


def test_a_board_that_cannot_be_read_says_nothing(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    path.write_bytes(b"not a database")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    assert WebhookAdapter._neoffice_open_tasks(CHAT) is None


def _deliver(monkeypatch, open_tasks):
    import aiohttp

    sent = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def text(self):
            return "ok"

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def post(self, _url, json=None, **_kw):
            sent.append(json)
            return _Resp()

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: _Session())
    monkeypatch.setattr(WebhookAdapter, "_neoffice_open_tasks", staticmethod(lambda chat_id: open_tasks))
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    delivery = {"deliver_extra": {"callback_url": "https://erp.example.test/api/method/x",
                                  "callback_token": "tok", "conversation_id": "nora-a45214e20dbc4dcb"}}
    assert asyncio.run(adapter._deliver_nora("Je transmets au pôle.", CHAT, delivery)).success
    return sent[-1]


def test_the_delivery_carries_the_open_work(monkeypatch):
    sent = _deliver(monkeypatch, (1, 1_050))
    assert (sent["open_tasks"], sent["open_since"]) == (1, 1_050)
    sent = _deliver(monkeypatch, (0, None))
    assert sent["open_tasks"] == 0 and "open_since" not in sent


def test_a_delivery_whose_board_cannot_be_read_carries_no_count(monkeypatch):
    assert "open_tasks" not in _deliver(monkeypatch, None)
