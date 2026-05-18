"""Planner — turns a user request into a typed DAG plan.

This module owns *everything* about producing and sanity-checking a plan:
    1. Call Claude with the planner system prompt + emit_plan tool.
    2. Coerce the LLM's JSON into typed Task objects.
    3. Auto-repair the one rule the planner gets wrong ~5% of the time
       (TOOL_CALL with too many doc IDs → CODE_TRANSFORM), and reject
       structural errors (cycles, dangling deps, duplicate IDs).

`plan_utils.py`, `validator.py`, and the SQL-agent files were folded in or
deleted on 2026-05-16 — the SQL path duplicated MCP and the validator was
40 lines of dead-simple logic that didn't earn its own file.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger

from app.api.sse import EventBus
from app.config import settings
from app.engine.prompts import EMIT_PLAN_TOOL, PLANNER_SYSTEM
from app.llm.anthropic_client import get_async_client
from app.models import Plan, Task, TaskKind, TaskStatus


# ── Task construction helpers ────────────────────────────────────────────────

def default_timeout(kind: TaskKind) -> int:
    if kind in (TaskKind.CODE_TRANSFORM, TaskKind.SUBAGENT, TaskKind.BULK_TOOL_CALL):
        return 600
    return 120


def coerce_task(raw: dict[str, Any]) -> Task:
    kind = TaskKind(raw["kind"])
    return Task(
        id=raw["id"],
        kind=kind,
        title=raw["title"],
        depends_on=raw.get("depends_on", []) or [],
        spec=raw.get("spec", {}) or {},
        timeout_s=int(raw.get("timeout_s") or default_timeout(kind)),
        max_retries=int(raw.get("max_retries") or 2),
        status=TaskStatus.PENDING,
    )


# ── Plan validation + auto-repair ────────────────────────────────────────────

def _count_doc_ids(args: dict[str, Any]) -> int:
    """Find the biggest list-of-strings in args — proxy for the doc-ID list
    size when the planner emits a TOOL_CALL it really shouldn't have."""
    for key in ("document_id", "document_ids", "doc_ids", "documentIds"):
        v = args.get(key)
        if isinstance(v, list):
            return len(v)
    biggest = 0
    for v in args.values():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            biggest = max(biggest, len(v))
    return biggest


def _rewrite_as_code(task: Task) -> Task:
    args = task.spec.get("args", {}) or {}
    tool = task.spec.get("tool") or "(unspecified)"
    return Task(
        id=task.id,
        kind=TaskKind.CODE_TRANSFORM,
        title=task.title + " (auto-repaired: TOOL_CALL with too many doc IDs)",
        depends_on=task.depends_on,
        spec={
            "code_intent": (
                f"Originally a direct TOOL_CALL of `{tool}` over many doc IDs. Args were: {args}. "
                f"Iterate the doc IDs in chunks of 200, calling `mcp.{tool}` per chunk (bulk mode). "
                f"Emit ##PROGRESS## per chunk. Aggregate successful/failed counts and return "
                f"{{'successful': int, 'failed': int, 'failed_documents': list[str]}}."
            ),
            "expected_output_schema": {
                "successful": "int",
                "failed": "int",
                "failed_documents": "list[str]",
            },
        },
        timeout_s=max(task.timeout_s, 600),
        max_retries=task.max_retries,
    )


def _has_cycle(tasks: list[Task]) -> bool:
    graph = {t.id: set(t.depends_on) for t in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dep in graph.get(node, ()):
            if dfs(dep):
                return True
        visiting.discard(node)
        visited.add(node)
        return False

    return any(dfs(n) for n in graph)


def validate_and_repair(plan: Plan) -> tuple[Plan, list[dict[str, Any]]]:
    """Auto-rewrite >20-doc TOOL_CALLs and reject structural errors.

    Returns (plan, repairs). `repairs` is the list to emit as plan.repaired
    SSE events.
    """
    repairs: list[dict[str, Any]] = []
    threshold = settings.bulk_doc_threshold

    new_tasks: list[Task] = []
    for t in plan.tasks:
        if t.kind == TaskKind.TOOL_CALL:
            count = _count_doc_ids(t.spec.get("args", {}) or {})
            if count > threshold:
                rewritten = _rewrite_as_code(t)
                repairs.append({
                    "reason": f"TOOL_CALL with {count} doc IDs exceeds threshold {threshold}",
                    "task_id": t.id,
                    "before_kind": t.kind.value,
                    "after_kind": rewritten.kind.value,
                })
                new_tasks.append(rewritten)
                continue
        new_tasks.append(t)
    plan.tasks = new_tasks

    ids = {t.id for t in plan.tasks}
    if len(ids) != len(plan.tasks):
        raise ValueError("plan has duplicate task IDs")
    for t in plan.tasks:
        for dep in t.depends_on:
            if dep not in ids:
                raise ValueError(f"task {t.id} depends on unknown task {dep}")
    if _has_cycle(plan.tasks):
        raise ValueError("plan contains a dependency cycle")
    return plan, repairs


# ── Planner (the actual LLM call) ────────────────────────────────────────────

class Planner:
    """Generates a structured DAG plan via Claude with adaptive thinking.

    Streams thinking deltas back as planner.thinking SSE events; extracts the
    emit_plan tool_use from the final message. System prompt + tool schema
    are prompt-cached so repeated calls amortise the write.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.client = get_async_client()

    async def plan(
        self,
        session_id: str,
        user_msg: str,
        container_id: Optional[str],
        clarifications: Optional[list[dict[str, str]]] = None,
        prior_failure: Optional[dict[str, Any]] = None,
        available_containers: Optional[list[str]] = None,
        live_tools: Optional[list[dict[str, Any]]] = None,
    ) -> Plan:
        user_block = self._build_user_block(
            user_msg, container_id, clarifications, prior_failure, available_containers, live_tools
        )

        # `thinking=adaptive` requires tool_choice="auto" (forced tool use is
        # incompatible with adaptive thinking). The system prompt + only-one-
        # tool surface keep the model deterministic in practice.
        async with self.client.messages.stream(
            model=settings.planner_model,
            max_tokens=16000,
            thinking={"type": settings.planner_thinking},
            output_config={"effort": settings.planner_effort},
            cache_control={"type": "ephemeral"},
            system=PLANNER_SYSTEM,
            tools=[EMIT_PLAN_TOOL],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": user_block}],
        ) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "thinking_delta":
                        text = getattr(delta, "thinking", "") or ""
                        if text:
                            await self.bus.emit(session_id, "planner.thinking", {"delta": text})
                    elif dtype == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            await self.bus.emit(session_id, "planner.text", {"delta": text})
            final_message = await stream.get_final_message()

        plan_payload: Optional[dict[str, Any]] = None
        for block in final_message.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "emit_plan":
                plan_payload = block.input  # type: ignore[assignment]
                break
        if plan_payload is None:
            raise RuntimeError("Planner did not emit a plan via the emit_plan tool")

        tasks = [coerce_task(t) for t in plan_payload.get("tasks", [])]
        plan = Plan(
            session_id=session_id,
            goal=plan_payload.get("goal", user_msg),
            container_id=container_id,
            tasks=tasks,
        )

        usage = getattr(final_message, "usage", None)
        if usage is not None:
            logger.info(
                f"[planner] cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
                f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)} "
                f"in={getattr(usage, 'input_tokens', 0)} out={getattr(usage, 'output_tokens', 0)}"
            )
        return plan

    @staticmethod
    def _build_user_block(
        user_msg: str,
        container_id: Optional[str],
        clarifications: Optional[list[dict[str, str]]],
        prior_failure: Optional[dict[str, Any]],
        available_containers: Optional[list[str]],
        live_tools: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        parts = [f"User request: {user_msg}"]
        if container_id:
            parts.append(f"Primary container_id: {container_id}")
        if available_containers and len(available_containers) > 1:
            parts.append(
                f"All available containers: {', '.join(available_containers)}.\n"
                f"For cross-container scope ('all my X' / 'across my documents'), emit a "
                f"CODE_TRANSFORM that iterates this list calling "
                f"`mcp.get_active_documents_metadata(c)` per container, then dispatches per-doc "
                f"tools (translate / insights) with the right container_id per call."
            )
        if clarifications:
            parts.append("Clarifications gathered:")
            for c in clarifications:
                parts.append(f"  Q: {c.get('question', '')}\n  A: {c.get('answer', '')}")
        if prior_failure:
            checkpoint = prior_failure.get("checkpoint")
            checkpoint_line = (
                f"\n  checkpoint: {checkpoint}" if checkpoint else ""
            )
            parts.append(
                "PRIOR ATTEMPT FAILED — replan around this failure:\n"
                f"  failed_task: {prior_failure.get('task_id')}\n"
                f"  error: {prior_failure.get('error')}"
                f"{checkpoint_line}\n"
                f"  prior_plan: {json.dumps(prior_failure.get('plan', {}), indent=2)[:2000]}"
            )
        if live_tools:
            tool_lines = []
            for t in live_tools:
                schema_str = json.dumps(t.get("input_schema", {}))
                tool_lines.append(
                    f"- {t['name']}: {t.get('description', '(no description)')}\n"
                    f"  input_schema: {schema_str}"
                )
            parts.append(
                "LIVE MCP TOOL SCHEMAS (discovered from server — authoritative):\n"
                + "\n".join(tool_lines)
            )
        parts.append("Now emit the plan via the emit_plan tool.")
        return "\n\n".join(parts)
