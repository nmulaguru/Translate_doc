from __future__ import annotations

import itertools
import json
import re
from typing import Any

from app.config import settings
from app.engine.prompts import SUBAGENT_SYSTEM
from app.llm.anthropic_client import get_async_client
from app.models import Task
from app.state.checkpoint import write_html_artifact
from app.workers.base import Worker, WorkerContext


class SubAgentWorker(Worker):
    """Spawns a child Claude conversation with isolated context.

    Used for SUBAGENT tasks — typically creative/synthesis work the planner
    chose to delegate. The child's context contains only the role,
    instructions, and the relevant upstream task outputs (not the full
    session history). When the response looks like an HTML document we
    persist it as an artifact and return an artifact_ref.
    """

    name = "subagent"

    async def execute(self, ctx: WorkerContext, task: Task) -> Any:
        role = task.spec.get("role", "specialist")
        instructions = task.spec.get("instructions", "")
        inputs_from = task.spec.get("inputs_from", task.depends_on) or []

        upstream = {k: ctx.upstream_outputs[k] for k in inputs_from if k in ctx.upstream_outputs}
        upstream_block = json.dumps(_compact_for_prompt(upstream), default=str)

        user = (
            f"Role: {role}\n\n"
            f"Instructions:\n{instructions}\n\n"
            f"Upstream outputs (JSON):\n{upstream_block}"
        )

        await ctx.bus.emit(
            ctx.session_id,
            "subagent.spawned",
            {"task_id": task.id, "role": role},
        )

        client = get_async_client()
        msg = await client.messages.create(
            model=settings.worker_model,
            max_tokens=8000,
            system=SUBAGENT_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text_parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
        text = "\n".join(text_parts).strip()

        html = _extract_html_document(text)

        # Heuristic: if the output is HTML, persist it as an artifact instead
        # of inlining it in the task output. Keeps SSE payloads tiny.
        if html is not None:
            path = write_html_artifact(ctx.session_id, html, name=f"{task.id}.html")
            return {"kind": "html_artifact", "artifact_ref": path, "size_bytes": len(html)}

        return {"kind": "text", "text": text}


def _extract_html_document(text: str) -> str | None:
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered.startswith("<!doctype") or lowered.startswith("<html"):
        return stripped

    fenced = re.search(r"```html\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
        lowered_candidate = candidate.lower()
        if lowered_candidate.startswith("<!doctype") or lowered_candidate.startswith("<html"):
            return candidate

    start_candidates = [
        idx for idx in (lowered.find("<!doctype"), lowered.find("<html")) if idx >= 0
    ]
    if not start_candidates:
        return None
    start = min(start_candidates)
    end = lowered.rfind("</html>")
    if end < start:
        return None
    return stripped[start : end + len("</html>")].strip()


def _compact_for_prompt(value: Any, *, list_limit: int = 50, depth: int = 0) -> Any:
    if depth > 6:
        return "<max depth reached>"
    if isinstance(value, list):
        n = len(value)
        # islice stops at list_limit without building a full copy — O(list_limit) not O(N).
        items = [
            _compact_for_prompt(v, list_limit=list_limit, depth=depth + 1)
            for v in itertools.islice(value, list_limit)
        ]
        if n > list_limit:
            items.append({"_truncated": n - list_limit, "_total": n})
        return items
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"documents", "inventory_sample"} and isinstance(item, list):
                compact[key] = _compact_for_prompt(item, list_limit=list_limit, depth=depth + 1)
            elif key == "insights_by_doc" and isinstance(item, dict):
                n = len(item)
                # islice over dict.items() — O(list_limit) not O(N).
                sample = list(itertools.islice(item.items(), list_limit))
                compact[key] = {
                    k: _compact_for_prompt(v, list_limit=list_limit, depth=depth + 1)
                    for k, v in sample
                }
                if n > list_limit:
                    compact[key]["_truncated"] = n - list_limit
                    compact[key]["_total"] = n
            else:
                compact[key] = _compact_for_prompt(item, list_limit=list_limit, depth=depth + 1)
        return compact
    return value
