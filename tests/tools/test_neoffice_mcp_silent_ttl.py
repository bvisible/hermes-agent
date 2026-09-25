# //// Neoffice — added file (no upstream equivalent): a server silent on its TTL is cacheable.
"""The MCP SDK's ListToolsResult defaults ttl_ms to 0 when a server says nothing about caching.
Hermes read that 0 as the server's hint, so every schema-cache entry expired as it was written and
no `lazy: true` server ever started lazily (osiris, 2026-09-25). Only a hint the server SENT counts."""
import asyncio

import pytest

mcp_types = pytest.importorskip("mcp.types")

from tools.mcp_tool import _paginate_full_list  # noqa: E402


def _drain(payload: dict) -> dict:
    result = mcp_types.ListToolsResult.model_validate(payload)

    async def list_method(**_kw):
        return result

    meta: dict = {}
    asyncio.run(_paginate_full_list(list_method, "tools", "wiki", cache_meta_out=meta))
    return meta


def test_a_server_silent_on_its_ttl_records_none():
    assert "ttl_ms" not in _drain({"tools": []})


def test_a_ttl_the_server_sent_is_kept():
    assert _drain({"tools": [], "ttlMs": 60000})["ttl_ms"] == 60000


def test_an_explicit_zero_is_still_honoured():
    """A server that says 0 opts out of caching; only the SDK's silent default is ignored."""
    assert _drain({"tools": [], "ttlMs": 0})["ttl_ms"] == 0
