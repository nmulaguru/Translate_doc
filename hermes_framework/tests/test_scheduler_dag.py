r"""Diamond-DAG scheduler test.

   T1
  /  \
 T2  T3
  \  /
   T4

T2 + T3 must run in parallel after T1 succeeds; T4 must wait for both.
"""

import asyncio
from pathlib import Path

import pytest

from app.api.sse import EventBus
from app.engine.router import Router
from app.engine.scheduler import Scheduler
from app.models import Plan, Session, Task, TaskKind, TaskStatus
from app.state.store import Store
from app.workers.base import Worker, WorkerContext


class _StubWorker(Worker):
    name = "stub"

    def __init__(self) -> None:
        self.timings: dict[str, tuple[float, float]] = {}

    async def execute(self, ctx: WorkerContext, task: Task) -> dict:
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(0.05)
        end = asyncio.get_event_loop().time()
        self.timings[task.id] = (start, end)
        return {"id": task.id, "deps": list(ctx.upstream_outputs.keys())}


class _StubRouter(Router):
    def __init__(self, worker: _StubWorker) -> None:
        self._w = worker

    def route(self, task: Task) -> Worker:  # type: ignore[override]
        return self._w


@pytest.mark.asyncio
async def test_diamond_dag(tmp_path: Path):
    db = tmp_path / "test.db"
    store = Store(db)
    await store.connect()
    bus = EventBus(store)

    session = Session(id="sess_test", user_msg="x")
    await store.create_session(session)

    tasks = [
        Task(id="T1", kind=TaskKind.RAG_QUERY, title="root", spec={}, depends_on=[]),
        Task(id="T2", kind=TaskKind.RAG_QUERY, title="left", spec={}, depends_on=["T1"]),
        Task(id="T3", kind=TaskKind.RAG_QUERY, title="right", spec={}, depends_on=["T1"]),
        Task(id="T4", kind=TaskKind.SYNTHESIZE, title="merge", spec={}, depends_on=["T2", "T3"]),
    ]
    plan = Plan(session_id=session.id, goal="diamond", tasks=tasks)
    await store.save_plan(plan)

    worker = _StubWorker()
    scheduler = Scheduler(store, bus, _StubRouter(worker))
    outputs = await scheduler.run(plan, container_id=None)

    assert set(outputs.keys()) == {"T1", "T2", "T3", "T4"}
    assert all(t.status == TaskStatus.SUCCEEDED for t in plan.tasks)

    # T2 + T3 should overlap
    t2_start, _ = worker.timings["T2"]
    t3_start, _ = worker.timings["T3"]
    _, t1_end = worker.timings["T1"]
    assert t2_start >= t1_end - 0.01
    assert t3_start >= t1_end - 0.01
    # T4 must start after both T2 and T3 finish
    t4_start, _ = worker.timings["T4"]
    _, t2_end = worker.timings["T2"]
    _, t3_end = worker.timings["T3"]
    assert t4_start >= max(t2_end, t3_end) - 0.01

    await store.close()
