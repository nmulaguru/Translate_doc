from __future__ import annotations

from app.models import Task, TaskKind
from app.workers.base import Worker
from app.workers.code_worker import CodeWorker
from app.workers.subagent_worker import SubAgentWorker
from app.workers.tool_worker import ToolWorker


class Router:
    """Maps task kinds to workers. Workers are stateless singletons; per-task
    state flows through WorkerContext + Task."""

    def __init__(self) -> None:
        self._tool = ToolWorker()
        self._code = CodeWorker()
        self._subagent = SubAgentWorker()

    def route(self, task: Task) -> Worker:
        if task.kind in (TaskKind.TOOL_CALL, TaskKind.RAG_QUERY):
            return self._tool
        if task.kind in (TaskKind.CODE_TRANSFORM, TaskKind.BULK_TOOL_CALL):
            return self._code
        if task.kind in (TaskKind.SUBAGENT, TaskKind.SYNTHESIZE):
            return self._subagent
        raise ValueError(f"no worker for task kind {task.kind}")
