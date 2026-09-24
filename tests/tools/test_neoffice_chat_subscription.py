# //// Neoffice — added file (no upstream equivalent): a chat session that creates a task is
# //// subscribed notify-only, so its person is never woken into a second, message-eating turn.
"""kanban_create from a desk/WhatsApp chat subscribes notify-only; other platforms keep wake."""
import pytest


@pytest.mark.parametrize("platform, expected", [
    ("webhook", "notify"),
    ("telegram", "notify+wake"),
    ("slack", "notify+wake"),
])
def test_the_creators_subscription_mode(monkeypatch, platform, expected):
    from gateway import session_context
    from tools import kanban_tools

    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": "chat-1",
           "HERMES_SESSION_PROFILE": "default"}
    monkeypatch.setattr(session_context, "get_session_env", lambda name, default="": env.get(name, default))
    target = kanban_tools._resolve_notify_target()
    assert target["delivery_mode"] == expected
