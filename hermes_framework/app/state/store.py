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
            "INSERT INTO sessions (id, container_id, status, created_at, user_msg, final_answer) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.container_id,
                session.status.value,
                session.created_at,
                session.user_msg,
                session.final_answer,
            ),
        )
        await self.conn.commit()

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self.conn.execute(
            "SELECT id, container_id, status, created_at, user_msg, final_answer "
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
        )

    async def update_session_status(
        self, session_id: str, status: SessionStatus, final_answer: Optional[str] = None
    ) -> None:
        await self.conn.execute(
            "UPDATE sessions SET status = ?, final_answer = COALESCE(?, final_answer) WHERE id = ?",
            (status.value, final_answer, session_id),
        )
        await self.conn.commit()

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
            " output_blob, artifact_ref, error, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )

    async def update_task(self, plan_id: str, task: Task) -> None:
        await self._upsert_task(plan_id, task)
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
