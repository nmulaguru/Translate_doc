from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from loguru import logger
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.config import settings


class MCPError(Exception):
    def __init__(self, message: str, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable


class CircuitBreaker:
    """Per-tool simple breaker. 5 failures within 60s opens for 30s."""

    def __init__(self, fail_threshold: int = 5, window_s: float = 60.0, open_s: float = 30.0):
        self.fail_threshold = fail_threshold
        self.window_s = window_s
        self.open_s = open_s
        self._failures: dict[str, list[float]] = {}
        self._opened_at: dict[str, float] = {}

    def allow(self, tool: str) -> bool:
        opened = self._opened_at.get(tool)
        if opened is not None:
            if time.monotonic() - opened < self.open_s:
                return False
            self._opened_at.pop(tool, None)
            self._failures.pop(tool, None)
        return True

    def record_success(self, tool: str) -> None:
        self._failures.pop(tool, None)

    def record_failure(self, tool: str) -> bool:
        now = time.monotonic()
        bucket = self._failures.setdefault(tool, [])
        bucket.append(now)
        cutoff = now - self.window_s
        self._failures[tool] = [t for t in bucket if t >= cutoff]
        if len(self._failures[tool]) >= self.fail_threshold:
            self._opened_at[tool] = now
            return True
        return False


class MCPClient:
    """Async wrapper over the MCP streamable-HTTP client.

    Handles connection lifecycle, retry with exponential backoff, and a per-tool
    circuit breaker. Each tool invocation re-establishes a fresh session because
    the streamable-HTTP transport is request-scoped — there is no long-lived
    session that survives across calls without server support, and trying to
    pool sessions across asyncio tasks deadlocks on the underlying httpx pool.
    For demo throughput this is fine; for production you'd add a session pool.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        max_retries: int = 3,
        breaker: Optional[CircuitBreaker] = None,
    ):
        self.url = url or settings.mcp_url
        self.max_retries = max_retries
        self.breaker = breaker or CircuitBreaker()
        self._tools_cache: Optional[list[dict[str, Any]]] = None

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover available tools from the MCP server. Cached after first call."""
        if self._tools_cache is not None:
            return self._tools_cache
        async with streamablehttp_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                self._tools_cache = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema,
                    }
                    for t in result.tools
                ]
        return self._tools_cache

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if not self.breaker.allow(name):
            raise MCPError(f"circuit_open for tool {name}", retriable=False)

        attempt = 0
        last_err: Optional[Exception] = None
        while attempt <= self.max_retries:
            try:
                return await self._invoke(name, args)
            except MCPError as e:
                if not e.retriable:
                    self.breaker.record_failure(name)
                    raise
                last_err = e
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(f"[MCP] {name} attempt {attempt} failed: {e}")

            attempt += 1
            if attempt > self.max_retries:
                break
            await asyncio.sleep(min(2 ** (attempt - 1), 8))

        self.breaker.record_failure(name)
        raise MCPError(
            f"tool {name} failed after {self.max_retries} retries: {last_err}",
            retriable=False,
        )

    async def _invoke(self, name: str, args: dict[str, Any]) -> Any:
        try:
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, args)
                    if getattr(result, "isError", False):
                        text = self._extract_text(result)
                        raise MCPError(f"tool error: {text}", retriable=False)
                    self.breaker.record_success(name)
                    return self._unwrap(result)
        except MCPError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MCPError(str(e), retriable=True) from e

    @staticmethod
    def _extract_text(result: Any) -> str:
        parts = []
        for c in getattr(result, "content", []) or []:
            if getattr(c, "type", None) == "text":
                parts.append(getattr(c, "text", ""))
        return "\n".join(parts) if parts else str(result)

    @staticmethod
    def _unwrap(result: Any) -> Any:
        """MCP tool results come back as content blocks. Our tools return JSON
        strings as text content, so we attempt to JSON-decode the first text
        block. Falls back to the raw string if decoding fails."""
        for c in getattr(result, "content", []) or []:
            if getattr(c, "type", None) == "text":
                text = getattr(c, "text", "")
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        return None


_client: Optional[MCPClient] = None


def get_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


# ── Typed convenience wrappers ───────────────────────────────────────────────
async def list_tools() -> list[dict[str, Any]]:
    return await get_client().list_tools()


async def get_active_documents_metadata(
    container_id: str,
    page_size: Optional[int] = None,
    page: Optional[int] = None,
    language: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"container_id": container_id}
    if page_size is not None:
        args["page_size"] = page_size
    if page is not None:
        args["page"] = page
    if language is not None:
        args["language"] = language
    if category is not None:
        args["category"] = category
    if status is not None:
        args["status"] = status
    return await get_client().call_tool("get_active_documents_metadata", args)


async def get_document_insights(
    container_id: str,
    model: Optional[str] = None,
    page_size: Optional[int] = None,
    page: Optional[int] = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"container_id": container_id}
    if model is not None:
        args["model"] = model
    if page_size is not None:
        args["page_size"] = page_size
    if page is not None:
        args["page"] = page
    return await get_client().call_tool("get_document_insights", args)


async def translate_document_preserving_structure(
    document_id: str | list[str],
    destination_lang: str,
    container_id: str,
) -> dict[str, Any]:
    return await get_client().call_tool(
        "translate_document_preserving_structure",
        {
            "document_id": document_id,
            "destinationLanguageThreeLetterCode": destination_lang,
            "container_id": container_id,
        },
    )


async def aiagent(prompt: str, container_id: str) -> str:
    result = await get_client().call_tool(
        "aiagent", {"prompt": prompt, "container_id": container_id}
    )
    if isinstance(result, str):
        return result
    return json.dumps(result)


async def search_documents(
    query: str,
    container_id: Optional[str] = None,
    top_k: int = 20,
    offset: int = 0,
    exhaustive: bool = False,
) -> dict[str, Any]:
    args: dict[str, Any] = {"query": query, "top_k": top_k, "offset": offset, "exhaustive": exhaustive}
    if container_id is not None:
        args["container_id"] = container_id
    return await get_client().call_tool("search_documents", args)


@asynccontextmanager
async def mcp_session() -> AsyncIterator[MCPClient]:
    yield get_client()
