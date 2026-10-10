from __future__ import annotations

import json
import time
from typing import Any

from app.config import get_settings
from app.logging import get_logger
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider, domain_of

logger = get_logger("search.providers.exa_mcp")

_EXA_MCP_ENDPOINT = "https://mcp.exa.ai/mcp"
_PROTOCOL_VERSION = "2024-11-05"

# The hosted server exposes several tools; web_search_exa is the plain search
# one and is available anonymously. agent_run needs OAuth or a key.
_SEARCH_TOOL = "web_search_exa"
_ADVANCED_TOOL = "web_search_advanced_exa"


class ExaMCPProvider(SearchProvider):
    """Exa via its hosted MCP server rather than the REST API.

    Exa's REST endpoint returns 402 x402 payment-required without a funded key,
    so the REST provider is unusable on a free install. The hosted MCP server at
    mcp.exa.ai answers anonymously under a rate limit, which makes it the only
    free route to Exa's neural index.

    This speaks just enough of the streamable-HTTP MCP protocol — initialize,
    notifications/initialized, tools/call — to issue one search per request,
    rather than depending on the MCP SDK inside the gateway.
    """

    name = "exa"

    def __init__(self, timeout: float = 25.0, *, advanced: bool = False, api_key: str = "") -> None:
        self.settings = get_settings()
        self.timeout = timeout
        self.advanced = advanced
        self.api_key = api_key

    def _headers(self, session_id: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    @staticmethod
    def _payload(resp: Any) -> dict:
        """Unwrap an MCP response, which may be JSON or a single SSE event."""
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return {}
        return resp.json()

    async def search(self, query: ProviderQuery) -> ProviderResult:
        import httpx

        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        tool = _ADVANCED_TOOL if self.advanced else _SEARCH_TOOL

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                init = await client.post(
                    _EXA_MCP_ENDPOINT,
                    headers=self._headers(None),
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "searchos", "version": "1.0.0"},
                        },
                    },
                )
                init.raise_for_status()
                session_id = init.headers.get("mcp-session-id") or self._payload(init).get("sessionId")

                await client.post(
                    _EXA_MCP_ENDPOINT,
                    headers=self._headers(session_id),
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )

                called = await client.post(
                    _EXA_MCP_ENDPOINT,
                    headers=self._headers(session_id),
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": self._arguments(query)},
                    },
                )
                called.raise_for_status()
                payload = self._payload(called)
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="http").inc()
            result.error = f"exa-mcp error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        parsed = self._extract(payload)
        if parsed is None:
            result.error = "exa-mcp: could not read tool result"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, item in enumerate(parsed[: query.num_results], start=1):
            url = item.get("url") or ""
            if not url:
                continue
            snippet = (item.get("text") or item.get("snippet") or item.get("description") or "").strip()
            result.results.append(
                SearchResult(
                    title=item.get("title") or url,
                    url=url,
                    snippet=snippet[:1200],
                    source=self.name,
                    rank=rank,
                    domain=domain_of(url),
                    score=self._score(item, rank),
                    published=item.get("publishedDate"),
                )
            )
        result.latency_ms = (time.monotonic() - start) * 1000
        return result

    def _arguments(self, query: ProviderQuery) -> dict[str, Any]:
        args: dict[str, Any] = {"query": query.q, "numResults": max(query.num_results, 10)}
        if query.domains:
            args["includeDomains"] = query.domains[:10]
        if query.exclude_domains:
            args["excludeDomains"] = query.exclude_domains[:10]
        return args

    @staticmethod
    def _extract(payload: dict) -> list[dict] | None:
        """Pull the result list out of a tools/call response.

        The hosted server answers with a plain-text block, not JSON:

            Title: ...
            URL: ...
            Published: ...
            Author: N/A
            Highlights:
            ...

            ---

            Title: ...

        Records are split on the ``Title:`` line, which is the only field
        guaranteed once per result. A JSON payload is still accepted, since the
        server may switch formats and an API-key tier behaves differently.
        """
        result = payload.get("result")
        if not isinstance(result, dict):
            return None

        structured = result.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("results"), list):
            return structured["results"]

        content = result.get("content") or []
        if not content:
            return None
        block = content[0] if isinstance(content[0], dict) else {}
        if isinstance(block.get("structuredContent"), dict):
            inner = block["structuredContent"].get("results")
            if isinstance(inner, list):
                return inner

        text = block.get("text")
        if not text:
            return None

        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            return ExaMCPProvider._parse_text(text)
        if isinstance(decoded, list):
            return [x for x in decoded if isinstance(x, dict)]
        if isinstance(decoded, dict):
            for key in ("results", "data", "items"):
                if isinstance(decoded.get(key), list):
                    return decoded[key]
        return None

    @staticmethod
    def _parse_text(text: str) -> list[dict]:
        records: list[dict] = []
        current: dict | None = None
        field: str | None = None

        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip() or line.strip() == "---":
                continue
            if line.startswith("Title:"):
                if current and current.get("url"):
                    records.append(current)
                current = {"title": line[len("Title:") :].strip()}
                field = "title"
                continue
            if current is None:
                continue
            if line.startswith("URL:"):
                current["url"] = line[len("URL:") :].strip()
                field = "url"
            elif line.startswith("Published:"):
                current["publishedDate"] = line[len("Published:") :].strip()
                field = "publishedDate"
            elif line.startswith("Author:"):
                field = None
            elif line.startswith("Highlights:"):
                current["text"] = ""
                field = "text"
            elif field == "text":
                current["text"] = (current.get("text", "") + "\n" + line).strip()

        if current and current.get("url"):
            records.append(current)
        return records

    @staticmethod
    def _score(item: dict, rank: int) -> float:
        """Exa returns no score; keep its order meaningful without inventing one."""
        raw = item.get("score")
        if isinstance(raw, (int, float)):
            return float(raw)
        return round(1.0 - (rank - 1) * 0.01, 4)
