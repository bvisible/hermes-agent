"""Validated webhook destinations live in the owner's admitted input, not a TTL cache."""
from copy import deepcopy
import hashlib
import json

from hermes_state_runtime import RuntimeStoreError

# //// Neoffice — the two customer-chat destinations of NORA: the desk callback (``nora``) and the central
# //// WhatsApp router (``whatsapp_router``). Upstream persists only a platform's chat id or a GitHub PR with
# //// each admission, and refuses any other ``deliver`` as invalid_params, so an admission on our routes was
# //// refused outright. What is persisted here is only what differs per MESSAGE (the desk conversation, the
# //// number, whether the answer is voice); the callback URL and token, the router URL and key are the
# //// ROUTE's, read at delivery time from the route config, never copied into the queue (the same reason
# //// upstream drops extras: they can hold credentials). ``language`` rides with them for the failure
# //// sentence. See WebhookAdapter._neoffice_complete_destination.
_NEOFFICE_CHAT_FIELDS = {'nora': ('conversation_id',), 'whatsapp_router': ('phone', 'is_audio')}
# //// END Neoffice ////


def route_digest(adapter, chat_id):
    from gateway.platforms.webhook import _WebhookDeliveryIdentity
    adapter._reload_dynamic_routes()
    try:
        route_name = _WebhookDeliveryIdentity.route_from_chat_id(chat_id)
    except (ValueError, TypeError, IndexError):
        raise RuntimeStoreError('not_found') from None
    route = adapter._routes.get(route_name)
    if route is None:
        raise RuntimeStoreError('not_found')
    return hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_destination(delivery):
    from gateway.platforms.webhook import _is_known_platform, _REPO_RE
    if (not isinstance(delivery, dict)
            or not {'deliver', 'deliver_extra'} <= set(delivery)
            or set(delivery) - {'deliver', 'deliver_extra', 'profile', 'mirror', 'route', 'language'}  # //// Neoffice — language
            or (delivery.get('profile') is not None and not isinstance(delivery['profile'], str))
            or not isinstance(delivery.get('mirror', False), bool)
            or (delivery.get('route') is not None and not isinstance(delivery['route'], str))
            or (delivery.get('language') is not None and not isinstance(delivery['language'], str))):  # //// Neoffice
        raise RuntimeStoreError('invalid_params')
    target, extra = delivery['deliver'], delivery['deliver_extra']
    if not isinstance(target, str) or not isinstance(extra, dict):
        raise RuntimeStoreError('invalid_params')
    if target == 'github_comment':
        repo, number = extra.get('repo'), extra.get('pr_number')
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo) or not str(number).isdigit() or int(number) <= 0:
            raise RuntimeStoreError('invalid_params')
    elif target != 'log' and target not in _NEOFFICE_CHAT_FIELDS and not _is_known_platform(target):  # //// Neoffice
        raise RuntimeStoreError('invalid_params')
    # Configuration extras may contain credentials or whole rendered payloads.
    # Persist only fields the selected sender actually consumes.
    fields = _NEOFFICE_CHAT_FIELDS.get(target) or {'log': (), 'github_comment': ('repo', 'pr_number')}.get(  # //// Neoffice
        target, ('chat_id', 'message_thread_id', 'thread_id'))
    result = {'deliver': target, 'deliver_extra': deepcopy({key: extra[key] for key in fields if key in extra})}
    # Omit the unbound defaults to preserve pre-profile receipt fingerprints.
    if delivery.get('profile'):
        result['profile'] = delivery['profile']
    # //// Neoffice — the customer's language, read back by send() for the failure sentence.
    if target in _NEOFFICE_CHAT_FIELDS and delivery.get('language'):
        result['language'] = delivery['language']
    # //// END Neoffice ////
    # Route opt-in (``mirror_to_session: true``): the delivered text is mirrored into the target chat's
    # session; the destination row carries it so a replayed delivery mirrors exactly like the first.
    if delivery.get('mirror') is True:
        result['mirror'] = True
        if delivery.get('route'):
            result['route'] = delivery['route']  # the mirror's label only
    return result


def snapshot_destination(adapter, delivery):
    from gateway.config import Platform

    result = validate_destination(delivery)
    if (result['deliver'] not in {'log', 'github_comment'} and result['deliver'] not in _NEOFFICE_CHAT_FIELDS  # //// Neoffice
            and not result['deliver_extra'].get('chat_id')):
        profile = result.get('profile')
        with adapter._profile_scope(profile):
            home = adapter._delivery_config(profile).get_home_channel(Platform(result['deliver']))
        if home is None:
            raise RuntimeStoreError('not_found')
        result['deliver_extra']['chat_id'] = home.chat_id
    return result


# //// Neoffice — whether a chat id belongs to one of our conversational routes (desk, WhatsApp).
def neoffice_chat_route(adapter, chat_id):
    """True when *chat_id* is a conversation of a route that delivers to a customer chat. Such a chat id
    is the PERSON's (or the thread's), shared by every message of the conversation: it is not unique per
    delivery as upstream's ``webhook:v2:`` ids are."""
    from gateway.platforms.webhook import _WebhookDeliveryIdentity
    try:
        route = _WebhookDeliveryIdentity.route_from_chat_id(chat_id)
    except (ValueError, TypeError, IndexError):
        return False
    return (adapter._routes.get(route) or {}).get('deliver') in _NEOFFICE_CHAT_FIELDS
# //// END Neoffice ////


def retained_destination(adapter, chat_id):
    """Readback also checks today's connector and authorization; retained data is not a grant."""
    from gateway.session_envelope import restore_native, _validate_native
    runner = adapter.gateway_runner or getattr(adapter._message_handler, '__self__', None)
    from gateway.session_authorities import active_authority, all_authorities, serving_authorities
    if not all_authorities(runner):
        return validate_destination(adapter._delivery_info.get(chat_id))
    # The reply leg runs in the routed profile's scope; that ledger holds the destination. A
    # delivery id is unique per route, so an unscoped caller may search every served ledger (a
    # parked profile's retained store is not served: reading it could fail every other profile's).
    scoped = active_authority(runner)
    candidates = [scoped] if scoped is not None else serving_authorities(runner)
    # Only the signed ingress admission carries the destination; later automation rows
    # (process/watch completions) on the same chat share its source but never compete.
    query = """SELECT payload_json FROM session_admissions
        WHERE json_extract(payload_json, '$.native_text_v1.source.chat_id')=?
          AND json_extract(payload_json, '$.native_text_v1.source.platform')='webhook'
          AND json_extract(payload_json, '$.native_text_v1.automation') IS NULL
        ORDER BY seq"""  # //// Neoffice — oldest first, so the newest of a conversation is the last
    rows = [row for authority in candidates for row in authority.db._read_all(query, (chat_id,))]
    # //// Neoffice — a conversation's chat id is shared by all its admissions: the newest one carries the
    # //// destination of the message being answered (upstream's chat ids are one per delivery).
    if len(rows) > 1 and neoffice_chat_route(adapter, chat_id):
        rows = rows[-1:]
    # //// END Neoffice ////
    if len(rows) != 1:
        raise RuntimeStoreError('not_found')
    payload = json.loads(rows[0]['payload_json'])
    envelope = payload['native_text_v1']
    event = restore_native(payload, runner)
    _validate_native(runner, event, envelope.get('provenance'))
    if runner._adapter_for_source(event.source) is not adapter:
        raise RuntimeStoreError('permission_denied')
    if envelope.get('webhook_route') != route_digest(adapter, chat_id):
        raise RuntimeStoreError('admission_conflict')
    return validate_destination(envelope.get('webhook_delivery'))
