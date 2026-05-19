from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class TaskKind(str, Enum):
    TOOL_CALL = "TOOL_CALL"
    BULK_TOOL_CALL = "BULK_TOOL_CALL"
    CODE_TRANSFORM = "CODE_TRANSFORM"
    RAG_QUERY = "RAG_QUERY"
    SUBAGENT = "SUBAGENT"
    SYNTHESIZE = "SYNTHESIZE"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # Task was RUNNING when the orchestrator process died. On startup the
    # resume worker marks it INTERRUPTED and re-runs it from its last
    # checkpoint. Distinct from FAILED so the scheduler knows it's resumable
    # with no replan needed.
    INTERRUPTED = "INTERRUPTED"
    SKIPPED = "SKIPPED"


class SessionStatus(str, Enum):
    CREATED = "CREATED"
    AWAITING_ANSWER = "AWAITING_ANSWER"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Task(BaseModel):
    id: str
    kind: TaskKind
    title: str
    depends_on: list[str] = Field(default_factory=list)
    spec: dict[str, Any] = Field(default_factory=dict)
    timeout_s: int = 120
    max_retries: int = 2
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    output: Optional[Any] = None
    artifact_ref: Optional[str] = None
    checkpoint: Optional[dict] = None  # last ##CHECKPOINT## emitted; survives timeout
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class Plan(BaseModel):
    plan_id: str = Field(default_factory=lambda: _uid("plan"))
    session_id: str
    goal: str
    container_id: Optional[str] = None
    tasks: list[Task]
    created_at: str = Field(default_factory=_now)


class Question(BaseModel):
    id: str = Field(default_factory=lambda: _uid("q"))
    text: str
    options: Optional[list[str]] = None
    answer: Optional[str] = None
    asked_at: str = Field(default_factory=_now)
    answered_at: Optional[str] = None


class Session(BaseModel):
    id: str = Field(default_factory=lambda: _uid("sess"))
    container_id: Optional[str] = None
    user_msg: str
    status: SessionStatus = SessionStatus.CREATED
    created_at: str = Field(default_factory=_now)
    final_answer: Optional[str] = None
    # Optional caller-provided URL — fired on session.completed / session.error
    # so long-running jobs can notify backends instead of holding open SSE.
    webhook_url: Optional[str] = None
    questions: list[Question] = Field(default_factory=list)


class Event(BaseModel):
    """SSE event written to the event log and streamed to clients."""

    id: int = 0  # AUTOINCREMENT from SQLite; 0 before persistence.
    session_id: str
    ts: str = Field(default_factory=_now)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateSessionRequest(BaseModel):
    container_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional — POSTed with the final-answer payload when the session ends.
    # Useful for jobs that outlive the user's SSE connection.
    webhook_url: Optional[str] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    status: SessionStatus


class MessageRequest(BaseModel):
    message: str
    container_id: Optional[str] = None


class AnswerRequest(BaseModel):
    question_id: str
    answer: str
