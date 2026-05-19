from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

from app.config import settings
from app.models import (
    Event,
    Plan,
    Question,
    Session,
    SessionStatus,
    Task,
    TaskKind,
    TaskStatus,
)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Store:
    """Async SQLite store for sessions, plans, tasks, events, questions, checkpoints.

    Single connection per process is fine for the demo. WAL mode is enabled so
    reads don't block writes; writes are serialized through a single asyncio
    task at the call sites that emit events at high frequency.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or settings.state_db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # Lightweight forward-migration for databases created before these
        # columns existed. SQLite has no IF NOT EXISTS for columns, so we
        # try and swallow the "duplicate column" error.
        for stmt in (
            "ALTER TABLE sessions ADD COLUMN webhook_url TEXT",
            "ALTER TABLE tasks ADD COLUMN checkpoint_json TEXT",
        ):
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError as e:
                # "duplicate column name" — already migrated, safe to ignore.
                if "duplicate column" not in str(e).lower():
                    raise
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Store.connect() must be called first"
        return self._conn

    # ── sessions ──────────────────────────────────────────────────────────
    async def create_session(self, session: Session) -> None:
        await self.conn.execute(
            "INSERT INTO sessions "
            "(id, container_id, status, created_at, user_msg, final_answer, webhook_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.container_id,
                session.status.value,
                session.created_at,
                session.user_msg,
                session.final_answer,
                session.webhook_url,
            ),
        )
        await self.conn.commit()

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self.conn.execute(
            "SELECT id, container_id, status, created_at, user_msg, final_answer, webhook_url "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Session(
            id=row[0],
            container_id=row[1],
            status=SessionStatus(row[2]),
            created_at=row[3],
            user_msg=row[4],
            final_answer=row[5],
            webhook_url=row[6] if len(row) > 6 else None,
        )

    async def update_session_status(
        self, session_id: str, status: SessionStatus, final_answer: Optional[str] = None
    ) -> None:
        await self.conn.execute(
            "UPDATE sessions SET status = ?, final_answer = COALESCE(?, final_answer) WHERE id = ?",
            (status.value, final_answer, session_id),
        )
        await self.conn.commit()

    # ── resume-on-startup helpers ─────────────────────────────────────────
    async def find_resumable_sessions(self) -> list[str]:
        """Find sessions that were mid-execution when the process died.

        PLANNING / EXECUTING states are in-flight; anything else is terminal
        or paused on user input. We don't auto-resume AWAITING_ANSWER because
        the user might still answer the open question.
        """
        async with self.conn.execute(
            "SELECT id FROM sessions WHERE status IN (?, ?) ORDER BY created_at ASC",
            (SessionStatus.PLANNING.value, SessionStatus.EXECUTING.value),
        ) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def mark_running_tasks_interrupted(self, session_id: str) -> int:
        """Promote any RUNNING tasks for a session to INTERRUPTED.

        Used on startup before re-executing. The scheduler treats
        INTERRUPTED as resumable PENDING with the saved checkpoint intact.
        Returns the number of tasks marked.
        """
        cur = await self.conn.execute(
            "UPDATE tasks SET status = ? "
            "WHERE status = ? AND plan_id IN (SELECT id FROM plans WHERE session_id = ?)",
            (TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value, session_id),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def latest_plan_for_session(self, session_id: str) -> Optional[Plan]:
        """Return the most recent plan persisted for this session, if any.

        On resume we re-execute the existing plan rather than re-running the
        planner — its tasks (with checkpoints) are the authoritative state.
        """
        async with self.conn.execute(
            "SELECT json_blob FROM plans WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        plan = Plan.model_validate_json(row[0])
        # Overlay current task statuses + checkpoints from the tasks table —
        # plans.json_blob is a snapshot at planning time and won't reflect
        # progress made before the crash.
        async with self.conn.execute(
            "SELECT id, status, attempts, output_blob, artifact_ref, error, "
            "       started_at, ended_at, checkpoint_json "
            "FROM tasks WHERE plan_id = ?",
            (plan.plan_id,),
        ) as cur:
            rows = await cur.fetchall()
        live = {r[0]: r for r in rows}
        for t in plan.tasks:
            r = live.get(t.id)
            if not r:
                continue
            t.status = TaskStatus(r[1])
            t.attempts = r[2] or 0
            if r[3] is not None:
                try:
                    t.output = json.loads(r[3])
                except json.JSONDecodeError:
                    t.output = r[3]
            t.artifact_ref = r[4]
            t.error = r[5]
            t.started_at = r[6]
            t.ended_at = r[7]
            if r[8] is not None:
                try:
                    t.checkpoint = json.loads(r[8])
                except json.JSONDecodeError:
                    t.checkpoint = None
        return plan

    # ── plans ─────────────────────────────────────────────────────────────
    async def save_plan(self, plan: Plan) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO plans (id, session_id, goal, json_blob, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (plan.plan_id, plan.session_id, plan.goal, plan.model_dump_json(), plan.created_at),
        )
        for task in plan.tasks:
            await self._upsert_task(plan.plan_id, task)
        await self.conn.commit()

    async def _upsert_task(self, plan_id: str, task: Task) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(id, plan_id, kind, title, depends_on_json, spec_json, status, attempts, "
            " output_blob, artifact_ref, error, started_at, ended_at, checkpoint_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                plan_id,
                task.kind.value,
                task.title,
                json.dumps(task.depends_on),
                json.dumps(task.spec),
                task.status.value,
                task.attempts,
                json.dumps(task.output) if task.output is not None else None,
                task.artifact_ref,
                task.error,
                task.started_at,
                task.ended_at,
                json.dumps(task.checkpoint, default=str) if task.checkpoint is not None else None,
            ),
        )

    async def update_task(self, plan_id: str, task: Task) -> None:
        await self._upsert_task(plan_id, task)
        await self.conn.commit()

    async def update_task_checkpoint(self, task_id: str, checkpoint: dict) -> None:
        """High-frequency checkpoint persist — called after EVERY
        ##CHECKPOINT## marker. Skips the full row rewrite for speed.
        Without this, a crash mid-task loses the resume offset entirely.
        """
        await self.conn.execute(
            "UPDATE tasks SET checkpoint_json = ? WHERE id = ?",
            (json.dumps(checkpoint, default=str), task_id),
        )
        await self.conn.commit()

    async def get_plan(self, plan_id: str) -> Optional[Plan]:
        async with self.conn.execute(
            "SELECT json_blob FROM plans WHERE id = ?", (plan_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Plan.model_validate_json(row[0])

    # ── events ────────────────────────────────────────────────────────────
    async def append_event(self, event: Event) -> int:
        cur = await self.conn.execute(
            "INSERT INTO events (session_id, ts, type, payload_json) VALUES (?, ?, ?, ?)",
            (event.session_id, event.ts, event.type, json.dumps(event.payload)),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def events_after(self, session_id: str, cursor: int = 0) -> list[Event]:
        async with self.conn.execute(
            "SELECT id, session_id, ts, type, payload_json FROM events "
            "WHERE session_id = ? AND id > ? ORDER BY id ASC",
            (session_id, cursor),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Event(id=r[0], session_id=r[1], ts=r[2], type=r[3], payload=json.loads(r[4]))
            for r in rows
        ]

    # ── questions ─────────────────────────────────────────────────────────
    async def save_question(self, session_id: str, q: Question) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO questions "
            "(id, session_id, text, options_json, answer, asked_at, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                q.id,
                session_id,
                q.text,
                json.dumps(q.options) if q.options else None,
                q.answer,
                q.asked_at,
                q.answered_at,
            ),
        )
        await self.conn.commit()

    async def answer_question(self, question_id: str, answer: str) -> None:
        from datetime import datetime, timezone

        await self.conn.execute(
            "UPDATE questions SET answer = ?, answered_at = ? WHERE id = ?",
            (answer, datetime.now(timezone.utc).isoformat(), question_id),
        )
        await self.conn.commit()

    async def get_pending_questions(self, session_id: str) -> list[Question]:
        async with self.conn.execute(
            "SELECT id, text, options_json, answer, asked_at, answered_at "
            "FROM questions WHERE session_id = ? AND answer IS NULL",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Question(
                id=r[0],
                text=r[1],
                options=json.loads(r[2]) if r[2] else None,
                answer=r[3],
                asked_at=r[4],
                answered_at=r[5],
            )
            for r in rows
        ]

    async def get_all_questions(self, session_id: str) -> list[Question]:
        async with self.conn.execute(
            "SELECT id, text, options_json, answer, asked_at, answered_at "
            "FROM questions WHERE session_id = ? ORDER BY asked_at",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Question(
                id=r[0],
                text=r[1],
                options=json.loads(r[2]) if r[2] else None,
                answer=r[3],
                asked_at=r[4],
                answered_at=r[5],
            )
            for r in rows
        ]


_store: Optional[Store] = None


async def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
        await _store.connect()
    return _store


@asynccontextmanager
async def store_lifespan() -> AsyncIterator[Store]:
    s = await get_store()
    try:
        yield s
    finally:
        await s.close()
