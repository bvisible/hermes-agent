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

# //// Neoffice — ENTIRE MODULE is a NORA addition (no upstream equivalent). Merged 2026-06-06
# from the richer osiris-poc router (RECURRENT class, keyword fast-path, conversation-aware
# routing, follow-up body, ack-via-desk) + the fork's stable-dir-workspace. Deterministic chat
# pre-router: classify (1 word) then CREATE the kanban task IN CODE — never rely on the LLM to
# call kanban_create (the "empty promise" timeout). See hermes-poc/CLAUDE.md. ////
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Business poles NORA routes to. Mirrors the orchestrator SOUL roster (the "Pôle"
# column) — keep in sync with configs/SOUL.md so deterministic routing matches what
# the orchestrator would have chosen on its good days.
POLES = ("compta", "ventes", "support", "rh", "analyse")

# Pole → user-facing label, PER LANGUAGE. NORA names the business "desk" to the user;
# the internal key (compta/…) is never leaked. French is canonical (Swiss-FR audience)
# and the default; the other languages mirror it for the multilingual chat path.
# //// Neoffice — multilingual desk labels + ack templates (was French-only) ////
POLE_LABELS = {
    "fr": {"compta": "Comptabilité", "ventes": "Ventes", "support": "Support", "rh": "Ressources Humaines", "analyse": "Analyse"},
    "de": {"compta": "Buchhaltung", "ventes": "Verkauf", "support": "Support", "rh": "Personalwesen", "analyse": "Analyse"},
    "it": {"compta": "Contabilità", "ventes": "Vendite", "support": "Supporto", "rh": "Risorse Umane", "analyse": "Analisi"},
    "en": {"compta": "Accounting", "ventes": "Sales", "support": "Support", "rh": "Human Resources", "analyse": "Analytics"},
}
# Backwards-compat alias (the French label map, still referenced by name elsewhere).
POLE_LABELS_FR = POLE_LABELS["fr"]

# Immediate-ack templates per language ({label} = the localized desk name).
ACK_TEMPLATES = {
    "fr": "Je transmets votre demande à votre pôle {label}, je reviens vers vous très vite.",
    "de": "Ich leite Ihre Anfrage an {label} weiter — ich melde mich gleich.",
    "it": "Inoltro la sua richiesta al reparto {label}, torno subito da lei.",
    "en": "I'm passing this to your {label} desk — I'll be right back with you.",
}


def _norm_lang(language: Optional[str]) -> str:
    """Normalize a Frappe code ('fr', 'fr-CH', 'de'…) to a supported ack language; default 'fr'."""
    code = (language or "").split("-")[0].strip().lower()
    return code if code in ACK_TEMPLATES else "fr"
# //// END Neoffice ////

# Per-conversation last route (in-memory, gateway-process-scoped). Lets the classifier
# resolve follow-ups IN CONTEXT: "Ceux de Daniel Moret" after "Combien de devis ouverts ?"
# (→ ventes) is the SAME ventes query filtered by a CLIENT — not an RH question about a
# person. Without this, each message is classified blind and a name-only follow-up gets
# mis-routed (verified 2026-06-03: "Ceux de Daniel Moret" → rh). Resets on gateway restart
# (the first follow-up after a restart degrades to context-free classify — acceptable).
_LAST_ROUTE: dict = {}
_LAST_ROUTE_MAX = 1000  # bound the dict; cleared wholesale when exceeded (cheap, rare)

# //// Neoffice — rolling per-conversation history of recent USER turns (the "film").
# A ROUTED worker runs as a FRESH, session-less kanban task: it sees ONLY the current
# message, so a MULTI-STEP request loses its thread (build a subscription → give the
# client, then the product, then the plan, across turns → by the last turn the worker
# no longer knows the client/goal from two turns earlier). Observed 2026-06-18: the
# worker created the client, then forgot it and the goal, then mis-parsed "Parfait" as a
# client name. We carry the recent user turns into the worker task body so it can pick up
# the build and continue to completion. In-memory, gateway-process-scoped, bounded like
# _LAST_ROUTE. (Long-term mem0 memory is per-user and unaffected — a separate mechanism;
# this only restores the short-term conversation thread for routed workers.) grep "//// Neoffice".
_CONV_HISTORY: dict = {}
_CONV_HISTORY_TURNS = 6  # how many recent user turns to carry into the worker
# //// END Neoffice ////

# //// Neoffice — briefing CTA → reply continuity (cross-process bridge).
# A WhatsApp morning briefing is pushed from Frappe straight to the WhatsApp router,
# OUTSIDE this gateway, so its call-to-action question ("voulez-vous que je prépare les
# rappels ?") never entered _LAST_ROUTE/_CONV_HISTORY. When the user replies "oui", we'd
# classify it blind → DIRECT → generic greeting, with NO continuity (reported 2026-06-19).
# The briefing records a "pending offer" {pole, action_en, question_fr, ts} keyed by phone
# in a shared file (Frappe + this gateway run as the SAME local user); we read it here on
# the FIRST inbound from that phone and continue the thread on the offer's pole. JSON
# contract mirrors nora/tasks/briefing_followup.py — keep in sync. grep "//// Neoffice".
import json as _json_off
import os as _os_off
import time as _time_off

_PENDING_OFFERS = _os_off.path.join(
    _os_off.path.expanduser("~"), ".hermes-nora-work", "pending_offers.json"
)
_OFFER_TTL_SECONDS = 6 * 3600
# An affirmative / "go ahead" reply → carry out the offered action. A name-only or new
# question is NOT here (it falls through to normal classification). Negative → drop it.
_AFFIRM_RE = re.compile(
    r"\b(oui|ouais|ok|okay|d'accord|daccord|volontiers|carr[ée]ment|vas[- ]?y|allez[- ]?y|"
    r"go|bien s[ûu]r|je veux bien|avec plaisir|les deux|pr[ée]pare|montre|envoie|fais[- ]?le|"
    r"parfait)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(
    r"\b(non|nope|pas maintenant|plus tard|laisse tomber|pas besoin|une autre fois)\b",
    re.IGNORECASE,
)


def _offer_phone_key(phone: Optional[str]) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


def _read_pending_offer(phone: Optional[str]) -> Optional[dict]:
    """Return a fresh pending offer for this phone, or None. Never raises."""
    key = _offer_phone_key(phone)
    if not key:
        return None
    try:
        with open(_PENDING_OFFERS, encoding="utf-8") as fh:
            data = _json_off.load(fh) or {}
    except Exception:
        return None
    off = data.get(key)
    if not isinstance(off, dict) or not off.get("pole"):
        return None
    if (_time_off.time() - float(off.get("ts", 0))) > _OFFER_TTL_SECONDS:
        return None
    return off


def _consume_pending_offer(phone: Optional[str]) -> None:
    """Delete the offer for this phone so it fires once. Never raises."""
    key = _offer_phone_key(phone)
    if not key:
        return
    try:
        with open(_PENDING_OFFERS, encoding="utf-8") as fh:
            data = _json_off.load(fh) or {}
        if key in data:
            del data[key]
            tmp = _PENDING_OFFERS + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json_off.dump(data, fh)
            _os_off.replace(tmp, _PENDING_OFFERS)
    except Exception:
        logger.warning("nora_chat_router: failed to consume pending offer")
# //// END Neoffice ////

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
    "- compta : factures, paiements, TVA, chiffre d'affaires, impayés, rappels de paiement / "
    "relances / rappels de facture, fournisseurs, commandes d'achat, rapports financiers "
    "(un chiffre demandé en TEXTE).\n"
    "- ventes : devis, commandes clients, factures de vente, articles, clients "
    "(création/recherche), prix.\n"
    "- support : emails (lecture/rédaction), pièces jointes & OCR, tickets, "
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

# ── Keyword fast-path (latency) ──────────────────────────────────────────────
# Route the OBVIOUS messages WITHOUT the ~1s LLM classifier call → the ack lands
# instantly. CONSERVATIVE on purpose: only UNAMBIGUOUS triggers are here. Ambiguous
# words ("facture", "client", "prix", "paiement") are deliberately absent — they fall
# through to the LLM (with the full SOUL) so routing quality is never sacrificed for
# speed. The fast-path is SKIPPED entirely when:
#   - there is a follow-up `prior` pole → the LLM-with-context path must handle it
#     ("Ceux de Daniel Moret" must stay on the prior pole), and
#   - the message carries recurrence markers → the 'recurrent' decision belongs to the LLM.
_RECUR_RE = re.compile(
    r"(tous les|chaque (jour|matin|soir|semaine|lundi|mardi|mercredi|jeudi|vendredi|mois)|"
    r"toutes les heures|r[ée]guli[èe]rement|automatiquement|planifie|programme[ -]?(moi|une|une t)|"
    r"fais[ -]?le tous|chaque fois)",
    re.IGNORECASE,
)
# (regex, pole) — first match wins. ANALYSE is checked FIRST: a chart/visual request goes
# to analyse regardless of subject (SOUL rule). Then the unambiguous domain keywords.
_FAST_PATH_RULES = (
    (re.compile(r"(graphique|en graphique|visuel|visualise|dataviz|tableau de bord|histogramme|camembert|courbe|diagramme)", re.IGNORECASE), "analyse"),
    # //// Neoffice — payment-reminder ("rappel de paiement" / "relance" / "rappel de facture")
    # routes DETERMINISTICALLY to compta. A dunning/Payment Reminder is a collections matter
    # (accounting), NOT a support ticket; the frappe_payment_reminder_create tool lives in compta
    # AND ventes (neoffice-devops commit c933017), never support. The phrasing is lexically
    # ambiguous to the LLM classifier (rappel→support, paiement/impayé→compta) so the model must
    # NOT decide it — a misroute to support lands on a pole without the tool → junk draft (observed
    # live). Placed ABOVE the generic compta rule (which matches "impayé" but not "relance"); a
    # plain "combien d'impayés ?" still hits compta below. grep "//// Neoffice".
    (re.compile(r"rappel[s]?\s+de\s+(paiement|facture)|lettre[s]?\s+de\s+relance|\brelanc\w*", re.IGNORECASE), "compta"),
    # //// END Neoffice ////
    (re.compile(r"(chiffre d'affaires|chiffre d affaires|\btva\b|impay[ée]|\bbilan\b|grand livre|écritures? comptables?|factures? fournisseur)", re.IGNORECASE), "compta"),
    (re.compile(r"(\bdevis\b|commande[s]? client|bon de commande client)", re.IGNORECASE), "ventes"),
    (re.compile(r"(cong[ée]s?\b|fiche de paie|bulletin de salaire|\bpaie\b|absences? (du|des))", re.IGNORECASE), "rh"),
)
# DIRECT only when the WHOLE message is a greeting/thanks/meta (so "Bonjour, quel est mon
# CA ?" is NOT caught here — the domain rules above match "chiffre d'affaires" first).
_DIRECT_RE = re.compile(
    r"^\s*(bonjour|salut|coucou|hello|hey|merci[\s!.]*|ça va|ca va|comment vas[ -]?tu|qui es[ -]?tu|que sais[ -]?tu faire)[\s!.?]*$",
    re.IGNORECASE,
)


def _fast_path(msg: str, prior: Optional[dict]) -> Optional[str]:
    """Unambiguous keyword → pole/'DIRECT' without an LLM call; else None (→ LLM classify).

    Never overrides the conversation-context (`prior`) or the 'recurrent' decision.
    """
    if prior and prior.get("pole"):
        return None  # follow-up → keep the context-aware LLM path
    if _RECUR_RE.search(msg):
        return None  # recurring request → the LLM owns the 'recurrent' classification
    for rx, pole in _FAST_PATH_RULES:
        if rx.search(msg):
            return pole
    if _DIRECT_RE.match(msg):
        return "DIRECT"
    return None


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
    # Latency: obvious messages skip the ~1s LLM call → the ack lands instantly.
    fast = _fast_path(msg, prior)
    if fast:
        logger.info("nora_chat_router: keyword fast-path → %s (no LLM call)", fast)
        return fast
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


def build_ack(pole: str, language: Optional[str] = None) -> str:
    """Acknowledgment naming the business pole, in the user's language (default French)."""
    # //// Neoffice — language-aware ack (was French-only) ////
    lang = _norm_lang(language)
    labels = POLE_LABELS.get(lang, POLE_LABELS["fr"])
    label = labels.get(pole) or POLE_LABELS["fr"].get(pole, "support")
    return ACK_TEMPLATES.get(lang, ACK_TEMPLATES["fr"]).format(label=label)
    # //// END Neoffice ////


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


# //// Neoffice — DETERMINISTIC FAST-PATH engine call. Ask the nora fast_answer engine
# (ONE structured-intent LLM call + a Frappe query in CODE) over the SAME desk callback
# the ack uses (X-Hermes-Token), exactly like _route_recurrent. Returns the answer text
# on a confident hit, else None → the caller routes to a worker (zero regression).
# grep "//// Neoffice".
def _fast_answer(
    message: str, chat_user: Optional[str], deliver_extra: Optional[dict]
) -> Optional[str]:
    extra = deliver_extra or {}
    cb = (extra.get("callback_url") or "").strip()
    token = (extra.get("callback_token") or "").strip()
    user = (chat_user or "").strip()
    _DELIVER = "nora.api.v2.hermes_callback.deliver"
    _FAST = "nora.api.fast_answer.answer_gateway"
    if not (cb and token and user) or _DELIVER not in cb:
        return None
    import json as _json
    import urllib.request

    url = cb.replace(_DELIVER, _FAST)
    body = _json.dumps({"user": user, "message": message}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Hermes-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = _json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("nora_chat_router: fast_answer POST failed → worker: %s", exc)
        return None
    if isinstance(data, dict) and data.get("answered") and (data.get("text") or "").strip():
        return data["text"].strip()
    return None
# //// END Neoffice ////


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
    language: Optional[str] = None,  # //// Neoffice — user's response language (multilingual) ////
    chat_phone: Optional[str] = None,  # //// Neoffice — phone, to match a pending briefing offer ////
) -> dict:
    """Classify *message* and, when it is a business request, create the kanban task
    in code + subscribe the notifier. Returns a decision dict::

        {"routed": bool, "category": str, "ack": Optional[str], "task_id": Optional[str]}

    ``routed=False`` (category ``DIRECT`` or any creation failure) means the caller
    MUST proceed with the normal agent dispatch — the message is never dropped.
    """
    prior = _LAST_ROUTE.get(conversation_id) if conversation_id else None

    # //// Neoffice — briefing CTA continuity. On the FIRST inbound after an out-of-band
    # briefing (no in-memory prior yet), an AFFIRMATIVE reply to NORA's recorded offer is
    # routed straight to the offer's pole, with the offered action injected as context
    # (below) so "oui" leads to the action instead of a blind DIRECT greeting. A negative
    # reply just consumes the offer; anything else falls through to normal classification.
    # grep "//// Neoffice".
    _offer = None
    if prior is None and chat_phone:
        _cand = _read_pending_offer(chat_phone)
        if _cand:
            if _AFFIRM_RE.search(message or ""):
                _offer = _cand
                _consume_pending_offer(chat_phone)
            elif _NEGATE_RE.search(message or ""):
                _consume_pending_offer(chat_phone)
    # //// END Neoffice ////

    if _offer:
        category = _offer["pole"]  # //// Neoffice — pole fixed by the accepted offer ////
    else:
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
        # //// Neoffice — accumulate the rolling conversation film (INCLUDING DIRECT turns,
        # which carry the goal, e.g. the opening "créer un abonnement"). grep "//// Neoffice".
        if len(_CONV_HISTORY) > _LAST_ROUTE_MAX:
            _CONV_HISTORY.clear()
        _conv_film = _CONV_HISTORY.setdefault(conversation_id, [])
        _conv_film.append((message or "")[:240])
        del _conv_film[:-_CONV_HISTORY_TURNS]
        # //// END Neoffice ////
    if category == "DIRECT":
        return {"routed": False, "category": "DIRECT", "ack": None, "task_id": None}

    # RECURRENT — a recurring "do this every X" request. Not a one-shot pole task: create
    # a scheduled task in code (deterministic, user-scoped) via the nora task_router and
    # deliver its ack. Falls back to the agent on any failure (zero regression).
    if category == "recurrent":
        return _route_recurrent(message, chat_user, deliver_extra)

    # ── Unify the conversation_id for the ack AND the worker result ──────────────────
    # The ack is delivered via deliver_extra["conversation_id"] (the cid the webhook
    # resolved from the inbound payload — provably the thread the desk polls, since the
    # user sees the ack), while the worker result is delivered via the notify-sub's
    # conversation_id (the raw payload conversation_id). When these two differ — observed
    # 2026-06-03 under the per-user session keying: the result landed in a spurious cid
    # (nora-8f29…) while the ack reached the desk thread — they sit in DIFFERENT desk
    # buffers, and get_replies returns the non-empty primary without falling back to the
    # other → the desk shows the ack but the result is stuck on "Nora consulte…" forever.
    # Force BOTH onto the deliver_extra cid (the ack's, which provably reaches the desk).
    _de_cid = ((deliver_extra or {}).get("conversation_id") or "").strip()
    _unified_cid = _de_cid or (conversation_id or None)
    logger.info(
        "nora_chat_router cid-unify: session=%s param_cid=%s deliver_extra_cid=%s → unified=%s",
        session_chat_id, conversation_id, _de_cid or None, _unified_cid,
    )

    # //// Neoffice — DETERMINISTIC FAST-PATH for simple data reads. Before spawning a
    # worker (Gemma 12b fumbles tool filters → loops 13-25× → ~25s + an apology), try the
    # nora fast_answer engine: ONE structured-intent LLM call + a Frappe query in CODE
    # (~0.5s, correct). On a hit we deliver it directly — no worker, no loop. French only
    # for now (the engine templates French); other languages fall through to the worker.
    # Any miss/uncertainty → None → normal worker route (zero regression). grep "//// Neoffice".
    if _norm_lang(language) == "fr":
        _fa_text = _fast_answer(message, chat_user, deliver_extra)
        if _fa_text:
            _fa_delivered = _post_ack_to_callback(
                _fa_text, {**(deliver_extra or {}), "conversation_id": _unified_cid}
            )
            logger.info(
                "nora_chat_router: FAST-ANSWER chat=%s → %s delivered=%s",
                session_chat_id, category, _fa_delivered,
            )
            return {
                "routed": True, "category": category, "ack": _fa_text,
                "task_id": None, "fast": True, "ack_delivered": _fa_delivered,
            }
    # //// END Neoffice ////

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
            # //// Neoffice — give the worker the conversation FILM (recent user turns) so a
            # MULTI-STEP request is CONTINUED, not restarted. The worker is session-less, so
            # without this it loses the thread (the client created two turns ago is invisible;
            # the goal stated three turns ago is gone). With the film it re-resolves entities
            # already created (they exist now) and drives to the final goal. Falls back to the
            # single-prior note when there is no longer film. grep "//// Neoffice".
            # Scaffolding fed to the worker LLM is ENGLISH (better model comprehension);
            # the user-facing REPLY stays in the user's language via the directive appended
            # below ([Reply to the user in <lang>]). grep "//// Neoffice".
            _body = message
            _film_prev = (_CONV_HISTORY.get(conversation_id) or [])[:-1]  # prior turns (drop current)
            # //// Neoffice — accepted briefing offer: inject the proposed action so "oui"
            # leads straight to it. Takes priority over the film/prior context. grep "//// Neoffice".
            if _offer:
                _body = (
                    "[The user is replying to NORA's morning-briefing proposal: "
                    f"« {(_offer.get('question_fr') or '').strip()} ». "
                    f"Proposed action: {(_offer.get('action_en') or '').strip()} "
                    "The user's reply is below; if it is affirmative, carry out the proposed "
                    "action NOW and present the result — do not merely acknowledge.]"
                    f"\n\n{message}"
                )
            elif _film_prev:
                _film_lines = "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(_film_prev))
                _body = (
                    "[ONGOING CONVERSATION — the user is carrying out a MULTI-STEP request. "
                    "Previous turns (oldest to newest):\n"
                    f"{_film_lines}\n"
                    "Take into account what has already been asked AND created in this thread: "
                    "do NOT redo what is done (entities created in earlier turns ALREADY EXIST — "
                    "look them up), and CONTINUE until the request is COMPLETE (not just one "
                    "isolated step). Current message below.]"
                    f"\n\n{message}"
                )
            elif prior and prior.get("pole") and prior.get("msg"):
                _body = (
                    f"[Conversation follow-up — the user first asked: « {prior['msg'][:300]} ». "
                    "The message below refines/continues that request; interpret it in that "
                    "context (a bare name = a customer to filter by).]"
                    f"\n\n{message}"
                )
            # //// Neoffice — tell the specialist worker which language to answer in (the
            # user's). ALWAYS carry it, FRENCH INCLUDED: the worker SOUL only "leans" FR, and a
            # cold model drifts to English without an explicit per-task directive (observed
            # 2026-06-18: fr user got an English worker summary). Deterministic beats hoping the
            # SOUL holds — the directive is one line at the BACK of the body (cache-safe, the
            # user turn is always last). grep "//// Neoffice".
            _wlang = _norm_lang(language)
            _lang_full = {"fr": "French", "de": "German", "it": "Italian", "en": "English"}.get(
                _wlang, _wlang
            )
            _body = f"{_body}\n\n[Reply to the user in {_lang_full}. Do not reply in any other language.]"
            # //// END Neoffice ////
            # //// Neoffice — stable worker cwd (re-ported from fork commit c68c362e6 after
            # taking the richer osiris-poc router). The dispatcher runs the worker with
            # cwd=<task workspace>; the default "scratch" workspace is ephemeral (deleted on
            # completion), so the worker's per-pole MCP stdio subprocess fails to start
            # (connected=False / tools=0, proven on the staging soak). A persistent "dir"
            # workspace gives a stable cwd so the MCP server connects. NORA metier workers
            # produce no files → a shared work dir is fine. grep "//// Neoffice".
            import os as _os_ws
            _nora_work = _os_ws.path.join(_os_ws.path.expanduser("~"), ".hermes-nora-work")
            try:
                _os_ws.makedirs(_nora_work, exist_ok=True)
            except OSError:
                _nora_work = None
            _ws_kwargs = (
                {"workspace_kind": "dir", "workspace_path": _nora_work}
                if _nora_work else {}
            )
            # //// END Neoffice ////
            task_id = kanban_db.create_task(
                conn,
                title=(message or "").strip()[:200] or "Demande",
                body=_body,
                assignee=category,
                created_by="nora-chat-router",
                initial_status="running",
                idempotency_key=idempotency_key,
                **_ws_kwargs,  # //// Neoffice — stable worker cwd ////
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
                conversation_id=_unified_cid,
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        # If we cannot create/subscribe the task, DO NOT silently drop the message —
        # fall back to the agent path (which may still route it). Surface the failure.
        logger.exception("nora_chat_router: task creation failed (%s) → DIRECT fallback", exc)
        return {"routed": False, "category": category, "ack": None, "task_id": None}

    ack = build_ack(category, language)
    # Deliver the immediate ack NOW so the user sees "I'm handing this to <pole>" right
    # away — it also makes the ~10s worker wait feel responsive. Failure to deliver the
    # ack must NEVER fail the routing (the worker result still arrives via the notifier).
    ack_delivered = _post_ack_to_callback(
        ack, {**(deliver_extra or {}), "conversation_id": _unified_cid}
    )
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
