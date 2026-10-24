# //// Neoffice — added file (no upstream equivalent): a desk conversation through the durable queue, end to end.
"""Two messages of one desk conversation, from the signed POST to the reply on the desk callback.

Since the durable queue (v0.22.3) a webhook message is admitted, executed by the session authority (the real
agent, here against a loopback model) and answered through ``WebhookAdapter.send``. Our desk route
(``deliver: nora``, ``session_key: user``) gives every message of a conversation the same chat id, which
upstream's per-delivery design does not expect. This test runs the whole real stack, the same way upstream's
test_webhook_authority.py does, and checks what the person would see:

- both answers reach the desk callback, under the conversation id the desk polls, with the route's token;
- the second turn's model request carries the first exchange (one session per conversation thread);
- the thread's session is still open after both turns (upstream ends a webhook's session after each);
- the callback's token is never written into the admission queue.

Only the provider (a loopback chat-completions peer) and the desk (a loopback HTTP server) are synthetic.
"""

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys

THREAD = "nora-0a1b2c3d4e5f6071"
PERSON = "person@example.com"


def test_a_desk_conversation_keeps_its_session_and_reaches_the_desk(tmp_path):
    result = subprocess.run([sys.executable, __file__],
        env=dict(os.environ, HERMES_HOME=str(tmp_path)), capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'desk-conversation.json').read_text())
    assert report['deliveries'] == [
        {'conversation_id': THREAD, 'token': 'desk-token', 'user': PERSON, 'text': 'LOCAL_ACK_MESSAGING_WARM'},
        {'conversation_id': THREAD, 'token': 'desk-token', 'user': PERSON, 'text': 'LOCAL_ACK_MESSAGING_WARM'},
    ]
    assert report['second_request_saw_first'] is True
    assert report['one_session'] is True and report['session_open'] is True
    assert report['token_in_queue'] is False


async def probe(peer, desk):
    import aiohttp
    from gateway import nora_chat_router
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    from tests.waiting import async_wait_until

    # The pre-router is NORA's own product logic (classifier, kanban): DIRECT here, the agent answers.
    nora_chat_router.route_chat_message = lambda **kwargs: {
        "routed": False, "category": "DIRECT", "ack": None, "task_id": None, "agent_hint": None}
    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='desk-conversation')
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        'host': '127.0.0.1', 'port': 0, 'secret': 'owned-hook-secret',
        'routes': {'nora_chat': {'prompt': '{message}', 'deliver': 'nora', 'session_key': 'user',
                                 'deliver_extra': {'callback_url': f'http://127.0.0.1:{desk.server_port}/deliver',
                                                   'callback_token': 'desk-token',
                                                   'conversation_id': '{conversation_id}'}}}}))
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter
    runner._wire_adapter_handlers(adapter)
    assert await adapter.connect()
    port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
    url = f'http://127.0.0.1:{port}/webhooks/nora_chat'

    def rows():
        return [row for sid in list(authority.sessions) for row in
                list_session_admissions(authority.db, session_id=sid, pending_only=False)]

    try:
        async with aiohttp.ClientSession() as client:
            for index, message in enumerate(('Première question de la conversation ?', 'Et la suivante ?')):
                body = json.dumps({'event_type': 'chat', 'user': PERSON, 'message': message,
                                   'conversation_id': THREAD}).encode()
                headers = {'X-Request-ID': f'desk-{index}', 'X-Hub-Signature-256': 'sha256=' + hmac.new(
                    b'owned-hook-secret', body, hashlib.sha256).hexdigest()}
                accepted = await client.post(url, data=body, headers=headers)
                assert accepted.status == 202, (accepted.status, await accepted.text())
                await async_wait_until(lambda: len(desk.deliveries) == index + 1, timeout=40,
                                       what=f'the answer to message {index + 1} on the desk')
        await async_wait_until(lambda: all(row['status'] == 'terminal' for row in rows()), timeout=10,
                               what='both admissions to settle')
        await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what='the finalizers')
        sessions = {row['target_session_id'] for row in rows()}
        second = json.dumps(peer.requests[-1].get('messages', []), ensure_ascii=False)
        Path(os.environ['HERMES_HOME'], 'desk-conversation.json').write_text(json.dumps({
            'deliveries': desk.deliveries,
            'second_request_saw_first': 'Première question de la conversation ?' in second,
            'one_session': len(sessions) == 1,
            'session_open': all(authority.db.get_session(sid)['ended_at'] is None for sid in sessions),
            'token_in_queue': any('desk-token' in json.dumps(row['payload']) for row in rows()),
        }))
    finally:
        await adapter.disconnect()


def main():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(1, str(Path(__file__).parent / 'fixtures'))
    from shared_authority_peer import ModelPeer

    class Desk(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.server.deliveries.append({'conversation_id': body.get('conversation_id'),
                                           'token': self.headers.get('X-Hermes-Token'),
                                           'user': body.get('user'), 'text': body.get('text')})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"message": "ok"}')

    peer = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
    peer.requests, peer.metadata_requests = [], []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    desk = ThreadingHTTPServer(('127.0.0.1', 0), Desk)
    desk.deliveries = []
    for server in (peer, desk):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'model:\n  default: local-wire-stub\n  provider: custom\n  base_url: {url}\n'
        'streaming:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n')
    try:
        asyncio.run(probe(peer, desk))
    finally:
        for server in (peer, desk):
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    import traceback
    status = 0
    try:
        main()
    except BaseException:
        traceback.print_exc()
        status = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)
