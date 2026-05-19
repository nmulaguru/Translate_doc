from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from app.api.sse import EventBus
from app.config import settings
from app.engine.router import Router
from app.models import Plan, Task, TaskKind, TaskStatus
from app.state.checkpoint import write_task_output
from app.state.store import Store
from app.workers.base import WorkerContext


def _load_artifact(artifact_ref: str | None) -> Any:
    """On resume, reconstitute a task's full output from its artifact file.

    The store keeps a small preview in tasks.output_blob but the full result
    spills to artifacts/<session>/<task>.json when over the inline limit.
    Downstream tasks need the full data, not the preview.
    """
    if not artifact_ref:
        return None
    p = Path(artifact_ref)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None

_MAX_CONCURRENT = 8  # raised from 4: more DAG tasks execute in parallel waves
# Substrings in `type(err).__name__: err.message` that mark a failure as
# DETERMINISTIC — same attempt will produce the same error. Don't retry.
# Splits into two groups for documentation only; behaviour is identical.
_FATAL_HINTS = (
    # Domain-level fatal — the request itself is wrong.
    "ValueError", "not found", "missing",
    # Python-level bugs in generated code — retrying re-executes the same
    # buggy script and burns time. The orchestrator's replan path is the
    # right recovery: the planner regenerates code with the error context.
    "TypeError", "SyntaxError", "NameError", "AttributeError",
    "ImportError", "ModuleNotFoundError", "IndentationError",
    # Sandbox guard violations — the script tried something the policy bans.
    # Regenerating differently is the only fix; retrying produces the same.
    "PolicyViolation", "violates sandbox policy",
)

# Task kinds that run in the CodeWorker sandbox — get the bulk timeout by default.
_BULK_KINDS = {TaskKind.BULK_TOOL_CALL, TaskKind.CODE_TRANSFORM}
_TASK_DEFAULT_TIMEOUT = 120  # matches Task.timeout_s default


def _is_retriable(err: BaseException) -> bool:
    msg = f"{type(err).__name__}: {err}"
    return not any(hint in msg for hint in _FATAL_HINTS)


class Scheduler:
    """Topo-wave DAG executor.

    Runs ready tasks (those whose dependencies have completed) in parallel up
    to `_MAX_CONCURRENT`. On per-task failure, retries with exponential
    backoff up to `task.max_retries`. After terminal failure, downstream tasks
    transitively skip; the orchestrator decides whether to replan.
    """

    def __init__(self, store: Store, bus: EventBus, router: Router | None = None) -> None:
        self.store = store
        self.bus = bus
        self.router = router or Router()

    async def run(
        self,
        plan: Plan,
        container_id: str | None,
        available_containers: list[str] | None = None,
    ) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        # INTERRUPTED tasks are resumable — promote them back to PENDING with
        # their checkpoint intact so the code worker can pick up where it
        # stopped (see __resume_from__ injection in code_worker.py).
        for t in plan.tasks:
            if t.status == TaskStatus.INTERRUPTED:
                t.status = TaskStatus.PENDING
        statuses: dict[str, TaskStatus] = {t.id: t.status for t in plan.tasks}
        # Seed outputs with the cached results of already-SUCCEEDED tasks so
        # downstream tasks can still read __upstream__ on resume. Prefer the
        # full artifact file over the inline preview when both exist.
        for t in plan.tasks:
            if t.status != TaskStatus.SUCCEEDED:
                continue
            full = _load_artifact(t.artifact_ref) if t.artifact_ref else None
            if full is not None:
                outputs[t.id] = full
            elif t.output is not None:
                outputs[t.id] = t.output
        by_id: dict[str, Task] = {t.id: t for t in plan.tasks}
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        avail = available_containers or []

        async def run_task(task: Task) -> None:
            async with semaphore:
                await self._execute_task(plan, task, container_id, outputs, avail)
                statuses[task.id] = task.status

        # Topo-wave loop
        while True:
            ready = [
                t
                for t in plan.tasks
                if statuses[t.id] == TaskStatus.PENDING
                and all(
                    statuses.get(d) == TaskStatus.SUCCEEDED for d in t.depends_on
                )
            ]
            if not ready:
                # Detect un-runnable remaining tasks (deps failed/skipped).
                blocked = [
                    t
                    for t in plan.tasks
                    if statuses[t.id] == TaskStatus.PENDING
                ]
                for t in blocked:
                    statuses[t.id] = TaskStatus.SKIPPED
                    t.status = TaskStatus.SKIPPED
                    t.error = "upstream dependency failed"
                    await self.store.update_task(plan.plan_id, t)
                    await self.bus.emit(
                        plan.session_id,
                        "task.skipped",
                        {"task_id": t.id, "reason": "upstream dependency failed"},
                    )
                break

            await asyncio.gather(*(run_task(t) for t in ready))
            if all(statuses[t.id] != TaskStatus.PENDING for t in plan.tasks):
                continue

        return outputs

    async def _execute_task(
        self,
        plan: Plan,
        task: Task,
        container_id: str | None,
        outputs: dict[str, Any],
        available_containers: list[str],
    ) -> None:
        from app.models import _now  # local import to avoid cycle on init

        worker = self.router.route(task)
        ctx = WorkerContext(
            session_id=plan.session_id,
            container_id=container_id,
            plan=plan,
            upstream_outputs=outputs,
            bus=self.bus,
            available_containers=available_containers,
            store=self.store,
        )

        # Bulk sandbox tasks get a higher timeout unless the planner set one explicitly.
        if task.kind in _BULK_KINDS and task.timeout_s == _TASK_DEFAULT_TIMEOUT:
            task.timeout_s = settings.sandbox_bulk_timeout_seconds

        task.status = TaskStatus.RUNNING
        task.started_at = _now()
        await self.store.update_task(plan.plan_id, task)
        await self.bus.emit(
            plan.session_id,
            "task.started",
            {"task_id": task.id, "kind": task.kind.value, "worker": worker.name, "title": task.title},
        )

        backoff = 1.0
        last_err: Exception | None = None
        while task.attempts <= task.max_retries:
            task.attempts += 1
            try:
                result = await asyncio.wait_for(worker.execute(ctx, task), timeout=task.timeout_s)
                inline, ref = await write_task_output(self.store, plan.session_id, task.id, result)
                task.output = inline
                task.artifact_ref = ref
                outputs[task.id] = result  # full result is kept in-memory for downstream tasks
                task.status = TaskStatus.SUCCEEDED
                task.ended_at = _now()
                await self.store.update_task(plan.plan_id, task)
                await self.bus.emit(
                    plan.session_id,
                    "task.completed",
                    {
                        "task_id": task.id,
                        "output_preview": inline,
                        "artifact_ref": ref,
                        "attempts": task.attempts,
                    },
                )
                return
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                last_err = e if isinstance(e, Exception) else RuntimeError("timeout")
                retriable = _is_retriable(last_err)
                logger.warning(
                    f"[scheduler] task {task.id} attempt {task.attempts} failed "
                    f"(retriable={retriable}): {last_err}"
                )
                await self.bus.emit(
                    plan.session_id,
                    "task.retrying" if retriable and task.attempts <= task.max_retries else "task.failed",
                    {
                        "task_id": task.id,
                        "attempt": task.attempts,
                        "error": str(last_err),
                        "retriable": retriable,
                    },
                )
                if not retriable or task.attempts > task.max_retries:
                    break
                await asyncio.sleep(backoff)
                backoff *= 2

        task.status = TaskStatus.FAILED
        task.ended_at = _now()
        task.error = str(last_err)
        await self.store.update_task(plan.plan_id, task)
