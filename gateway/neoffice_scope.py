# //// Neoffice — added file (no upstream equivalent): our code that runs in the gateway
# //// OUTSIDE an agent turn reads its secrets in the launch profile's scope.
"""The launch profile's secret scope, for the gateway code of ours that no turn wraps.

v2026.9.24 made ``get_secret`` fail closed when multiplexing is on and no profile scope is
installed. Agent turns, the dispatcher tick (``_default_profile_secret_scope``) and the
notifier install one. Our chat pre-router (its classifier is an LLM call made from the
webhook handler) and our memory webhooks (mem0 reads MEM0_MODE and the engine's key in an
executor thread) did not: on the dev instance, the evening of the rebase, the classifier
failed 7 times in 40 minutes — every message fell back to the orchestrator agent, whose
notify+wake subscription then woke a second turn that swallowed the user's next message —
and every memory webhook answered 502, the nightly consolidation included.

Same construction as upstream's dispatcher helper: the launch home's ``.env``, installed
only while multiplexing is on (a single-profile gateway reads ``os.environ`` as before).
"""

from __future__ import annotations

import contextlib
from functools import wraps
from pathlib import Path


@contextlib.contextmanager
def launch_profile_secrets():
    """Install the gateway launch profile's secret scope for the duration of the block."""
    from agent.secret_scope import (
        build_profile_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope)
    from hermes_constants import get_hermes_home

    if not is_multiplex_active():
        yield
        return
    home = get_hermes_home()
    token = set_secret_scope(build_profile_secret_scope(Path(home)), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)


def with_launch_profile_secrets(fn):
    """Decorator form of :func:`launch_profile_secrets` (the scope is a contextvar: it does not
    reach an executor thread, so wrap the function that RUNS in the thread)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        with launch_profile_secrets():
            return fn(*args, **kwargs)

    return wrapper
