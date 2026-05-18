from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.api.sse import EventBus
from app.models import Plan, Task


@dataclass
class WorkerContext:
    session_id: str
    container_id: Optional[str]
    plan: Plan
    upstream_outputs: dict[str, Any]
    bus: EventBus
    available_containers: list[str] = field(default_factory=list)


class Worker:
    name: str = "worker"

    async def execute(self, ctx: WorkerContext, task: Task) -> Any:  # pragma: no cover
        raise NotImplementedError
