"""//// Neoffice — added file (no upstream equivalent).

A kanban worker (dispatcher-spawned `chat -q`, HERMES_KANBAN_TASK set) skips the start-up work
meant for a person at a terminal: the pending fleet-restart hint and the banner prefetch.
"""
import sys
import types

from hermes_cli import main as main_mod


def _stub_banner(monkeypatch, calls):
    monkeypatch.setitem(sys.modules, "hermes_cli.banner", types.SimpleNamespace(
        prefetch_update_check=lambda: calls.append("update"),
        prefetch_banner_data=lambda: calls.append("banner"),
        _available_skills_cache=None))
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: calls.append("skills"))


def test_a_worker_prefetches_no_banner_but_still_syncs_skills(monkeypatch, tmp_path):
    calls = []
    _stub_banner(monkeypatch, calls)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: True)
    main_mod._start_chat_background_prefetch()
    import time
    deadline = time.monotonic() + 3
    while "skills" not in calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "update" not in calls and "banner" not in calls
    assert "skills" in calls


def test_a_person_still_gets_the_banner_prefetch(monkeypatch):
    calls = []
    _stub_banner(monkeypatch, calls)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: True)
    main_mod._start_chat_background_prefetch()
    assert "update" in calls and "banner" in calls


def test_a_worker_is_not_shown_the_fleet_restart_hint(monkeypatch):
    # v0.21.6 moved the hint into main_install_repair._recover_update_debts_on_startup, and the
    # guard moved with it.
    import hermes_cli.update_cmd_fleet as fleet
    import hermes_cli.update_pause_record as pause_record
    from hermes_cli import main_install_repair

    calls = []
    monkeypatch.setattr(fleet, "_warn_pending_fleet_restart_on_startup", lambda: calls.append("hint"))
    monkeypatch.setattr(pause_record, "recover", lambda: None)
    monkeypatch.setattr(sys, "argv", ["hermes", "chat", "-q", "x"])
    for task, expected in (("t_test", []), (None, ["hint"])):
        calls.clear()
        if task:
            monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        else:
            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        main_install_repair._recover_update_debts_on_startup()
        assert calls == expected
