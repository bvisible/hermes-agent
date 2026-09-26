# //// Neoffice — added file (no upstream equivalent): a systemd scope probe that only
# //// timed out is retried, and its verdict is kept seconds, not a minute.
"""The restart-safe scope probe under a slow user manager.

On a host under memory pressure the user systemd manager is paged out: its first D-Bus
call took 2.98 s, the next ones 0.02 s. With one 3 s probe cached for 60 s, that single
slow call refused every Kanban spawn of the next minute.
"""

import subprocess

import pytest

import tools.process_registry as pr


@pytest.fixture
def probe(monkeypatch):
    """Drive the probe: a list of outcomes (a return code, or "timeout"), and a clock."""
    monkeypatch.setattr(pr, "_IS_LINUX", True)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_PROBED_AT", 0.0)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    clock = [1000.0]
    monkeypatch.setattr("tools.process_registry.time.monotonic", lambda: clock[0])
    outcomes, bounds = [], []

    def fake_run(argv, **kwargs):
        bounds.append(kwargs.get("timeout"))
        outcome = outcomes.pop(0)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        return subprocess.CompletedProcess(args=argv, returncode=outcome)

    monkeypatch.setattr("subprocess.run", fake_run)
    return outcomes, bounds, clock


def test_a_slow_first_answer_is_retried_with_a_longer_bound(probe):
    outcomes, bounds, _clock = probe
    outcomes.extend(["timeout", 0])
    assert pr._systemd_run_user_scope_available() is True
    assert bounds == [3.0, 10.0]


def test_a_probe_that_only_timed_out_is_asked_again_seconds_later(probe):
    outcomes, bounds, clock = probe
    outcomes.extend(["timeout", "timeout"])
    assert pr._systemd_run_user_scope_available() is False
    clock[0] += 6  # well under the 60 s a real refusal is kept
    outcomes.append(0)
    assert pr._systemd_run_user_scope_available() is True
    assert len(bounds) == 3


def test_a_real_refusal_is_still_kept_for_the_full_ttl(probe):
    outcomes, bounds, clock = probe
    outcomes.append(1)  # systemd-run answered, and said no
    assert pr._systemd_run_user_scope_available() is False
    clock[0] += 6
    assert pr._systemd_run_user_scope_available() is False
    assert bounds == [3.0], "a refusal must not be probed again before the TTL"
