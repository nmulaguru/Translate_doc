"""The `mcp` module the sandbox sees.

Exposes the five MCP tools from Sample_FastMCP.py and wraps each call
in ##MCP_CALL## / ##MCP_RESULT## stdout markers so the parent code worker can
surface every MCP tool call as a `task.mcp_call` / `task.mcp_result` SSE event.
Without these markers the user can only see the aggregated `##PROGRESS##` lines
the generated script emits — the individual tool calls inside the sandbox are
invisible. The cost is a few extra stdout lines per call (~80 bytes each).
"""

from __future__ import annotations

import itertools
import json
import sys
from typing import Any, Optional

from app.mcp_client.client import (
    aiagent as _aiagent,
    get_active_documents_metadata as _meta,
    get_document_insights as _insights,
    search_documents as _search,
    translate_document_preserving_structure as _translate,
)

_call_counter = itertools.count(1)


def _emit_call(tool: str, summary: dict) -> int:
    cid = next(_call_counter)
    sys.stdout.write(
        "##MCP_CALL## " + json.dumps({"id": cid, "tool": tool, "summary": summary}) + "\n"
    )
    sys.stdout.flush()
    return cid


def _emit_result(call_id: int, tool: str, ok: bool, summary: dict) -> None:
    sys.stdout.write(
        "##MCP_RESULT## "
        + json.dumps({"id": call_id, "tool": tool, "ok": ok, "summary": summary})
        + "\n"
    )
    sys.stdout.flush()


def _summarize_result(tool: str, result: Any) -> dict:
    """Tiny projection — we never let raw doc lists into the SSE stream."""
    if not isinstance(result, dict):
        return {"_type": type(result).__name__}
    if tool == "translate_document_preserving_structure":
        return {
            "mode": result.get("mode"),
            "total": result.get("total"),
            "successful": result.get("successful"),
            "failed": result.get("failed"),
        }
    if tool == "get_active_documents_metadata":
        return {"total_documents": result.get("total_documents")}
    if tool == "get_document_insights":
        ins = result.get("insights") or {}
        return {
            "total_documents": result.get("total_documents"),
            "doc_count_in_insights": len(ins) if isinstance(ins, dict) else None,
        }
    return {"keys": list(result.keys())[:6]}


async def get_active_documents_metadata(
    container_id: str,
    page_size: Optional[int] = None,
    page: Optional[int] = None,
    language: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    cid = _emit_call("get_active_documents_metadata", {
        "container_id": container_id,
        "page_size": page_size,
        "page": page,
        "language": language,
        "category": category,
        "status": status,
    })
    try:
        result = await _meta(container_id, page_size=page_size, page=page,
                             language=language, category=category, status=status)
    except Exception as e:
        _emit_result(cid, "get_active_documents_metadata", False,
                     {"error": f"{type(e).__name__}: {e}"})
        raise
    _emit_result(cid, "get_active_documents_metadata", True,
                 _summarize_result("get_active_documents_metadata", result))
    return result


async def get_document_insights(
    container_id: str,
    model: Optional[str] = None,
    page_size: Optional[int] = None,
    page: Optional[int] = None,
) -> dict[str, Any]:
    cid = _emit_call("get_document_insights", {
        "container_id": container_id,
        "model": model,
        "page_size": page_size,
        "page": page,
    })
    try:
        result = await _insights(container_id, model=model, page_size=page_size, page=page)
    except Exception as e:
        _emit_result(cid, "get_document_insights", False,
                     {"error": f"{type(e).__name__}: {e}"})
        raise
    _emit_result(cid, "get_document_insights", True,
                 _summarize_result("get_document_insights", result))
    return result


async def search_documents(
    query: str,
    container_id: Optional[str] = None,
    top_k: int = 20,
    offset: int = 0,
    exhaustive: bool = False,
) -> dict[str, Any]:
    cid = _emit_call("search_documents", {
        "query": query[:120],
        "container_id": container_id,
        "top_k": top_k,
        "exhaustive": exhaustive,
    })
    try:
        result = await _search(query, container_id=container_id, top_k=top_k,
                               offset=offset, exhaustive=exhaustive)
    except Exception as e:
        _emit_result(cid, "search_documents", False, {"error": f"{type(e).__name__}: {e}"})
        raise
    summary = {"total_matched": result.get("total_matched") if isinstance(result, dict) else None}
    _emit_result(cid, "search_documents", True, summary)
    return result


async def translate_document_preserving_structure(
    document_id: str | list[str],
    destination_lang: str | None = None,
    container_id: str | None = None,
    destinationLanguageThreeLetterCode: str | None = None,
) -> dict[str, Any]:
    lang = destination_lang or destinationLanguageThreeLetterCode
    if not lang:
        raise ValueError("destination language is required")
    if not container_id:
        raise ValueError("container_id is required")

    cid = _emit_call(
        "translate_document_preserving_structure",
        {
            "container_id": container_id,
            "lang": lang,
            "doc_count": len(document_id) if isinstance(document_id, list) else 1,
            "mode": "bulk" if isinstance(document_id, list) else "single",
        },
    )
    try:
        result = await _translate(document_id, lang, container_id)
    except Exception as e:
        _emit_result(cid, "translate_document_preserving_structure", False,
                     {"error": f"{type(e).__name__}: {e}"})
        raise
    _emit_result(cid, "translate_document_preserving_structure", True,
                 _summarize_result("translate_document_preserving_structure", result))
    return result


async def aiagent(prompt: str, container_id: str) -> str:
    cid = _emit_call("aiagent", {"container_id": container_id, "prompt_preview": prompt[:120]})
    try:
        result = await _aiagent(prompt, container_id)
    except Exception as e:
        _emit_result(cid, "aiagent", False, {"error": f"{type(e).__name__}: {e}"})
        raise
    summary = {"length": len(result) if isinstance(result, str) else None}
    _emit_result(cid, "aiagent", True, summary)
    return result
