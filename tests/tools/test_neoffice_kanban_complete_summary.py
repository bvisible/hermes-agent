# //// Neoffice — added file (no upstream equivalent): the answer of a worker goes in `summary`.
"""Since the inference engine moved to vLLM 0.31 (30.10), the workers closed a card with `result`, the
legacy one-line log field, instead of `summary`: 100% `summary` until 30.10, 77% on 31.10, 5% for the
accounting imputation tasks on 02.11 (maintenance#2164). The person got one line without the justification
the task asked for. The previous Hermes commit behaved the same, so it was the model side, not the code.

`kanban_complete` now requires `summary` and does not offer `result` to the model. The handler still takes a
`result` from the CLI (`--result`), which is not a model.
"""
import json

import pytest

from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA


def test_summary_is_required_and_result_is_not_offered_to_the_model():
    parameters = KANBAN_COMPLETE_SCHEMA["parameters"]
    assert parameters["required"] == ["summary"]
    assert "summary" in parameters["properties"]
    assert "result" not in parameters["properties"]


def test_the_description_does_not_send_the_model_to_result():
    description = KANBAN_COMPLETE_SCHEMA["description"]
    assert "``result``" not in description
    assert "``summary`` is required" in description


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def test_the_handler_still_takes_a_result_from_the_cli(worker_env):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_complete({"result": "done from the command line"}))
    assert out["ok"] is True
    assert out["task_id"] == worker_env
