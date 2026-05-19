from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from app.api.sse import EventBus
from app.models import Plan, Task

if TYPE_CHECKING:
    # Forward-only: importing Store at runtime here would create a cycle
    # (Store → schema → sandbox writes → workers).
    from app.state.store import Store


@dataclass
class WorkerContext:
    session_id: str
    container_id: Optional[str]
    plan: Plan
    upstream_outputs: dict[str, Any]
    bus: EventBus
    available_containers: list[str] = field(default_factory=list)
    # Optional store handle. Workers that emit high-frequency state updates
    # (CodeWorker persisting ##CHECKPOINT## markers) use this to bypass the
    # full-row upsert path. Optional so tests can construct lightweight
    # contexts without a real store.
    store: Optional["Store"] = None


class Worker:
    name: str = "worker"

    async def execute(self, ctx: WorkerContext, task: Task) -> Any:  # pragma: no cover
        raise NotImplementedError
