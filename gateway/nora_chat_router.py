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

# Classifier system prompt. Mirrors the SOUL roster domains + its two disambiguation
# rules (any chart/visual → analyse regardless of subject; a plain number in text →
# the owning pole). The model must answer with EXACTLY one lowercase token.
_CLASSIFIER_SYSTEM = (
    "Tu es le routeur de Neoffice. On te donne le message d'un utilisateur. "
    "Tu réponds par UN SEUL mot parmi : compta, ventes, support, rh, analyse, direct. "
    "Aucune ponctuation, aucune explication, juste le mot.\n\n"
    "Choisis le pôle métier qui doit traiter la demande :\n"
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
    "'direct' pour une vraie demande métier, choisis le pôle."
)

# A classifier reply token → canonical pole (or "DIRECT"). We accept the bare key.
_TOKEN_RE = re.compile(r"[a-zàâçéèêëîïôûùüÿñæœ]+", re.IGNORECASE)


def classify(
    message: str,
    *,
    call_llm_fn: Callable[..., Any],
    main_runtime: Optional[dict],
    timeout: float = 8.0,
) -> str:
    """Return a pole in :data:`POLES`, or ``"DIRECT"``.

    Defensive by construction: an empty message, an LLM error/timeout, or an
    unrecognized reply all resolve to ``"DIRECT"`` so the caller falls back to the
    normal agent path (never drops a message because routing was uncertain).
    """
    msg = (message or "").strip()
    if not msg:
        return "DIRECT"
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content": msg[:2000]},
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
    board: Optional[str] = None,
    classify_timeout: float = 8.0,
) -> dict:
    """Classify *message* and, when it is a business request, create the kanban task
    in code + subscribe the notifier. Returns a decision dict::

        {"routed": bool, "category": str, "ack": Optional[str], "task_id": Optional[str]}

    ``routed=False`` (category ``DIRECT`` or any creation failure) means the caller
    MUST proceed with the normal agent dispatch — the message is never dropped.
    """
    category = classify(
        message, call_llm_fn=call_llm_fn, main_runtime=main_runtime, timeout=classify_timeout
    )
    if category == "DIRECT":
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}

    # Create the task exactly as the kanban_create tool does (assignee + running →
    # the dispatcher spawns the specialist worker). idempotency_key (the webhook
    # delivery id) makes a retried POST reuse the same task instead of double-routing.
    try:
        from hermes_cli import kanban_db

        conn = kanban_db.connect(board=board)
        try:
            task_id = kanban_db.create_task(
                conn,
                title=(message or "").strip()[:200] or "Demande",
                body=message,
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
