"""Orchestrator — the top-level agent loop.

Receives a user message, runs Interrogator (Plan Mode) → Planner → Scheduler,
and composes the final answer. Synthesizer used to be its own module; it's
inlined here because it's 25 lines of "ask Claude to summarise these outputs"
and didn't earn separate file status.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from app.api.sse import EventBus
from app.config import settings
from app.engine.interrogator import Interrogator
from app.engine.planner import Planner, validate_and_repair
from app.engine.prompts import SYNTHESIZER_SYSTEM
from app.engine.scheduler import Scheduler
from app.llm.anthropic_client import get_async_client
from app.mcp_client.client import list_tools as mcp_list_tools
from app.models import Plan, SessionStatus, TaskStatus
from app.state.store import Store

MAX_REPLANS = 1

# Match relative artifact paths the synthesizer (or pre-existing code) might
# emit, so we can rewrite them to absolute clickable URLs in one pass.
_ARTIFACT_RE = re.compile(r"(?<![\w/])artifacts/([\w\-./]+)")


# ── Synthesizer (was app/engine/synthesizer.py) ──────────────────────────────

def _stringify_outputs(outputs: dict[str, Any], limit: int = 6000) -> str:
    lines = []
    for tid, val in outputs.items():
        text = json.dumps(val, default=str) if isinstance(val, (dict, list)) else str(val)
        lines.append(f"--- {tid} ---\n{text[:limit]}")
    return "\n\n".join(lines)


def _artifact_url(ref: str) -> str:
    """Turn `artifacts/sess_x/T3.html` into a full clickable URL."""
    base = settings.public_base_url.rstrip("/")
    # Strip any leading "./" or "artifacts/" so we always serve under /artifacts/.
    rel = ref.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if rel.startswith("artifacts/"):
        rel = rel[len("artifacts/") :]
    return f"{base}/artifacts/{rel}"


def _absolutize_artifact_paths(text: str) -> str:
    """Rewrite any bare `artifacts/...` paths in the final answer to full URLs.

    The synthesizer is instructed to do this itself, but we belt-and-suspender
    here in case the LLM falls back to relative paths.
    """
    return _ARTIFACT_RE.sub(
        lambda m: _artifact_url("artifacts/" + m.group(1)),
        text,
    )


async def _synthesize_final_answer(
    plan: Plan, outputs: dict[str, Any], user_msg: str
) -> str:
    """Compose the user-facing final answer from task outputs."""
    client = get_async_client()
    # Enumerate every artifact the plan produced so the synthesizer always
    # has the full clickable URL available — never has to construct one.
    artifact_lines = []
    for t in plan.tasks:
        if t.artifact_ref:
            url = _artifact_url(t.artifact_ref)
            artifact_lines.append(f"- {t.id} ({t.title}): {url}")
    artifacts_block = (
        "Artifacts produced (use these EXACT URLs in your Report line):\n"
        + "\n".join(artifact_lines)
        if artifact_lines
        else "Artifacts produced: none"
    )
    body = (
        f"User asked: {user_msg}\n\n"
        f"Plan goal: {plan.goal}\n\n"
        f"Public base URL: {settings.public_base_url.rstrip('/')}\n"
        f"{artifacts_block}\n\n"
        f"Task outputs:\n{_stringify_outputs(outputs)}\n\n"
        "Compose the final answer following the rules in your system prompt. "
        "Use a markdown table for any countable metrics. If artifacts exist, "
        "end with a Report line containing the absolute URL (no backticks)."
    )
    msg = await client.messages.create(
        model=settings.worker_model,
        max_tokens=2000,
        system=SYNTHESIZER_SYSTEM,
        messages=[{"role": "user", "content": body}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    return _absolutize_artifact_paths("\n".join(parts).strip())


class Orchestrator:
    """The top-level agent. One instance per process.

    For each user message it runs: Interrogator -> wait for answers if needed
    -> Planner -> validate+repair -> Scheduler -> Synthesizer.

    `start_session(...)` returns immediately after kicking off the background
    task; callers stream events via the SSE endpoint to observe progress.
    """

    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        self.interrogator = Interrogator(store, bus)
        self.planner = Planner(bus)
        self.scheduler = Scheduler(store, bus)
        self._in_flight: dict[str, asyncio.Task] = {}

    async def start_session(
        self, session_id: str, user_msg: str, container_id: Optional[str]
    ) -> None:
        task = asyncio.create_task(self._run(session_id, user_msg, container_id))
        self._in_flight[session_id] = task
        task.add_done_callback(lambda _t: self._in_flight.pop(session_id, None))

    async def resume_interrupted_sessions(self) -> list[str]:
        """Scan the store for sessions that were mid-execution at process
        death and kick off a re-execution for each.

        Returns the list of session_ids that were resumed. Called from
        FastAPI lifespan on startup; the scheduler reads INTERRUPTED tasks
        as resumable PENDING and the code worker reads their checkpoints
        via __resume_from__.
        """
        if not settings.resume_interrupted_on_startup:
            return []
        sids = await self.store.find_resumable_sessions()
        resumed: list[str] = []
        for sid in sids:
            session = await self.store.get_session(sid)
            if session is None:
                continue
            n = await self.store.mark_running_tasks_interrupted(sid)
            logger.info(f"[resume] session={sid} interrupted_tasks={n}")
            await self.bus.emit(
                sid,
                "session.resumed",
                {"interrupted_tasks": n, "reason": "process restart"},
            )
            # Kick off background re-execution. Skips interrogation because
            # any clarifying answers are already persisted.
            task = asyncio.create_task(
                self._run_resume(sid, session.user_msg, session.container_id)
            )
            self._in_flight[sid] = task
            task.add_done_callback(lambda _t, s=sid: self._in_flight.pop(s, None))
            resumed.append(sid)
        return resumed

    async def _run_resume(
        self, session_id: str, user_msg: str, container_id: Optional[str]
    ) -> None:
        """Resume execution of the latest plan for a session.

        Differs from _run: skips interrogation AND skips re-planning. The
        existing plan's INTERRUPTED tasks restart from their checkpoints;
        SUCCEEDED tasks stay SUCCEEDED.
        """
        try:
            plan = await self.store.latest_plan_for_session(session_id)
            if plan is None:
                logger.warning(f"[resume] session={session_id} has no plan; falling back to fresh run")
                await self._run(session_id, user_msg, container_id, skip_interrogation=True)
                return

            from app.engine.containers import discover_containers

            available_containers = discover_containers()
            outputs = await self.scheduler.run(plan, container_id, available_containers)

            failed = [t for t in plan.tasks if t.status == TaskStatus.FAILED]
            succeeded = [t for t in plan.tasks if t.status == TaskStatus.SUCCEEDED]
            if failed:
                final = self._compose_failure_answer(plan)
            else:
                final = await _synthesize_final_answer(plan, outputs, user_msg)
            await self.store.update_session_status(
                session_id,
                SessionStatus.SUCCEEDED if not failed else SessionStatus.FAILED,
                final,
            )
            payload = {
                "final_answer": final,
                "succeeded_tasks": [t.id for t in succeeded],
                "failed_tasks": [t.id for t in failed],
                "artifacts": [
                    {"task_id": t.id, "ref": t.artifact_ref, "url": _artifact_url(t.artifact_ref)}
                    for t in plan.tasks
                    if t.artifact_ref
                ],
                "resumed": True,
            }
            await self.bus.emit(session_id, "session.completed", payload)
            await self._fire_webhook(session_id, "session.completed", payload)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"resume failed for session {session_id}")
            await self.store.update_session_status(session_id, SessionStatus.FAILED)
            err_payload = {"error": f"{type(e).__name__}: {e}", "resumed": True}
            await self.bus.emit(session_id, "session.error", err_payload)
            await self._fire_webhook(session_id, "session.error", err_payload)

    async def _fire_webhook(self, session_id: str, event_type: str, payload: dict) -> None:
        """POST the terminal event to the session's webhook_url if one was set.

        Fire-and-forget with one retry. Failures are logged but never crash
        the orchestrator — a webhook is best-effort by design.
        """
        session = await self.store.get_session(session_id)
        if session is None or not session.webhook_url:
            return
        url = session.webhook_url
        body = {
            "session_id": session_id,
            "event": event_type,
            "ts": payload.get("ts"),
            "payload": payload,
        }
        for attempt in range(settings.webhook_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
                    resp = await client.post(url, json=body)
                if resp.status_code < 500:
                    logger.info(f"[webhook] session={session_id} url={url} status={resp.status_code}")
                    return
                logger.warning(
                    f"[webhook] session={session_id} url={url} 5xx={resp.status_code} "
                    f"attempt={attempt + 1}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[webhook] session={session_id} url={url} error={e} attempt={attempt + 1}")
            if attempt < settings.webhook_max_retries:
                await asyncio.sleep(1.5 ** attempt)

    async def resume_session(self, session_id: str) -> None:
        """Re-run the planner+executor for a session that was awaiting answers."""
        from app.engine.containers import discover_containers

        session = await self.store.get_session(session_id)
        if session is None:
            return

        # If the user just answered the "which container?" question, promote
        # that answer to session.container_id before re-entering the run loop.
        container_id = session.container_id
        if not container_id:
            known = set(discover_containers())
            for q in await self.store.get_all_questions(session_id):
                if q.answer and q.answer in known:
                    container_id = q.answer
                    await self.store.conn.execute(
                        "UPDATE sessions SET container_id = ? WHERE id = ?",
                        (container_id, session_id),
                    )
                    await self.store.conn.commit()
                    break

        task = asyncio.create_task(
            self._run(session.id, session.user_msg, container_id, skip_interrogation=True)
        )
        self._in_flight[session_id] = task
        task.add_done_callback(lambda _t: self._in_flight.pop(session_id, None))

    async def _run(
        self,
        session_id: str,
        user_msg: str,
        container_id: Optional[str],
        skip_interrogation: bool = False,
    ) -> None:
        try:
            await self.bus.emit(
                session_id,
                "session.started",
                {"user_msg": user_msg, "container_id": container_id},
            )

            try:
                live_tools = await mcp_list_tools()
                logger.info(f"[orchestrator] discovered {len(live_tools)} MCP tools: {[t['name'] for t in live_tools]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[orchestrator] tool discovery failed, falling back to prompt knowledge: {e}")
                live_tools = []

            available_containers: list[str] = []
            if not skip_interrogation:
                await self.store.update_session_status(session_id, SessionStatus.PLANNING)
                result = await self.interrogator.interrogate(session_id, user_msg, container_id)
                available_containers = result.available_containers
                # Adopt whatever container_id the interrogator resolved.
                if result.resolved_container_id and not container_id:
                    container_id = result.resolved_container_id
                    await self.store.conn.execute(
                        "UPDATE sessions SET container_id = ? WHERE id = ?",
                        (container_id, session_id),
                    )
                    await self.store.conn.commit()
                if not result.proceed:
                    await self.store.update_session_status(
                        session_id, SessionStatus.AWAITING_ANSWER
                    )
                    return
            else:
                # Resume path — re-derive the container list so the planner
                # still gets the multi-container context.
                from app.engine.containers import discover_containers

                available_containers = discover_containers()

            # Build the clarifications context (always include answered questions)
            answered = [
                {"question": q.text, "answer": q.answer or ""}
                for q in await self.store.get_all_questions(session_id)
                if q.answer
            ]

            plan = await self._plan_with_validation(
                session_id, user_msg, container_id, answered, available_containers,
                live_tools=live_tools,
            )

            await self.store.update_session_status(session_id, SessionStatus.EXECUTING)
            active_plan, outputs = await self._execute_with_replan(
                plan, container_id, user_msg, answered, available_containers,
                live_tools=live_tools,
            )

            failed = [t for t in active_plan.tasks if t.status == TaskStatus.FAILED]
            succeeded = [t for t in active_plan.tasks if t.status == TaskStatus.SUCCEEDED]

            if failed:
                final = self._compose_failure_answer(active_plan)
            else:
                final = await _synthesize_final_answer(active_plan, outputs, user_msg)
            await self.store.update_session_status(
                session_id, SessionStatus.SUCCEEDED if not failed else SessionStatus.FAILED, final
            )
            payload = {
                "final_answer": final,
                "succeeded_tasks": [t.id for t in succeeded],
                "failed_tasks": [t.id for t in failed],
                "artifacts": [
                    {
                        "task_id": t.id,
                        "ref": t.artifact_ref,
                        "url": _artifact_url(t.artifact_ref),
                    }
                    for t in active_plan.tasks
                    if t.artifact_ref
                ],
            }
            await self.bus.emit(session_id, "session.completed", payload)
            await self._fire_webhook(session_id, "session.completed", payload)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"orchestrator failed for session {session_id}")
            await self.store.update_session_status(session_id, SessionStatus.FAILED)
            err_payload = {"error": f"{type(e).__name__}: {e}"}
            await self.bus.emit(session_id, "session.error", err_payload)
            await self._fire_webhook(session_id, "session.error", err_payload)

    @staticmethod
    def _compose_failure_answer(plan: Plan) -> str:
        failed = [t for t in plan.tasks if t.status == TaskStatus.FAILED]
        skipped = [t for t in plan.tasks if t.status == TaskStatus.SKIPPED]
        artifacts = [t for t in plan.tasks if t.artifact_ref]

        lines = [
            "I could not complete the request.",
            "",
            "Failed tasks:",
        ]
        for task in failed:
            lines.append(f"- {task.id} ({task.title}): {task.error or 'unknown error'}")

        if skipped:
            lines.append("")
            lines.append("Skipped tasks:")
            for task in skipped:
                lines.append(f"- {task.id} ({task.title}): {task.error or 'upstream dependency failed'}")

        if artifacts:
            lines.append("")
            lines.append("Artifacts created before the failure:")
            for task in artifacts:
                lines.append(f"- {task.id}: {task.artifact_ref}")

        return "\n".join(lines)

    async def _plan_with_validation(
        self,
        session_id: str,
        user_msg: str,
        container_id: Optional[str],
        clarifications: list[dict[str, str]],
        available_containers: Optional[list[str]] = None,
        prior_failure: Optional[dict[str, Any]] = None,
        live_tools: Optional[list[dict[str, Any]]] = None,
    ) -> Plan:
        plan = await self.planner.plan(
            session_id,
            user_msg,
            container_id,
            clarifications,
            prior_failure,
            available_containers or [],
            live_tools=live_tools,
        )
        plan, repairs = validate_and_repair(plan, live_tools=live_tools)
        for r in repairs:
            await self.bus.emit(session_id, "plan.repaired", r)
        await self.store.save_plan(plan)
        await self.bus.emit(
            session_id,
            "plan.created",
            {"plan_id": plan.plan_id, "goal": plan.goal, "tasks": [t.model_dump() for t in plan.tasks]},
        )
        return plan

    async def _execute_with_replan(
        self,
        plan: Plan,
        container_id: Optional[str],
        user_msg: str,
        clarifications: list[dict[str, str]],
        available_containers: Optional[list[str]] = None,
        live_tools: Optional[list[dict[str, Any]]] = None,
    ) -> tuple[Plan, dict[str, Any]]:
        outputs = await self.scheduler.run(plan, container_id, available_containers)
        failed = [t for t in plan.tasks if t.status == TaskStatus.FAILED]
        if not failed:
            return plan, outputs

        # One bounded replan pass.
        first_failure = failed[0]
        await self.bus.emit(
            plan.session_id,
            "plan.replanning",
            {
                "failed_task": first_failure.id,
                "error": first_failure.error,
                "remaining": [t.id for t in plan.tasks if t.status == TaskStatus.SKIPPED],
            },
        )
        prior_failure = {
            "task_id": first_failure.id,
            "error": first_failure.error,
            "checkpoint": first_failure.checkpoint,
            "plan": plan.model_dump(),
        }
        new_plan = await self._plan_with_validation(
            plan.session_id,
            user_msg,
            container_id,
            clarifications,
            available_containers,
            prior_failure,
            live_tools=live_tools,
        )
        new_outputs = await self.scheduler.run(new_plan, container_id, available_containers)
        outputs.update(new_outputs)
        return new_plan, outputs
