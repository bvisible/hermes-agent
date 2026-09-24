# //// Neoffice — added file (no upstream equivalent): our gateway code that runs outside a
# //// turn reads secrets in the launch profile's scope (v2026.9.24 fails closed otherwise).
"""The pre-router's classifier and the memory webhooks can read their secrets again."""
import inspect

import pytest

from agent import secret_scope


@pytest.fixture
def multiplexed(monkeypatch, tmp_path):
    """A multiplexed gateway whose launch profile holds the engine key in its .env."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("OLARES_API_KEY=launch-key\nMEM0_MODE=oss\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("OLARES_API_KEY", raising=False)
    secret_scope.set_multiplex_active(True)
    yield home
    secret_scope.set_multiplex_active(False)


def test_outside_any_scope_a_secret_read_fails_closed(multiplexed):
    with pytest.raises(secret_scope.UnscopedSecretError):
        secret_scope.get_secret("OLARES_API_KEY")


def test_the_launch_scope_reads_the_launch_profile(multiplexed):
    from gateway.neoffice_scope import launch_profile_secrets

    with launch_profile_secrets():
        assert secret_scope.get_secret("OLARES_API_KEY") == "launch-key"
    with pytest.raises(secret_scope.UnscopedSecretError):
        secret_scope.get_secret("OLARES_API_KEY")  # and only for the block


def test_the_router_classifier_reads_the_engine_key(multiplexed):
    from gateway import nora_chat_router

    seen = {}

    def classifier(**_kw):  # as agent.auxiliary_client.call_llm does, it reads the key
        seen["key"] = secret_scope.get_secret("OLARES_API_KEY")
        raise RuntimeError("stop after the read: the router falls back, the test only needs the read")

    nora_chat_router.route_chat_message(
        message="Quelle stratégie adopter pour organiser notre équipe cette année ?",
        session_chat_id="webhook:nora_chat:test", conversation_id=None, thread_id=None, user_id="u",
        notifier_profile="default", idempotency_key="k", call_llm_fn=classifier, main_runtime=None)
    assert seen.get("key") == "launch-key", "the classifier ran without the profile's secrets"


def test_the_memory_webhooks_run_mem0_inside_the_scope():
    from gateway.platforms import webhook

    source = inspect.getsource(webhook)
    for fn in ("_store", "_read", "_forget"):
        assert (f"@with_launch_profile_secrets  # //// Neoffice — see gateway/neoffice_scope.py\n"
                f"        def {fn}()") in source, fn
