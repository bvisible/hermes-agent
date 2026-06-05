"""Mem0 memory plugin — MemoryProvider interface.

Server-side LLM fact extraction, semantic search with reranking, and
automatic deduplication via the Mem0 Platform API.

Original PR #2933 by kartik-mem0, adapted to MemoryProvider ABC.

Config via environment variables:
  MEM0_API_KEY       — Mem0 Platform API key (required)
  MEM0_USER_ID       — User identifier (default: hermes-user)
  MEM0_AGENT_ID      — Agent identifier (default: hermes)

Or via $HERMES_HOME/mem0.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Circuit breaker: after this many consecutive failures, pause API calls
# for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from env vars, with $HERMES_HOME/mem0.json overrides.

    Environment variables provide defaults; mem0.json (if present) overrides
    individual keys.  This avoids a silent failure when the JSON file exists
    but is missing fields like ``api_key`` that the user set in ``.env``.
    """
    from hermes_constants import get_hermes_home

    config = {
        "api_key": os.environ.get("MEM0_API_KEY", ""),
        "user_id": os.environ.get("MEM0_USER_ID", "hermes-user"),
        "agent_id": os.environ.get("MEM0_AGENT_ID", "hermes"),
        "rerank": True,
        "keyword_search": False,
        # --- NORA self-hosted (library) mode: FAISS + Olares OpenAI-compat ---
        "mode": os.environ.get("MEM0_MODE", "cloud"),
        "olares_api_key_env": os.environ.get("MEM0_OLARES_KEY_ENV", "OLARES_API_KEY"),
        "llm_base_url": os.environ.get("MEM0_LLM_BASE_URL", ""),
        "llm_model": os.environ.get("MEM0_LLM_MODEL", ""),
        "embedder_base_url": os.environ.get("MEM0_EMBEDDER_BASE_URL", ""),
        "embedder_model": os.environ.get("MEM0_EMBEDDER_MODEL", ""),
        "embedding_dims": int(os.environ.get("MEM0_EMBEDDING_DIMS", "0") or 0),
        "faiss_path": os.environ.get("MEM0_FAISS_PATH", ""),
    }

    config_path = get_hermes_home() / "mem0.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    return config


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "mem0_profile",
    "description": (
        "Retrieve all stored memories about the user — preferences, facts, "
        "project context. Fast, no reranking. Use at conversation start."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": (
        "Search memories by meaning. Returns relevant facts ranked by similarity. "
        "Set rerank=true for higher accuracy on important queries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "rerank": {"type": "boolean", "description": "Enable reranking for precision (default: false)."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA = {
    "name": "mem0_conclude",
    "description": (
        "Store a durable fact about the user. Stored verbatim (no LLM extraction). "
        "Use for explicit preferences, corrections, or decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact to store."},
            "scope": {
                "type": "string",
                "enum": ["user", "company"],
                "description": (
                    "user = private to this user (default); "
                    "company = shared with everyone in the organisation."
                ),
            },
        },
        "required": ["conclusion"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class Mem0MemoryProvider(MemoryProvider):
    """Mem0 Platform memory with server-side extraction and semantic search."""

    def __init__(self):
        self._config = None
        self._client = None
        self._client_lock = threading.Lock()
        self._api_key = ""
        self._user_id = "hermes-user"
        self._agent_id = "hermes"
        self._rerank = True
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._sync_thread = None
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        cfg = _load_config()
        if cfg.get("mode") == "library":
            try:
                import mem0  # noqa: F401
            except ImportError:
                return False
            return bool(os.environ.get(cfg.get("olares_api_key_env", "OLARES_API_KEY")))
        return bool(cfg.get("api_key"))

    def save_config(self, values, hermes_home):
        """Write config to $HERMES_HOME/mem0.json."""
        import json
        from pathlib import Path
        config_path = Path(hermes_home) / "mem0.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        from utils import atomic_json_write
        atomic_json_write(config_path, existing, mode=0o600)

    def get_config_schema(self):
        return [
            {"key": "mode", "description": "cloud (Mem0 Platform) or library (self-hosted FAISS)", "default": "cloud", "choices": ["cloud", "library"]},
            {"key": "api_key", "description": "Mem0 Platform API key (cloud mode only)", "secret": True, "required": False, "env_var": "MEM0_API_KEY", "url": "https://app.mem0.ai"},
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
            {"key": "rerank", "description": "Enable reranking for recall", "default": "true", "choices": ["true", "false"]},
            {"key": "llm_base_url", "description": "(library) LLM OpenAI-compat base_url", "default": ""},
            {"key": "llm_model", "description": "(library) LLM model id", "default": ""},
            {"key": "embedder_base_url", "description": "(library) embeddings OpenAI-compat base_url", "default": ""},
            {"key": "embedder_model", "description": "(library) embedding model id", "default": ""},
            {"key": "embedding_dims", "description": "(library) embedding vector dimension", "default": ""},
            {"key": "faiss_path", "description": "(library) FAISS index path", "default": ""},
            {"key": "olares_api_key_env", "description": "(library) env var holding the LLM/embedder key", "default": "OLARES_API_KEY"},
        ]

    def _get_client(self):
        """Thread-safe client accessor with lazy initialization."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            if (self._config or {}).get("mode") == "library":
                self._client = self._build_local_memory()
                return self._client
            try:
                from mem0 import MemoryClient
                self._client = MemoryClient(api_key=self._api_key)
                return self._client
            except ImportError:
                raise RuntimeError("mem0 package not installed. Run: pip install mem0ai")

    def _build_local_memory(self):
        """Self-hosted mem0 Memory: FAISS index + Olares OpenAI-compatible LLM/embedder.

        Keeps all user memory on-instance — never hits api.mem0.ai or api.openai.com.
        The mem0 openai LLM/embedder honour `openai_base_url`, so pointing them at
        Olares is enough to stay sovereign (verify no OPENROUTER_API_KEY in env, which
        mem0's OpenAI client would otherwise prefer).
        """
        try:
            from mem0 import Memory
        except ImportError:
            raise RuntimeError("mem0 package not installed. Run: pip install mem0ai faiss-cpu")
        cfg = self._config or {}
        key_env = cfg.get("olares_api_key_env", "OLARES_API_KEY")
        olares_key = os.environ.get(key_env, "")
        if not olares_key:
            raise RuntimeError(f"mem0 library mode: env var {key_env} is empty")
        faiss_path = cfg.get("faiss_path")
        if not faiss_path:
            from hermes_constants import get_hermes_home
            faiss_path = str(get_hermes_home() / "memory" / "faiss")
        parent = os.path.dirname(faiss_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        vs_config = {
            "collection_name": "nora_mem0",
            "path": faiss_path,
            "distance_strategy": "cosine",
            "normalize_L2": True,
        }
        dims = int(cfg.get("embedding_dims") or 0)
        if dims:
            vs_config["embedding_model_dims"] = dims
        mem0_config = {
            "vector_store": {"provider": "faiss", "config": vs_config},
            "llm": {
                "provider": "openai",
                "config": {
                    "model": cfg.get("llm_model", ""),
                    "openai_base_url": cfg.get("llm_base_url", ""),
                    "api_key": olares_key,
                    "temperature": 0.1,
                    "max_tokens": 2000,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": cfg.get("embedder_model", ""),
                    "openai_base_url": cfg.get("embedder_base_url", ""),
                    "api_key": olares_key,
                },
            },
        }
        from mem0 import Memory as _M
        return _M.from_config(mem0_config)

    def _is_breaker_open(self) -> bool:
        """Return True if the circuit breaker is tripped (too many failures)."""
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            # Cooldown expired — reset and allow a retry
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "Mem0 circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.",
                self._consecutive_failures, _BREAKER_COOLDOWN_SECS,
            )

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        self._api_key = self._config.get("api_key", "")
        # Prefer gateway-provided user_id for per-user memory scoping;
        # fall back to config/env default for CLI (single-user) sessions.
        self._user_id = kwargs.get("user_id") or self._config.get("user_id", "hermes-user")
        self._agent_id = self._config.get("agent_id", "hermes")
        # //// NORA CORE PATCH — shared company-scope memory (re-ported from the dropped
        # apply_mem0_scoping_patch). A `scope="company"` fact lands in ONE shared bucket
        # (user_id=company_id) so EVERY user of the instance sees it; personal facts stay
        # in the per-user bucket and reads merge both. The gateway passes company_id per
        # call; default "company" = one shared bucket per instance. grep "NORA CORE PATCH".
        self._company_id = kwargs.get("company_id") or self._config.get("company_id", "company")
        # //// END NORA CORE PATCH ////
        self._rerank = self._config.get("rerank", True)

    def _read_filters(self) -> Dict[str, Any]:
        """Filters for search/get_all — scoped to user only for cross-session recall."""
        return {"user_id": self._user_id}

    def _write_filters(self) -> Dict[str, Any]:
        """Filters for add — scoped to user + agent for attribution."""
        return {"user_id": self._user_id, "agent_id": self._agent_id}

    # //// NORA CORE PATCH — company-scope memory (shared bucket across the instance's users) ////
    def _company_write_filters(self) -> Dict[str, Any]:
        """Filters for writing a company-scoped (shared) fact — lands in the company bucket."""
        return {"user_id": self._company_id, "agent_id": self._agent_id}

    def _scoped_read_buckets(self) -> List[Dict[str, Any]]:
        """Buckets a read covers: the caller's per-user bucket + the shared company bucket.
        Personal facts stay private; company facts are visible to every user of the instance."""
        buckets = [self._read_filters()]
        if self._company_id and self._company_id != self._user_id:
            buckets.append({"user_id": self._company_id})
        return buckets

    def _merged_read(self, fetch) -> list:
        """Run fetch(filters) over each scoped bucket and merge, deduped by memory id."""
        seen, out = set(), []
        for filt in self._scoped_read_buckets():
            for item in self._unwrap_results(fetch(filt)):
                key = item.get("id") or item.get("memory")
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
        return out
    # //// END NORA CORE PATCH ////

    @staticmethod
    def _unwrap_results(response: Any) -> list:
        """Normalize Mem0 API response — v2 wraps results in {"results": [...]}."""
        if isinstance(response, dict):
            return response.get("results", [])
        if isinstance(response, list):
            return response
        return []

    def system_prompt_block(self) -> str:
        return (
            "# Mem0 Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Use mem0_search to find memories, mem0_conclude to store facts, "
            "mem0_profile for a full overview."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Mem0 Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return

        def _run():
            try:
                client = self._get_client()
                # NORA CORE PATCH — prefetch covers the per-user + shared company buckets
                results = self._merged_read(
                    lambda f: client.search(query=query, filters=f, rerank=self._rerank, top_k=5)
                )
                if results:
                    lines = [r.get("memory", "") for r in results if r.get("memory")]
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="mem0-prefetch")
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Send the turn to Mem0 for server-side fact extraction (non-blocking)."""
        if self._is_breaker_open():
            return

        def _sync():
            try:
                client = self._get_client()
                messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ]
                client.add(messages, **self._write_filters())
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Mem0 sync failed: %s", e)

        # Wait for any previous sync before starting a new one
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="mem0-sync")
        self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA, CONCLUDE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({
                "error": "Mem0 API temporarily unavailable (multiple consecutive failures). Will retry automatically."
            })

        try:
            client = self._get_client()
        except Exception as e:
            return tool_error(str(e))

        if tool_name == "mem0_profile":
            try:
                # NORA CORE PATCH — merge the per-user bucket + the shared company bucket
                memories = self._merged_read(lambda f: client.get_all(filters=f))
                self._record_success()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                lines = [m.get("memory", "") for m in memories if m.get("memory")]
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Failed to fetch profile: {e}")

        elif tool_name == "mem0_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            rerank = args.get("rerank", False)
            top_k = min(int(args.get("top_k", 10)), 50)
            try:
                # NORA CORE PATCH — search the per-user bucket + the shared company bucket, merge
                results = self._merged_read(
                    lambda f: client.search(query=query, filters=f, rerank=rerank, top_k=top_k)
                )
                results.sort(key=lambda r: r.get("score", 0) or 0, reverse=True)
                results = results[:top_k]
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"memory": r.get("memory", ""), "score": r.get("score", 0)} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Search failed: {e}")

        elif tool_name == "mem0_conclude":
            conclusion = args.get("conclusion", "")
            if not conclusion:
                return tool_error("Missing required parameter: conclusion")
            # NORA CORE PATCH — scope="company" stores into the shared company bucket
            scope = str(args.get("scope") or "user").lower()
            write_filters = self._company_write_filters() if scope == "company" else self._write_filters()
            try:
                client.add(
                    [{"role": "user", "content": conclusion}],
                    **write_filters,
                    infer=False,
                )
                self._record_success()
                return json.dumps({"result": "Fact stored."})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Failed to store: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        with self._client_lock:
            self._client = None


def register(ctx) -> None:
    """Register Mem0 as a memory provider plugin."""
    ctx.register_memory_provider(Mem0MemoryProvider())
