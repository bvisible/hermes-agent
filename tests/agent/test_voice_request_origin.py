"""Voice metadata survives the real SQLite notification path without promoting text.

Place this test under tests/agent in the candidate export and invoke the canonical
Hermes scripts/run_tests.sh runner. Only the two candidate source modules are
loaded by path; all DB helpers are real imports from the pinned Hermes source.
No source inspection, real credentials, model calls or business operations.
"""
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load_candidate(name, relative):
    path = Path(__file__).resolve().parents[2] / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


router = load_candidate("voice_origin_candidate_router", "gateway/nora_chat_router.py")
breaker = load_candidate("voice_origin_candidate_breaker", "agent/neoffice_kanban_breaker.py")


class VoiceRequestOrigin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-voice-origin-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = {"HERMES_HOME": str(self.root / "home"),
               "HERMES_KANBAN_DB": str(self.root / "kanban.db"),
               "HERMES_KANBAN_BOARD": "", "HERMES_KANBAN_TASK": ""}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        breaker._NEOFFICE_SUBSCRIBED.clear()
        breaker._NEOFFICE_TASK_PRIORITIES.clear()
        from hermes_cli import kanban_db, kanban_db_connect, kanban_db_notify
        self.db, self.connect, self.notify = kanban_db, kanban_db_connect, kanban_db_notify
        self.conn = self.connect.connect()
        self.addCleanup(self.conn.close)
        self.task = self.db.create_task(self.conn, title="Synthetic priority QA",
                                       body="No actions", workspace_kind="scratch")
        os.environ["HERMES_KANBAN_TASK"] = self.task
        self.agent = SimpleNamespace(api_mode="chat_completions", base_url="https://olares1.noraai.ch/v1")

    def register(self, page_context, **extra):
        router._add_notify_sub(self.notify, self.conn, task_id=self.task,
                               platform="webhook", chat_id="synthetic-chat",
                               conversation_id="synthetic-conversation",
                               page_context=page_context, **extra)
        # A subsequent task/worker reads persisted state, not this test's cache.
        breaker._NEOFFICE_SUBSCRIBED.clear()
        breaker._NEOFFICE_TASK_PRIORITIES.clear()
        return self.notify.list_notify_subs(self.conn, self.task)[0]

    def test_sqlite_voice_to_text_roundtrip_preserves_routing_and_rejects_arbitrary_priority(self):
        for source in ("nora-console-voice", "nora-quick-voice", "nora-live-widget"):
            with self.subTest(source=source):
                sub = self.register({"source": source, "priority": -999},
                                    delivery_metadata={"routing_anchor": "keep"})
                meta = sub["delivery_metadata"]
                self.assertEqual(meta["conversation_id"], "synthetic-conversation")
                self.assertEqual(meta["routing_anchor"], "keep")
                self.assertNotIn("priority", meta)
                kwargs = {"model": "nora", "messages": [{"role": "user", "content": "QA"}],
                          "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
                messages = kwargs["messages"]
                result = breaker.neoffice_request_priority(self.agent, kwargs)
                self.assertEqual(result["extra_body"]["priority"], -10)
                self.assertIs(result["messages"], messages)
                self.assertEqual(result["extra_body"]["chat_template_kwargs"], {"enable_thinking": False})
                # Re-subscribe to the SAME persisted row. Empty source must erase
                # the previous marker through the real upstream merge/encoder.
                self.register({"source": "nora-console"},
                              delivery_metadata={"neoffice_request_source": source})
                self.assertEqual(breaker.neoffice_request_priority(self.agent, {})["extra_body"]["priority"], 0)
        for page in (None, {}, "nora-quick-voice", {"source": "voice"},
                     {"source": "nora-quick-voice-evil"}, {"priority": -10}):
            with self.subTest(page=page):
                self.register(page)
                self.assertEqual(breaker.neoffice_request_priority(self.agent, {})["extra_body"]["priority"], 0)

    def test_only_subscribed_olares_worker_is_promoted_and_explicit_priority_wins(self):
        self.assertNotIn("extra_body", breaker.neoffice_request_priority(self.agent, {}))
        self.register({"source": "nora-quick-voice"})
        for priority in (-20, 0, 20):
            with self.subTest(explicit=priority):
                result = breaker.neoffice_request_priority(self.agent, {"extra_body": {"priority": priority}})
                self.assertEqual(result["extra_body"]["priority"], priority)
        agents = (SimpleNamespace(api_mode="chat_completions", base_url=self.agent.base_url,
                                  _turn_origin="background_review"),
                  SimpleNamespace(api_mode="anthropic_messages", base_url=self.agent.base_url),
                  SimpleNamespace(api_mode="chat_completions", base_url="https://api.openai.com/v1"))
        with patch.object(self.connect, "connect", side_effect=AssertionError("must not read DB")):
            for agent in agents:
                with self.subTest(agent=vars(agent)):
                    self.assertEqual(breaker.neoffice_request_priority(agent, {"model": "nora"}), {"model": "nora"})
            with patch.dict(os.environ, {"HERMES_KANBAN_TASK": ""}):
                self.assertEqual(breaker.neoffice_request_priority(self.agent, {}), {})
        # A voice-like metadata value on another platform is ordinary chat, not voice.
        other = self.db.create_task(self.conn, title="Synthetic other transport", workspace_kind="scratch")
        self.notify.add_notify_sub(self.conn, task_id=other, platform="telegram", chat_id="synthetic",
                                   delivery_metadata={"neoffice_request_source": "nora-quick-voice"})
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": other}):
            self.assertEqual(breaker.neoffice_request_priority(self.agent, {})["extra_body"]["priority"], 0)


if __name__ == "__main__":
    unittest.main()
