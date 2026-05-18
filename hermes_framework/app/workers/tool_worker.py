from __future__ import annotations

from typing import Any

from app.mcp_client.client import get_client
from app.models import Task, TaskKind
from app.workers.base import Worker, WorkerContext

# Only these MCP tools accept the singular `container_id` arg. Auto-injecting
# it for any other tool (e.g. `query_corpus`, which takes `container_ids`
# plural) produces a confusing FastMCP "unknown arg" error and a misleading
# "unhandled errors in a TaskGroup" surface. Keep this list in sync with the
# MCP tool signatures in Sample_FastMCP.py.
_TOOLS_WITH_CONTAINER_ID: frozenset[str] = frozenset({
    "get_active_documents_metadata",
    "get_document_insights",
    "translate_document_preserving_structure",
    "aiagent",
})


def _preview(result: Any, limit: int = 400) -> Any:
    if result is None:
        return None
    if isinstance(result, str):
        return result[:limit]
    if isinstance(result, dict):
        keys = list(result.keys())
        if "documents" in result and isinstance(result["documents"], list):
            return {
                "container_id": result.get("container_id"),
                "total_documents": result.get("total_documents", len(result["documents"])),
                "first_3": result["documents"][:3],
            }
        if "insights" in result and isinstance(result["insights"], dict):
            keys = list(result["insights"].keys())
            return {
                "container_id": result.get("container_id"),
                "doc_count": len(keys),
                "first_doc": keys[0] if keys else None,
            }
        return {k: result[k] for k in keys[:6]}
    if isinstance(result, list):
        return result[:3]
    return result


class ToolWorker(Worker):
    """Executes one MCP tool call. Used for TOOL_CALL and RAG_QUERY tasks."""

    name = "tool"

    async def execute(self, ctx: WorkerContext, task: Task) -> Any:
        client = get_client()
        if task.kind == TaskKind.RAG_QUERY:
            prompt = task.spec.get("prompt") or ctx.plan.goal
            container_id = task.spec.get("container_id") or ctx.container_id
            await ctx.bus.emit(
                ctx.session_id,
                "task.tool_call",
                {"task_id": task.id, "tool": "aiagent", "args": {"prompt": prompt[:200]}},
            )
            result = await client.call_tool(
                "aiagent", {"prompt": prompt, "container_id": container_id}
            )
            await ctx.bus.emit(
                ctx.session_id,
                "task.tool_result",
                {"task_id": task.id, "tool": "aiagent", "result_preview": _preview(result)},
            )
            return result

        # Generic TOOL_CALL
        tool = task.spec.get("tool")
        args = task.spec.get("args", {}) or {}
        # Only auto-inject container_id for tools that accept it. query_corpus
        # uses container_ids (plural list) and would reject a stray
        # container_id kwarg.
        if (
            tool in _TOOLS_WITH_CONTAINER_ID
            and ctx.container_id
            and "container_id" not in args
        ):
            args["container_id"] = ctx.container_id
        if not tool:
            raise ValueError(f"TOOL_CALL task {task.id} missing spec.tool")

        await ctx.bus.emit(
            ctx.session_id,
            "task.tool_call",
            {"task_id": task.id, "tool": tool, "args": args},
        )
        result = await client.call_tool(tool, args)
        await ctx.bus.emit(
            ctx.session_id,
            "task.tool_result",
            {"task_id": task.id, "tool": tool, "result_preview": _preview(result)},
        )
        return result
