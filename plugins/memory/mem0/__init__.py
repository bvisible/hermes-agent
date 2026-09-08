"""Mem0 memory plugin — MemoryProvider interface.

Server-side fact extraction and semantic search via the Mem0 Platform API (cloud), a
self-hosted Mem0 server (MEM0_HOST, HTTP), or OSS Memory. Secrets live in $HERMES_HOME/.env
(MEM0_API_KEY, MEM0_HOST); settings in $HERMES_HOME/mem0.json via `hermes memory setup`:
mode ("platform"|"oss"), host, user_id (canonical id across gateways; unset → gateway-native
id), agent_id. MEM0_* env vars remain a fallback.

//// Neoffice — fork divergence summary (grep "//// Neoffice"):
  * OSS backend is wrapped in a process-wide singleton + operation lock:
    initialize() runs on EVERY agent run, and a fresh qdrant-embedded client
    per run would re-open (and re-lock) the on-disk store each turn — the
    same per-turn reload bug we fixed for FAISS (7s/turn), plus qdrant local
    mode does not tolerate two live clients on one path.
  * Shared company-scope bucket: scope="company" facts land in ONE bucket
    (user_id=company_id) visible to every user of the instance; reads merge
    the per-user bucket + the company bucket.
  * Per-turn capture stores the RAW turn (infer=False) — reliable, zero LLM
    load; the nightly consolidation distills durable facts (memory_retain).
  * retain_facts(): bulk verbatim store used by the gateway webhook
    (event_type "memory_retain") for NORA's end-of-day consolidation.
  * mem0_profile / mem0_conclude kept as hidden aliases of mem0_list /
    mem0_add so pre-v2026.7.1 SOUL prompts and skills keep working.
//// END Neoffice ////
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Circuit breaker: after _BREAKER_THRESHOLD consecutive failures, pause API
# calls for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD, _BREAKER_COOLDOWN_SECS, _PREFETCH_WAIT_SECS = 5, 120, 3
_CLIENT_ERROR_TYPES = ("MemoryNotFoundError", "ValidationError")
# Placeholder user_id. initialize() treats it as "no operator-configured user_id"
# so legacy mem0.json files written by the wizard don't override gateway-native ids.
_DEFAULT_USER_ID = "hermes-user"

# //// Neoffice — process-wide singleton for the OSS (self-hosted) backend.
# initialize() runs on EVERY agent run and rebuilds self._backend, so without
# a cache the embedded vector store would be re-opened each turn: for FAISS
# that cost ~7s/turn (agent.log showed "Loaded FAISS index" 57x for 77 turns);
# for qdrant embedded a second live client on the same path conflicts with the
# storage lock. One backend per oss-config, built once per gateway process.
# _LockedBackend serialises every operation: sync_turn (thread) + prefetch
# (thread) + tool calls can hit the store concurrently, and neither FAISS nor
# qdrant-embedded guarantees concurrent add/search safety. Closed at process
# exit only (never by a provider's shutdown — other runs share it).
_OSS_BACKEND_SINGLETON: Dict[str, Any] = {}
_OSS_SINGLETON_LOCK = threading.Lock()


class _LockedBackend:
    """Proxy that serialises all backend calls under one process-wide lock."""

    def __init__(self, backend: Any) -> None:
        self.__dict__["_backend"] = backend
        self.__dict__["_op_lock"] = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__dict__["_backend"], name)
        if not callable(attr):
            return attr
        lock = self.__dict__["_op_lock"]

        def _locked(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return attr(*args, **kwargs)

        return _locked


def _close_oss_singletons() -> None:
    with _OSS_SINGLETON_LOCK:
        for backend in _OSS_BACKEND_SINGLETON.values():
            try:
                backend.close()
            except Exception:
                pass
        _OSS_BACKEND_SINGLETON.clear()


atexit.register(_close_oss_singletons)
# //// END Neoffice ////


def _is_client_error(exc: Exception) -> bool:
    """True for user-caused errors (bad ID, not found) that should NOT trip circuit breaker."""
    err_str = str(exc).lower()
    return type(exc).__name__ in _CLIENT_ERROR_TYPES or any(s in err_str for s in ("404", "not found", "valid uuid"))


def _read_mem0_json(config_path: Path) -> dict:
    """Best-effort read of mem0.json; missing/corrupt file -> {}."""
    if config_path.exists():
        with suppress(Exception):
            return json.loads(config_path.read_text(encoding="utf-8"))
    return {}

# //// Neoffice — pure conversational filler must never become a memory.
# Greetings and acknowledgements ("Bonjour", "C'est bien noté, j'ai enregistré
# votre code") carry nothing to recall, yet with infer=False they were stored
# verbatim, forever: 25% of a real store, measured on osiris 2026-07-29.
#
# DELIBERATELY CONSERVATIVE — memory is the product's core value, so the cost of
# dropping a real fact is far higher than the cost of keeping one extra ack. A
# message is discarded ONLY when all three hold:
#   1. it is short (long text is almost always substance),
#   2. it contains NO digit (codes, amounts, dates, references all carry digits),
#   3. it *opens* with a known politeness/ack formula.
# So "C'est noté, votre code est QW771234" is KEPT (rule 2), and anything the
# pattern does not recognise is KEPT (default = remember). grep "//// Neoffice".
_ACK_OPENING_RE = re.compile(
    r"^\s*(?:"
    r"c'est\s+(?:bien\s+)?not[ée]|bien\s+not[ée]|j'ai\s+(?:bien\s+)?enregistr|"
    r"je\s+vais\s+(?:tr[èe]s\s+)?bien|avec\s+plaisir|je\s+suis\s+l[àa]\s+pour|"
    r"comment\s+(?:puis-je|puis\s+je)|n'h[ée]sitez\s+pas|"
    r"bonjour|bonsoir|salut|merci|d'accord|parfait|tr[èe]s\s+bien|ok\b|"
    r"noted\b|you're\s+welcome|how\s+can\s+i\s+help|hello\b|hi\b|thanks\b|thank\s+you|"
    # //// Neoffice — sign-offs observed polluting the osiris store.
    r"[àa]\s+(?:tout\s+[àa]\s+l'heure|bient[ôo]t|demain|plus\s+tard)|"
    r"bonne\s+(?:journ[ée]e|soir[ée]e|nuit)|au\s+revoir|bye\b|see\s+you|good\s+(?:bye|night)"
    r")",
    re.IGNORECASE,
)

# //// Neoffice — a BARE acknowledgement, and nothing else.
# Measured on the osiris store: 1424 of 1425 points were raw conversational
# capture, and searching a supplier name returned "Oui." and "À tout à l'heure !"
# scoring ABOVE the real facts. But these words must only be dropped when they
# ARE the whole message: "Oui, c'est Romande Énergie notre fournisseur
# d'électricité" is exactly the fact we exist to keep. Hence the anchored match
# — an opening-prefix rule silently deleted both of those in testing.
_BARE_ACK_RE = re.compile(
    r"^\s*(?:oui|non|ouais|ouaip|yep|yes|no|si|voil[àa]|exact|exactement|"
    r"tout\s+[àa]\s+fait|c'est\s+(?:[çc]a|bon|clair)|super|g[ée]nial|nickel|top)"
    r"[\s!.…]*$",
    re.IGNORECASE,
)
# //// END Neoffice ////
_MEMORY_FILLER_MAX_LEN = 160

# A question ASKING memory back ("tu te souviens de mon code ?") states no fact —
# it is the user querying, not informing. Storing these was measurable pollution:
# the same question sat 4 times in a production store, and each copy is recalled
# later as if it were knowledge. Only RECALL questions are matched; a question
# that CARRIES information ("peux-tu noter que je préfère le vouvoiement ?") does
# not match and is kept, and the digit rule below still protects anything with a
# code, amount or date. grep "//// Neoffice".
_RECALL_QUESTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:est-ce que\s+)?tu\s+te\s+(?:souviens|rappelles)|te\s+(?:souviens|rappelles)-tu|"
    r"vous\s+(?:souvenez|rappelez)-vous|"
    r"quel(?:le)?s?\s+(?:est|sont|était)|c'est\s+quoi\s+(?:mon|ma|mes|le|la)|"
    r"peux-tu\s+me\s+(?:rappeler|redire|redonner)|"
    r"pouvez-vous\s+me\s+(?:rappeler|redire|redonner)|"
    r"do\s+you\s+remember|what\s+(?:is|are|was)\s+my|can\s+you\s+remind\s+me|"
    # //// Neoffice — "Comment je m'appelle ?" is the user TESTING recall, not
    # stating a fact; storing it teaches the model its own question back.
    r"comment\s+je\s+m'appelle|qui\s+suis[-\s]je|comment\s+(?:je\s+)?m'appelais|"
    r"tu\s+sais\s+(?:qui|comment)\s+je|what's\s+my\s+name|who\s+am\s+i"
    r")",
    re.IGNORECASE,
)


def _is_low_value_for_memory(text: Optional[str]) -> bool:
    """True for short digit-free politeness, and for pure recall questions."""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # A digit means a code / amount / date may be stated: always worth keeping.
    if any(ch.isdigit() for ch in stripped):
        return False
    if stripped.endswith("?") and _RECALL_QUESTION_RE.match(stripped):
        return True
    # //// Neoffice — a bare "Oui." carries nothing; "Oui, c'est Romande Énergie"
    # carries everything. Only the anchored form is dropped.
    if _BARE_ACK_RE.match(stripped):
        return True
    # //// END Neoffice ////
    if len(stripped) > _MEMORY_FILLER_MAX_LEN:
        return False
    return bool(_ACK_OPENING_RE.match(stripped))
# //// END Neoffice ////


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Env vars provide defaults; $HERMES_HOME/mem0.json overrides individual keys.
    Layering avoids a silent failure when the JSON file exists but lacks fields
    like ``api_key`` that the user set in ``.env``."""
    from hermes_constants import get_hermes_home
    config = {"mode": os.environ.get("MEM0_MODE", "platform"), "api_key": get_secret("MEM0_API_KEY", ""), "host": os.environ.get("MEM0_HOST", ""), "agent_id": os.environ.get("MEM0_AGENT_ID", "hermes"), "oss": {}}
    if os.environ.get("MEM0_USER_ID"):  # only when explicitly configured, so initialize() can fall back to the gateway-native id
        config["user_id"] = os.environ["MEM0_USER_ID"]
    file_cfg = _read_mem0_json(get_hermes_home() / "mem0.json")
    config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
    return config


def _schema(name: str, description: str, properties: dict[str, tuple[str, str]], required: list[str]) -> dict:
    props = {k: {"type": t, "description": d} for k, (t, d) in properties.items()}
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": props, "required": required}}


TOOL_SCHEMAS = [
    _schema("mem0_search", "Search the user's memories by meaning; returns facts ranked by relevance. Use this before answering any question that may depend on what you know about the user (preferences, facts, history, people, projects, past decisions). For multi-part or multi-hop questions, call it several times — vary the wording and run follow-up searches on what earlier results reveal; one search is rarely enough.",
            {"query": ("string", "What to search for."), "top_k": ("integer", "Max results (default: 10, max: 50)."), "rerank": ("boolean", "Rerank results for relevance (default: false, platform mode only).")}, ["query"]),
    # //// Neoffice — `scope` added to the write schema: a fact can be stored in the
    # //// shared company bucket instead of the caller's private one. One instance =
    # //// one company, so a company fact must be recallable by every colleague.
    # //// Drop when upstream supports a shared bucket. ////
    _schema("mem0_add", "Store a durable fact about the user, verbatim (no LLM extraction). Call this the moment the user states a lasting preference, correction, decision, or personal detail worth recalling on future turns — don't wait to be asked to remember. Skip transient chit-chat and facts you've already stored.",
            {"content": ("string", "The fact to store."), "scope": ("string", "user = private to this user (default); company = shared with everyone in the organisation.")},
            ["content"]),
    _schema("mem0_update", "Replace the text of an existing memory by its ID (take the ID from a mem0_search result). Use when a stored fact has changed or was wrong — correct it in place instead of adding a duplicate.",
            {"memory_id": ("string", "Memory UUID to update."), "text": ("string", "New text content.")}, ["memory_id", "text"]),
    _schema("mem0_delete", "Delete a memory by its ID (take the ID from a mem0_search result). Use when a stored fact is obsolete or the user asks you to forget it; prefer mem0_update if the fact merely changed.",
            {"memory_id": ("string", "Memory UUID to delete.")}, ["memory_id"]),
    # //// Neoffice — mem0_list is OURS: upstream removed the paging tool, but NORA's
    # //// `mem0_profile` alias and the nightly consolidation both page the whole bucket
    # //// (the consolidation compares every stored fact to the day's transcript to find
    # //// the stale one). Search alone cannot enumerate. Drop if upstream restores a
    # //// listing tool of its own.
    _schema("mem0_list", "List the user's stored memories, newest first, with pagination. Use it to review or audit what is remembered when a search is not enough; prefer mem0_search to answer a question.",
            {"page": ("integer", "1-based page number (default 1)."),
             "page_size": ("integer", "Memories per page, 1-200 (default 100).")}, []),
    # //// END Neoffice ////
]

_PROMPT_BODY = (
    "You have persistent memory of this user from past conversations. You should call mem0_search before answering anything that could depend on prior context (the user's preferences, facts, history, people, projects, or earlier decisions) — do not rely on the chat window alone, and do not assume you have no memory.\n"
    "For multi-part or multi-hop questions, run several searches with different wording/angles and follow-up searches on what the first results surface; one search is rarely enough. Keep searching until you have every fact the question needs before you answer.\n"
    "Tools: mem0_search to find memories, mem0_add to store facts, mem0_update and mem0_delete to manage by ID."
)



# //// Neoffice — v2026.9.7 removed the `_write_metadata()` method and inlined its body at
# //// each upstream call site. Our raw per-turn capture and `retain_facts` still need it, so
# //// keep one function with the SAME shape rather than re-inlining it twice and letting the
# //// two drift. Drop if upstream reinstates a metadata helper.
def _neoffice_write_metadata(provider: Any) -> Dict[str, Any]:
    """Tag a write with the gateway channel, exactly as upstream's inlined literal does."""
    return {"channel": provider._channel} if provider._channel else {}
# //// END Neoffice ////

class Mem0MemoryProvider(MemoryProvider):
    """Mem0 memory with server-side extraction and semantic search (platform, self-hosted or OSS)."""

    def __init__(self):
        self._config = self._backend = self._sync_thread = self._prefetch_thread = None
        self._mode, self._api_key, self._host, self._user_id, self._agent_id = "platform", "", "", _DEFAULT_USER_ID, "hermes"
        self._rerank_default, self._channel = False, "cli"  # channel = gateway name (cli/telegram/discord/...)
        self._prefetch_query = self._prefetch_result = ""
        self._prefetch_done = self._atexit_registered = False
        self._consecutive_failures, self._breaker_open_until = 0, 0.0  # circuit breaker state
        self._breaker_lock, self._sync_lock, self._prefetch_lock = threading.Lock(), threading.Lock(), threading.Lock()

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        cfg = _load_config()
        if cfg.get("mode", "platform") == "oss":
            return bool(cfg.get("oss", {}).get("vector_store"))
        return bool(cfg.get("api_key") or cfg.get("host"))  # platform needs a key; self-hosted a host (key optional with AUTH_DISABLED)

    def save_config(self, values, hermes_home):
        """Merge-write config to $HERMES_HOME/mem0.json."""
        from utils import atomic_json_write
        config_path = Path(hermes_home) / "mem0.json"
        atomic_json_write(config_path, {**_read_mem0_json(config_path), **values}, mode=0o600)

    def get_config_schema(self):
        api_key_required = _load_config().get("mode", "platform") != "oss"
        return [
            {"key": "api_key", "description": "Mem0 Platform API key", "secret": True, "required": api_key_required, "env_var": "MEM0_API_KEY", "url": "https://app.mem0.ai"},
            {"key": "host", "description": "Self-hosted Mem0 server URL (leave blank for cloud)", "required": False, "env_var": "MEM0_HOST"},
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
            {"key": "rerank", "description": "Enable reranking for recall", "default": "false", "choices": ["true", "false"]},
        ]

    def post_setup(self, hermes_home: str, config: dict) -> None:
        from ._setup import post_setup
        post_setup(hermes_home, config)

    def _oss_hint(self, template: str, default: str = "vector store") -> str:
        """OSS-only hint; ``{vs}`` is the configured vector-store provider. "" in other modes."""
        return template.format(vs=self._config.get("oss", {}).get("vector_store", {}).get("provider", default)) if self._mode == "oss" else ""

    def _create_backend(self):
        # Lazy-install the mem0 SDK before the backend imports it (honors security.allow_lazy_installs);
        # on failure the backend import raises the canonical error, captured below.
        with suppress(Exception):
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("memory.mem0", prompt=False)
        try:
            from . import _backend
            if self._mode == "oss":
                # //// Neoffice — one OSS backend per config, shared across runs
                # (see module docstring: per-run rebuild = per-turn store reload
                # for FAISS / storage-lock conflict for qdrant embedded). The
                # provider's shutdown() must NOT close it; atexit does.
                from ._backend import OSSBackend
                oss_cfg = self._config.get("oss", {})
                cache_key = json.dumps(oss_cfg, sort_keys=True, default=str)
                with _OSS_SINGLETON_LOCK:
                    cached = _OSS_BACKEND_SINGLETON.get(cache_key)
                    if cached is not None:
                        return cached
                    backend = _LockedBackend(OSSBackend(oss_cfg))
                    _OSS_BACKEND_SINGLETON[cache_key] = backend
                    logger.info(
                        "Mem0 OSS backend built once as process singleton — "
                        "reused across runs, no per-turn reload"
                    )
                    return backend
                # //// END Neoffice ////
            return _backend.SelfHostedBackend(self._api_key, self._host) if self._host else _backend.PlatformBackend(self._api_key)
        except Exception as e:
            logger.error("Mem0 backend failed to initialize (%s mode): %s", self._mode, e)
            self._init_error = str(e)
            return None

    def _is_breaker_open(self) -> bool:
        """True while the breaker is tripped; an expired cooldown resets the failure count."""
        with self._breaker_lock:
            if self._consecutive_failures >= _BREAKER_THRESHOLD and time.monotonic() < self._breaker_open_until:
                return True
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._consecutive_failures = 0
            return False

    def _format_error(self, prefix: str, exc: Exception) -> str:
        msg = f"{prefix}: {exc}"
        if any(s in str(exc).lower() for s in ("connection", "refused", "timeout")):
            msg += self._oss_hint(" (check that {vs} is running)")
        return msg

    def _record_success(self):
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._breaker_lock:
            self._consecutive_failures = count = self._consecutive_failures + 1
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
        if count >= _BREAKER_THRESHOLD:
            hint = self._oss_hint(" Check that your {vs} vector store is running and reachable.", "unknown")
            logger.warning("Mem0 circuit breaker tripped after %d consecutive failures. Pausing API calls for %ds.%s", count, _BREAKER_COOLDOWN_SECS, hint)

    def _try(self, call, log, msg: str):
        """Background-path wrapper: run ``call`` under the breaker; on error log ``msg`` and return None."""
        try:
            result = call()
        except Exception as e:
            self._record_failure()
            log(msg, e)
            return None
        self._record_success()
        return result

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = cfg = _load_config()
        self._mode, self._api_key, self._host, self._agent_id = cfg.get("mode", "platform"), cfg.get("api_key", ""), cfg.get("host", ""), cfg.get("agent_id", "hermes")
        # user_id precedence: operator-configured (env/mem0.json) > gateway-native id (kwargs) > _DEFAULT_USER_ID.
        # The literal placeholder counts as unset so wizard users still get gateway-native ids.
        configured = cfg.get("user_id")
        self._user_id = (None if configured == _DEFAULT_USER_ID else configured) or kwargs.get("user_id") or _DEFAULT_USER_ID
        # Persisted rerank preference: default for mem0_search when the model omits ``rerank``. Platform-only.
        _rr = cfg.get("rerank", False)
        self._rerank_default = _rr.lower() in ("true", "1", "yes") if isinstance(_rr, str) else bool(_rr)
        # //// Neoffice — shared company-scope memory. A scope="company" fact
        # lands in ONE shared bucket (user_id=company_id) so EVERY user of the
        # instance sees it; personal facts stay per-user and reads merge both.
        # The gateway passes company_id per call; default "company" = one
        # shared bucket per instance. grep "//// Neoffice".
        self._company_id = kwargs.get("company_id") or self._config.get("company_id", "company")
        # //// END Neoffice ////
        self._channel = kwargs.get("platform") or "cli"
        self._backend = self._create_backend()
        if self._backend and not self._atexit_registered:
            atexit.register(self._shutdown_backend)
            self._atexit_registered = True

    def _search(self, query: str, top_k: int = 10, rerank: bool = False, backend=None) -> list:
        # Scoped to user_id only — by design — so recall surfaces memories from any gateway/agent under this
        # principal; writes attach agent_id and metadata.channel so narrower views remain possible at query time.
        return (backend or self._backend).search(query, filters={"user_id": self._user_id}, top_k=top_k, rerank=rerank)

    def _add(self, messages: list, infer: bool):
        metadata = {"channel": self._channel} if self._channel else {}
        return self._backend.add(messages, user_id=self._user_id, agent_id=self._agent_id, infer=infer, metadata=metadata)

    # //// Neoffice — company-scope memory (shared bucket across the instance's users) ////
    def _scoped_read_buckets(self) -> List[Dict[str, Any]]:
        """Buckets a read covers: the caller's per-user bucket + the shared
        company bucket. Personal facts stay private; company facts are visible
        to every user of the instance."""
        # //// Neoffice — v2026.9.7 removed the `_read_filters()` helper and inlined
        # //// `filters={"user_id": self._user_id}` at each call site; mirror that literal
        # //// here so the per-user bucket keeps the exact upstream shape.
        buckets = [{"user_id": self._user_id}]
        company_id = getattr(self, "_company_id", None)
        if company_id and company_id != self._user_id:
            buckets.append({"user_id": company_id})
        return buckets

    def _merged_search(self, backend, query: str, *, top_k: int, rerank: bool) -> list:
        """backend.search over the per-user + company buckets, deduped by id,
        best-score first — with a bounded recency bonus."""
        seen, out = set(), []
        for filt in self._scoped_read_buckets():
            for item in backend.search(query, filters=filt, top_k=top_k, rerank=rerank) or []:
                key = item.get("id") or item.get("memory")
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
        out.sort(key=self._recency_ranked_score, reverse=True)
        return out[:top_k]

    # //// Neoffice — when two memories state the SAME thing with different values,
    # the newer one must win at RECALL time. The nightly consolidation retires the
    # stale one, but between two runs both are live: a user who restates a code in
    # the morning would still be answered with yesterday's value (measured: llm/09
    # scores 1/3 when three codes arrive within minutes).
    #
    # Deliberately a SMALL, BOUNDED bonus (≤ +12%) that decays over ~30 days: it can
    # only reorder near-ties — i.e. memories about the same subject — and can never
    # lift an off-topic memory above a relevant one, which a hard "newest first"
    # sort would do. Undated memories get no bonus rather than being penalised.
    _RECENCY_MAX_BONUS = 0.12
    _RECENCY_HALFLIFE_DAYS = 30.0

    def _recency_ranked_score(self, item: dict) -> float:
        score = float(item.get("score") or 0.0)
        created = item.get("created_at") or item.get("updated_at")
        if not created:
            return score
        try:
            from datetime import datetime, timezone

            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)
        except (ValueError, TypeError, OverflowError):
            return score
        freshness = 0.5 ** (age_days / self._RECENCY_HALFLIFE_DAYS)
        return score * (1.0 + self._RECENCY_MAX_BONUS * freshness)
    # //// END Neoffice ////

    def _merged_get_all(self, backend, *, page: int, page_size: int) -> dict:
        """backend.get_all over the per-user + company buckets, deduped, then
        paginated in memory (same strategy the OSS backend itself uses)."""
        seen, out = set(), []
        for filt in self._scoped_read_buckets():
            response = backend.get_all(filters=filt, page=1, page_size=10000)
            for item in (response or {}).get("results", []):
                key = item.get("id") or item.get("memory")
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
        total = len(out)
        start = (page - 1) * page_size
        return {"results": out[start:start + page_size], "count": total}
    # //// END Neoffice ////

    def system_prompt_block(self) -> str:
        # Mirror _create_backend precedence (oss > host > platform). Rerank is a Mem0 Platform feature only.
        mode_label = "OSS (self-hosted)" if self._mode == "oss" else "self-hosted (HTTP API)" if self._host else "platform (cloud API)"
        rerank_note = " Rerank is available on search." if (self._mode == "platform" and not self._host) else ""
        return f"# Mem0 Memory\nActive. Mode: {mode_label}. User: {self._user_id}.\n{_PROMPT_BODY}{rerank_note}"

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def _consume_prefetch_result(self, query: str) -> str | None:
        """Pop the finished prefetch body for ``query`` (None if absent or still running)."""
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result, self._prefetch_result, self._prefetch_done = self._prefetch_result, "", False
            return result

    def _start_prefetch(self, query: str) -> None:
        backend = self._backend
        if not query or backend is None or self._is_breaker_open():
            return

        def _run():
            # //// Neoffice — the prefetch reads the per-user AND the shared company bucket.
            # //// Upstream searches self._user_id only, so a scope="company" fact would be
            # //// invisible to the very prefetch that primes the system prompt. Drop when
            # //// upstream supports a shared bucket of its own.
            results = self._try(
                lambda: self._merged_search(backend, query, top_k=10, rerank=True),
                logger.debug, "Mem0 prefetch failed: %s")
            # //// END Neoffice ////
            lines = [r.get("memory", "") for r in (results or []) if r.get("memory")]
            body = "## Mem0 Memory\n" + "\n".join(f"- {l}" for l in lines) if lines else ""
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result, self._prefetch_done = body, True

        with self._prefetch_lock:
            # Same query already answered or still in flight: don't restart it.
            if self._prefetch_query == query and (self._prefetch_done or (self._prefetch_thread and self._prefetch_thread.is_alive())):
                return
            self._prefetch_query, self._prefetch_result, self._prefetch_done = query, "", False
            self._prefetch_thread = t = threading.Thread(target=_run, daemon=True, name="mem0-prefetch")
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        if (cached := self._consume_prefetch_result(query)) is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        return self._consume_prefetch_result(query) or ""  # slow backend: skip injection; mem0_search remains the backstop

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Send the turn to Mem0 for server-side fact extraction (non-blocking)."""
        if self._backend is None or self._is_breaker_open():
            return
        # //// Neoffice — a turn with no real owner is not a memory. self._user_id
        # falls back to _DEFAULT_USER_ID for ownerless runs: the 8-minute
        # chat-warmup ping and every kanban worker turn landed there — 4 325 of
        # dmis's 4 361 stored "memories" were literally « Reponds uniquement: ok »
        # and srv02 (a template with zero users) held 11 236 of them (measured
        # 2026-09-01). Nobody recalls that bucket on purpose (user recall scopes
        # the canonical id + company), and the warmup's own recall block changed
        # as its pings accumulated — churning the very prefix cache the warmup
        # exists to keep warm. No owner → no per-turn capture; the end-of-day
        # consolidation remains the writer of durable facts. A CLI session that
        # wants capture can still configure an explicit user_id.
        if (self._user_id or _DEFAULT_USER_ID) == _DEFAULT_USER_ID:
            return
        # //// END Neoffice ////

        def _sync():
            backend = self._backend
            if backend is None:
                return
            try:
                messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ]
                # //// Neoffice — per-turn capture stores the RAW turn (infer=False).
                # The mem0 extraction LLM (Olares) is the same GPU the chat and the
                # workers share: infer=True adds one LLM extraction call per turn and
                # fails whenever that endpoint is saturated or unconfigured — the
                # breaker then opens and real-time capture silently stops (desk/
                # console "forgot" within the same day). Storing raw (embeddings
                # only, NO chat LLM) makes per-turn capture RELIABLE and immediately
                # recallable, and puts ZERO load on the chat GPU. The end-of-day
                # consolidation (consolidate_pending → memory_retain) still distills
                # the high-signal durable facts. grep "//// Neoffice".
                #
                # Drop conversational filler BEFORE writing. infer=False means nothing
                # judges what lands in the store, so every "C'est bien noté" and
                # "Bonjour" became a permanent memory: measured 2026-07-29 on osiris,
                # 25% of a real user's store was acks. That is not just clutter — the
                # recalled block is injected into the system prompt, so a store that
                # grows every turn makes the prompt prefix change every turn, which
                # invalidates llama.cpp's prefix cache and forces a full ~22k-token
                # re-prefill (7-8s) on EVERY request instead of ~0.2s. Keeping filler
                # out is therefore both a memory-quality AND a latency fix.
                kept = [m for m in messages if not _is_low_value_for_memory(m["content"])]
                if not kept:
                    return  # nothing worth remembering in this turn
                backend.add(
                    kept,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    infer=False,
                    metadata=_neoffice_write_metadata(self),
                )
                # //// END Neoffice ////
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Mem0 sync failed: %s", e)

        with self._sync_lock:
            prev = self._sync_thread
            if prev and prev.is_alive():
                prev.join(timeout=5.0)
                if prev.is_alive():  # still busy after the wait: skip to avoid duplicate ingestion
                    return
            self._sync_thread = threading.Thread(target=_sync, daemon=True, name="mem0-sync")
            self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(TOOL_SCHEMAS)

    # -- tool handlers: (required params, error label, body, client-error policy) ---
    # Client errors (bad ID / not found) never trip the breaker, except for mem0_add
    # where they count as failures; update/delete answer them with "Memory not found".

    def _tool_search(self, args: dict) -> str:
        top_k = max(1, min(int(args.get("top_k", 10)), 50))
        rerank_raw = args.get("rerank", self._rerank_default)
        rerank = rerank_raw.lower() not in ("false", "0", "no") if isinstance(rerank_raw, str) else bool(rerank_raw)
        # //// Neoffice — read the per-user AND the shared company bucket (upstream reads
        # //// user_id only), and surface created_at: the nightly consolidation uses it to
        # //// decide which of two contradicting facts is the stale one, and the recency
        # //// bonus in _merged_search is only debuggable with the date visible.
        results = self._merged_search(self._backend, args["query"], top_k=top_k, rerank=rerank)
        if not results:
            return json.dumps({"result": "No relevant memories found."})
        items = [{"id": r.get("id"), "memory": r.get("memory", ""), "score": r.get("score", 0),
                  "created_at": r.get("created_at")} for r in results]
        # //// END Neoffice ////
        return json.dumps({"results": items, "count": len(items)})

    # //// Neoffice — added handler: upstream dropped mem0_list, we still need to page the
    # //// whole bucket (see the schema note above). Reads both buckets and keeps created_at.
    def _tool_list(self, args: dict) -> str:
        page = max(1, int(args.get("page", 1)))
        page_size = min(max(1, int(args.get("page_size", 100))), 200)
        response = self._merged_get_all(self._backend, page=page, page_size=page_size)
        results = (response or {}).get("results", [])
        if not results:
            return json.dumps({"result": "No memories stored yet."})
        items = [{"id": m.get("id"), "memory": m.get("memory", ""),
                  "created_at": m.get("created_at")} for m in results]
        return json.dumps({"results": items, "count": response.get("count", len(items)),
                           "page": page, "page_size": page_size})
    # //// END Neoffice ////

    def _tool_add(self, args: dict) -> str:
        # //// Neoffice — accept the legacy `conclusion` spelling (pre-v2026.7.1 SOUL prompts
        # //// and skills still say mem0_conclude(conclusion=...)), and honour scope="company"
        # //// by writing into the shared bucket instead of the caller's private one.
        content = args.get("content", "") or args.get("conclusion", "")
        if not content:
            return tool_error("Missing required parameter: content")
        if str(args.get("scope") or "user").lower() == "company":
            result = self._backend.add(
                [{"role": "user", "content": content}], user_id=self._company_id,
                agent_id=self._agent_id, infer=False,
                metadata={"channel": self._channel} if self._channel else {})
            event_id = result.get("event_id") if isinstance(result, dict) else None
            msg = "Fact stored." if (self._mode == "oss" or self._host) else "Fact queued for storage."
            return json.dumps({"result": msg, "event_id": event_id})
        # //// END Neoffice ////
        result = self._add([{"role": "user", "content": content}], infer=False)
        event_id = result.get("event_id") if isinstance(result, dict) else None
        # Cloud add is async (server-side extraction); OSS and self-hosted store synchronously.
        msg = "Fact stored." if (self._mode == "oss" or self._host) else "Fact queued for storage."
        return json.dumps({"result": msg, "event_id": event_id})

    _TOOL_HANDLERS = {
        "mem0_search": (("query",), "Search failed", _tool_search, "skip"),
        # //// Neoffice — mem0_list (see the schema and the handler above) ////
        "mem0_list": ((), "Failed to list memories", _tool_list, "skip"),
        # //// Neoffice — no required-param gate: the handler accepts `conclusion` too ////
        "mem0_add": ((), "Failed to store", _tool_add, "count"),
        "mem0_update": (("memory_id", "text"), "Update failed", lambda self, a: json.dumps(self._backend.update(a["memory_id"], a["text"])), "not_found"),
        "mem0_delete": (("memory_id",), "Delete failed", lambda self, a: json.dumps(self._backend.delete(a["memory_id"])), "not_found"),
    }

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        # //// Neoffice — hidden aliases so pre-v2026.7.1 SOUL prompts and
        # skills keep working after the upstream tool rename: mem0_profile
        # was replaced by mem0_list, mem0_conclude by mem0_add. ////
        if tool_name == "mem0_profile":
            tool_name = "mem0_list"
        elif tool_name == "mem0_conclude":
            tool_name = "mem0_add"
        # //// END Neoffice ////
        if self._backend is None:
            err = getattr(self, "_init_error", "unknown error")
            return json.dumps({"error": f"Mem0 backend not initialized: {err}.{self._oss_hint(' Check that {vs} is running and reachable.')}"})
        if self._is_breaker_open():
            return json.dumps({"error": f"Mem0 temporarily unavailable (multiple consecutive failures). Will retry automatically.{self._oss_hint(' Check that your {vs} is running.')}"})
        if tool_name not in self._TOOL_HANDLERS:
            return tool_error(f"Unknown tool: {tool_name}")
        required, label, body, on_client_error = self._TOOL_HANDLERS[tool_name]
        if missing := next((k for k in required if not args.get(k, "")), None):
            return tool_error(f"Missing required parameter: {missing}")
        try:
            result = body(self, args)
        except Exception as e:
            client = _is_client_error(e)
            if client and on_client_error == "not_found":
                return tool_error(f"Memory not found: {args['memory_id']}")
            if not client or on_client_error == "count":
                self._record_failure()
            return tool_error(self._format_error(label, e))
        self._record_success()
        return result

    # //// Neoffice — bulk verbatim retain for an explicit user (end-of-day
    # consolidation). NORA's nightly consolidation extracts high-signal durable
    # facts from the day's chat and POSTs them to the gateway webhook
    # (event_type "memory_retain"). We store each one VERBATIM (infer=False).
    #
    # ⚠️ DO NOT switch this to infer=True hoping it will arbitrate contradicting
    # facts — it does NOT. Verified on mem0 OSS 2.0.10 (2026-07-29): the OSS
    # write path is purely ADDITIVE. `Memory._add_to_vector_store` imports
    # ADDITIVE_EXTRACTION_PROMPT and calls `_create_memory` only — it contains
    # ZERO calls to `_update_memory` / `_delete_memory`, and never uses
    # DEFAULT_UPDATE_MEMORY_PROMPT (that constant is dead code in OSS; the
    # ADD/UPDATE/DELETE reasoning lives in the paid Platform backend).
    # Measured live: pushing "code de chantier = RT111222" next to an existing
    # "= RT999888" left BOTH stored, even in identical canonical form.
    # infer=True therefore only costs one LLM call per fact and rewrites the
    # text in ENGLISH (mem0's extraction prompt), which fragments a French
    # store and degrades vector recall. Superseding contradicting facts has to
    # be done by US, in the consolidation pass. grep "//// Neoffice".
    def retain_facts(self, facts: List[str], *, scope: str = "user") -> int:
        """Consolidate pre-extracted facts into memory, scoped to self._user_id
        (or the shared company bucket for scope="company"). Returns count stored."""
        if self._backend is None or self._is_breaker_open():
            return 0
        write_user_id = (
            self._company_id if scope == "company" else self._user_id
        )
        stored = 0
        for fact in facts:
            text = (fact or "").strip()
            if not text:
                continue
            try:
                self._backend.add(
                    [{"role": "user", "content": text}],
                    user_id=write_user_id,
                    agent_id=self._agent_id,
                    infer=False,
                    metadata=_neoffice_write_metadata(self),
                )
                stored += 1
            except Exception as e:
                self._record_failure()
                logger.warning("retain_facts: store failed: %s", e)
        if stored:
            self._record_success()
        return stored
    # //// END Neoffice ////

    def _shutdown_backend(self):
        with suppress(Exception):
            if self._backend:
                # //// Neoffice — never close a shared OSS singleton from a
                # provider teardown: other agent runs still hold it. Just drop
                # the reference; _close_oss_singletons() (atexit) closes it.
                if not isinstance(self._backend, _LockedBackend):
                    self._backend.close()
                # //// END Neoffice ////
                self._backend = None

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        self._shutdown_backend()


def register(ctx) -> None:
    """Register Mem0 as a memory provider plugin."""
    ctx.register_memory_provider(Mem0MemoryProvider())


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

ADD_SCHEMA = {
    "name": "mem0_add",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM extraction). "
        "Call this the moment the user states a lasting preference, correction, "
        "decision, or personal detail worth recalling on future turns — don't "
        "wait to be asked to remember. Skip transient chit-chat and facts you've "
        "already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
        },
        "required": ["content"],
    },
}

DELETE_SCHEMA = {
    "name": "mem0_delete",
    "description": (
        "Delete a memory by its ID (take the ID from a mem0_search "
        "result). Use when a stored fact is obsolete or the user asks you to "
        "forget it; prefer mem0_update if the fact merely changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to delete."},
        },
        "required": ["memory_id"],
    },
}

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": (
        "Search the user's memories by meaning; returns facts ranked by "
        "relevance. Use this before answering any question that may depend on "
        "what you know about the user (preferences, facts, history, people, "
        "projects, past decisions). For multi-part or multi-hop questions, "
        "call it several times — vary the wording and run follow-up searches "
        "on what earlier results reveal; one search is rarely enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
            "rerank": {"type": "boolean", "description": "Rerank results for relevance (default: false, platform mode only)."},
        },
        "required": ["query"],
    },
}

UPDATE_SCHEMA = {
    "name": "mem0_update",
    "description": (
        "Replace the text of an existing memory by its ID (take the ID from a "
        "mem0_search result). Use when a stored fact has changed "
        "or was wrong — correct it in place instead of adding a duplicate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to update."},
            "text": {"type": "string", "description": "New text content."},
        },
        "required": ["memory_id", "text"],
    },
}
# ---- END PLUGIN-COMPAT ----
