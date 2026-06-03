"""Deterministic NORA chat pre-router (gateway-side).

ROOT CAUSE THIS FIXES (proven from agent.log on 2026-06-03): the Qwen orchestrator
sometimes emits the routing acknowledgment as plain TEXT and then STOPS, WITHOUT
calling ``kanban_create`` — so no task is created, no worker spawns, no answer is
ever delivered, and the desk poll times out (the "empty promise"). Prompt-level
``TOOL_USE_ENFORCEMENT`` (already active for qwen models) is demonstrably
insufficient: it is guidance, not a hard guarantee.

This router takes the LLM out of the routing DECISION and out of the task-creation
ACTION. A one-shot classifier (the same ``call_llm`` helper ``generate_title`` uses)
picks a business pole; we then create the kanban task IN CODE — exactly the call the
``kanban_create`` tool makes, so the dispatcher spawns the specialist worker — and
deliver a fixed French acknowledgment. The model can no longer "forget" to create the
task, because creating it is no longer the model's job.

Anything the classifier marks ``DIRECT`` (greetings, small talk, meta questions, a
one-sentence answer Nora can give without business data) — and any classifier failure
— falls back to the normal agent dispatch, so the direct-answer path is unchanged
(zero regression). The router is dependency-injected (call_llm, model runtime, deliver
function are passed in) so it is unit-testable and the gateway hook stays a thin call.

User-facing strings are French (Swiss business audience); code/comments are English.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Business poles NORA routes to. Mirrors the orchestrator SOUL roster (the "Pôle"
# column) — keep in sync with configs/SOUL.md so deterministic routing matches what
# the orchestrator would have chosen on its good days.
POLES = ("compta", "ventes", "support", "rh", "analyse")

# Pole → user-facing French label (never leak the internal key or any stack term).
POLE_LABELS_FR = {
    "compta": "Comptabilité",
    "ventes": "Ventes",
    "support": "Support",
    "rh": "Ressources Humaines",
    "analyse": "Analyse",
}

# Per-conversation last route (in-memory, gateway-process-scoped). Lets the classifier
# resolve follow-ups IN CONTEXT: "Ceux de Daniel Moret" after "Combien de devis ouverts ?"
# (→ ventes) is the SAME ventes query filtered by a CLIENT — not an RH question about a
# person. Without this, each message is classified blind and a name-only follow-up gets
# mis-routed (verified 2026-06-03: "Ceux de Daniel Moret" → rh). Resets on gateway restart
# (the first follow-up after a restart degrades to context-free classify — acceptable).
_LAST_ROUTE: dict = {}
_LAST_ROUTE_MAX = 1000  # bound the dict; cleared wholesale when exceeded (cheap, rare)

# Classifier system prompt. Mirrors the SOUL roster domains + its two disambiguation
# rules (any chart/visual → analyse regardless of subject; a plain number in text →
# the owning pole). The model must answer with EXACTLY one lowercase token.
_CLASSIFIER_SYSTEM = (
    "Tu es le routeur de Neoffice. On te donne le message d'un utilisateur. "
    "Tu réponds par UN SEUL mot parmi : recurrent, compta, ventes, support, rh, analyse, direct. "
    "Aucune ponctuation, aucune explication, juste le mot.\n\n"
    "PRIORITÉ ABSOLUE — 'recurrent' : si la demande doit se RÉPÉTER dans le temps "
    "(« tous les matins / chaque jour / toutes les heures / chaque lundi / chaque semaine / "
    "régulièrement / automatiquement / planifie / programme une tâche / fais-le tous les… »), "
    "réponds 'recurrent' — PEU IMPORTE le sujet (même si ça parle d'emails, de factures ou de "
    "PDF). Une demande PONCTUELLE (une seule fois, maintenant) n'est PAS 'recurrent'.\n\n"
    "Sinon, choisis le pôle métier qui doit traiter la demande :\n"
    "- compta : factures, paiements, TVA, chiffre d'affaires, impayés, fournisseurs, "
    "commandes d'achat, rapports financiers (un chiffre demandé en TEXTE).\n"
    "- ventes : devis, commandes clients, factures de vente, articles, clients "
    "(création/recherche), prix.\n"
    "- support : emails (lecture/rédaction), pièces jointes & OCR, rappels, tickets, "
    "aide à l'utilisation.\n"
    "- rh : congés, paie, employés, contrats, absences.\n"
    "- analyse : graphiques, visuels, dataviz, cartes d'indicateurs, tableaux de bord "
    "sur mesure — QUEL QUE SOIT le sujet (« montre-moi … en graphique »). Une demande "
    "de visualisation va TOUJOURS à analyse, jamais à compta/ventes.\n\n"
    "Réponds 'direct' UNIQUEMENT si le message est une salutation, un remerciement, du "
    "bavardage, une question sur Nora elle-même, ou une demande à laquelle on répond en "
    "une phrase SANS consulter les données métier. En cas de doute entre un pôle et "
    "'direct' pour une vraie demande métier, choisis le pôle.\n\n"
    "SUITE DE CONVERSATION : si un contexte « message précédent → pôle X » t'est donné ET "
    "que le nouveau message est une PRÉCISION/SUITE (un nom seul, « ceux de … », « et pour … », "
    "un filtre, un pronom comme « les siens »), garde le MÊME pôle X. Un nom de personne dans un "
    "contexte ventes/devis ou compta/factures est un CLIENT, PAS un sujet RH (rh = congés, paie, "
    "employés internes uniquement)."
)

# A classifier reply token → canonical pole (or "DIRECT"). We accept the bare key.
_TOKEN_RE = re.compile(r"[a-zàâçéèêëîïôûùüÿñæœ]+", re.IGNORECASE)


def classify(
    message: str,
    *,
    call_llm_fn: Callable[..., Any],
    main_runtime: Optional[dict],
    prior: Optional[dict] = None,
    timeout: float = 8.0,
) -> str:
    """Return a pole in :data:`POLES`, or ``"DIRECT"``.

    ``prior`` (optional ``{"msg", "pole"}``) is the previous turn's user message and the
    pole it routed to; when present it is fed to the classifier so a follow-up/refinement
    ("Ceux de Daniel Moret") stays on the same pole instead of being classified blind
    (a client name in a devis context would otherwise look like an RH/person query).

    Defensive by construction: an empty message, an LLM error/timeout, or an
    unrecognized reply all resolve to ``"DIRECT"`` so the caller falls back to the
    normal agent path (never drops a message because routing was uncertain).
    """
    msg = (message or "").strip()
    if not msg:
        return "DIRECT"
    user_content = msg[:2000]
    if prior and prior.get("pole"):
        user_content = (
            f"[Contexte conversation — message précédent : « {(prior.get('msg') or '')[:200]} » "
            f"→ pôle « {prior['pole']} ». Si ce nouveau message est une suite/précision de ce "
            f"qui précède, garde le pôle « {prior['pole']} ».]\n{user_content}"
        )
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = call_llm_fn(
            task="nora_chat_routing",
            messages=messages,
            max_tokens=8,
            temperature=0.0,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        raw = (resp.choices[0].message.content or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 — any failure must degrade to DIRECT
        logger.warning("nora_chat_router: classifier failed, falling back to DIRECT: %s", exc)
        return "DIRECT"
    tokens = _TOKEN_RE.findall(raw)
    for tok in tokens:
        if tok == "recurrent":
            return "recurrent"
        if tok in POLES:
            return tok
        if tok in ("direct", "aucun", "none"):
            return "DIRECT"
    # Unrecognized output → safest is to let the agent handle it.
    logger.info("nora_chat_router: unrecognized classifier output %r → DIRECT", raw[:80])
    return "DIRECT"


def build_ack(pole: str) -> str:
    """Fixed French acknowledgment naming the business pole (SOUL style)."""
    label = POLE_LABELS_FR.get(pole, "équipe concernée")
    return f"Je transmets votre demande à votre pôle {label}, je reviens vers vous très vite."


def _add_notify_sub(kanban_db, conn, **kw) -> None:
    """Subscribe the notifier so the worker's terminal result is delivered back to
    this chat. ``conversation_id`` is added by the runtime cid patch; call defensively
    so the router also works on a pristine (un-patched) fork checkout."""
    try:
        kanban_db.add_notify_sub(conn, **kw)
    except TypeError:
        kw.pop("conversation_id", None)
        kanban_db.add_notify_sub(conn, **kw)


def _post_ack_to_callback(ack: str, deliver_extra: Optional[dict]) -> bool:
    """Deliver the immediate ack to the desk via the nora callback — the SAME POST the
    nora-deliver path uses ({conversation_id, text} + X-Hermes-Token). We POST it here
    rather than via the platform's ``_direct_deliver`` because that routes the custom
    "nora" deliver type to ``_deliver_cross_platform``, which doesn't know it, so the
    ack was silently dropped (success=False, no exception). Desk only — when there is no
    callback_url (e.g. WhatsApp) we skip; the worker's result still delivers via the
    notifier. Never raises: a failed ack must not break routing."""
    extra = deliver_extra or {}
    url = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    cid = (extra.get("conversation_id") or "").strip()
    if not (url and token and cid and ack):
        return False
    import json as _json
    import urllib.request

    body = _json.dumps({"conversation_id": cid, "text": ack}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= getattr(resp, "status", 0) < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: ack callback POST failed: %s", exc)
        return False


def _route_recurrent(
    message: str, chat_user: Optional[str], deliver_extra: Optional[dict]
) -> dict:
    """RECURRENT class: a recurring request ("relève mes mails tous les matins"). We create
    the scheduled task IN CODE via the nora ``task_router.route_recurrent`` endpoint — it
    extracts the schedule DETERMINISTICALLY (time/frequency/kind/mailbox), scopes it to the
    user, and returns a fixed ack we deliver to the desk. Like the kanban path, this takes
    the action out of the weak model's hands. Any failure (no user, endpoint down, declined)
    → routed=False so the caller falls back to the normal agent dispatch (zero regression).
    The Frappe URL + auth are derived from the SAME desk callback the ack POST uses."""
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    cid = (extra.get("conversation_id") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _ROUTE = "nora.api.v2.task_router.route_recurrent"
    if not (cb and token and user) or _DELIVER not in cb:
        return {"routed": False, "category": "recurrent", "ack": None, "task_id": None}
    import json as _json
    import urllib.request

    url = cb.replace(_DELIVER, _ROUTE)
    body = _json.dumps({"user": user, "message": message, "conversation_id": cid}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: recurrent POST failed → agent fallback: %s", exc)
        return {"routed": False, "category": "recurrent", "ack": None, "task_id": None}
    if not isinstance(data, dict) or not data.get("ok"):
        logger.info(
            "nora_chat_router: recurrent declined (%s) → agent fallback", (data or {}).get("error")
        )
        return {"routed": False, "category": "recurrent", "ack": None, "task_id": None}
    ack = data.get("ack") or "C'est noté, je programme ça."
    ack_delivered = _post_ack_to_callback(ack, deliver_extra)
    logger.info(
        "nora_chat_router: recurrent task=%s ack_delivered=%s", data.get("task_id"), ack_delivered
    )
    return {
        "routed": True, "category": "recurrent", "ack": ack,
        "task_id": data.get("task_id"), "ack_delivered": ack_delivered,
    }


def route_chat_message(
    *,
    message: str,
    session_chat_id: str,
    conversation_id: Optional[str],
    thread_id: Optional[str],
    user_id: Optional[str],
    notifier_profile: Optional[str],
    idempotency_key: Optional[str],
    call_llm_fn: Callable[..., Any],
    main_runtime: Optional[dict],
    deliver_extra: Optional[dict] = None,
    chat_user: Optional[str] = None,
    board: Optional[str] = None,
    classify_timeout: float = 8.0,
) -> dict:
    """Classify *message* and, when it is a business request, create the kanban task
    in code + subscribe the notifier. Returns a decision dict::

        {"routed": bool, "category": str, "ack": Optional[str], "task_id": Optional[str]}

    ``routed=False`` (category ``DIRECT`` or any creation failure) means the caller
    MUST proceed with the normal agent dispatch — the message is never dropped.
    """
    prior = _LAST_ROUTE.get(conversation_id) if conversation_id else None
    category = classify(
        message,
        call_llm_fn=call_llm_fn,
        main_runtime=main_runtime,
        prior=prior,
        timeout=classify_timeout,
    )
    # Remember this turn so the NEXT message resolves a follow-up in context. Track DIRECT
    # too (pole=None) so a follow-up to a greeting doesn't inherit a stale pole.
    if conversation_id:
        if len(_LAST_ROUTE) > _LAST_ROUTE_MAX:
            _LAST_ROUTE.clear()
        _LAST_ROUTE[conversation_id] = {
            "msg": (message or "")[:200],
            "pole": (category if category in POLES else None),
        }
    if category == "DIRECT":
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}

    # RECURRENT — a recurring "do this every X" request. Not a one-shot pole task: create
    # a scheduled task in code (deterministic, user-scoped) via the nora task_router and
    # deliver its ack. Falls back to the agent on any failure (zero regression).
    if category == "recurrent":
        return _route_recurrent(message, chat_user, deliver_extra)

    # Create the task exactly as the kanban_create tool does (assignee + running →
    # the dispatcher spawns the specialist worker). idempotency_key (the webhook
    # delivery id) makes a retried POST reuse the same task instead of double-routing.
    try:
        from hermes_cli import kanban_db

        conn = kanban_db.connect(board=board)
        try:
            # Give the WORKER the conversation context too, so a follow-up is executed
            # as a refinement of the prior turn — "Ceux de Daniel Moret" after "Combien de
            # devis ouverts ?" must mean "the OPEN DEVIS of client Daniel Moret", not "show
            # Daniel Moret's profile". The router already kept the right pole; this keeps the
            # right INTENT. Only when the previous turn routed to the same kind of request.
            _body = message
            if prior and prior.get("pole") and prior.get("msg"):
                _body = (
                    f"[Suite de conversation — l'utilisateur a d'abord demandé : "
                    f"« {prior['msg'][:300]} ». Le message ci-dessous précise/poursuit cette "
                    f"demande, à interpréter dans ce contexte (un nom = un client à filtrer).]"
                    f"\n\n{message}"
                )
            task_id = kanban_db.create_task(
                conn,
                title=(message or "").strip()[:200] or "Demande",
                body=_body,
                assignee=category,
                created_by="nora-chat-router",
                initial_status="running",
                idempotency_key=idempotency_key,
            )
            _add_notify_sub(
                kanban_db,
                conn,
                task_id=task_id,
                platform="webhook",
                chat_id=session_chat_id,
                thread_id=thread_id or None,
                user_id=user_id or None,
                notifier_profile=notifier_profile,
                conversation_id=conversation_id or None,
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        # If we cannot create/subscribe the task, DO NOT silently drop the message —
        # fall back to the agent path (which may still route it). Surface the failure.
        logger.exception("nora_chat_router: task creation failed (%s) → DIRECT fallback", exc)
        return {"routed": False, "category": category, "ack": None, "task_id": None}

    ack = build_ack(category)
    # Deliver the immediate ack NOW so the user sees "I'm handing this to <pole>" right
    # away — it also makes the ~10s worker wait feel responsive. Failure to deliver the
    # ack must NEVER fail the routing (the worker result still arrives via the notifier).
    ack_delivered = _post_ack_to_callback(ack, deliver_extra)
    logger.info(
        "nora_chat_router: routed deterministically chat=%s → %s task=%s ack_delivered=%s",
        session_chat_id, category, task_id, ack_delivered,
    )
    return {
        "routed": True,
        "category": category,
        "ack": ack,
        "task_id": task_id,
        "ack_delivered": ack_delivered,
    }
