from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.sse import init_bus, stream_session_events
from app.config import settings
from app.engine.orchestrator import Orchestrator
from app.models import (
    AnswerRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    MessageRequest,
    Session,
    SessionStatus,
)
from app.state.store import get_store

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = await get_store()
    bus = init_bus(store)
    orchestrator = Orchestrator(store, bus)
    app.state.store = store
    app.state.bus = bus
    app.state.orchestrator = orchestrator
    logger.info("[startup] hermes-framework ready")
    yield
    logger.info("[shutdown] closing store")
    await store.close()


app = FastAPI(
    title="Hermes Framework",
    description="API-driven multi-agent agentic framework over MCP.",
    version="0.1.0",
    lifespan=lifespan,
)


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/sessions", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    store = app.state.store
    session = Session(user_msg="", container_id=req.container_id)
    await store.create_session(session)
    return CreateSessionResponse(session_id=session.id, status=session.status)


@app.post("/v1/sessions/{session_id}/messages")
async def post_message(session_id: str, req: MessageRequest) -> dict[str, str]:
    store = app.state.store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Persist the new user message + container_id by recreating the row.
    container_id = req.container_id or session.container_id
    session.user_msg = req.message
    session.container_id = container_id
    session.status = SessionStatus.PLANNING
    await store.conn.execute(
        "UPDATE sessions SET user_msg = ?, container_id = ?, status = ? WHERE id = ?",
        (session.user_msg, container_id, session.status.value, session.id),
    )
    await store.conn.commit()

    await app.state.orchestrator.start_session(session.id, req.message, container_id)
    return {"status": "accepted", "session_id": session.id}


@app.post("/v1/sessions/{session_id}/answer")
async def post_answer(session_id: str, req: AnswerRequest) -> dict[str, str]:
    store = app.state.store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    await store.answer_question(req.question_id, req.answer)
    await app.state.bus.emit(
        session_id,
        "plan_mode.answered",
        {"question_id": req.question_id, "answer": req.answer},
    )

    pending = await store.get_pending_questions(session_id)
    if not pending:
        await app.state.orchestrator.resume_session(session_id)
        return {"status": "resumed"}
    return {"status": "awaiting_more_answers", "pending": str(len(pending))}


@app.get("/v1/sessions/{session_id}/events")
async def get_events(
    session_id: str, cursor: int = Query(0, ge=0)
) -> StreamingResponse:
    store = app.state.store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return StreamingResponse(
        stream_session_events(session_id, cursor),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    store = app.state.store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    questions = await store.get_all_questions(session_id)
    return {
        "session": session.model_dump(),
        "questions": [q.model_dump() for q in questions],
    }


@app.get("/ui")
async def ui() -> FileResponse:
    index = UI_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="UI not packaged")
    return FileResponse(index)


# Static assets for the UI.
if UI_DIR.exists():
    app.mount("/ui-assets", StaticFiles(directory=UI_DIR), name="ui-assets")


@app.get("/")
async def index() -> JSONResponse:
    return JSONResponse(
        {
            "service": "hermes-framework",
            "endpoints": {
                "create_session": "POST /v1/sessions",
                "send_message": "POST /v1/sessions/{id}/messages",
                "stream_events": "GET /v1/sessions/{id}/events",
                "answer": "POST /v1/sessions/{id}/answer",
                "get_session": "GET /v1/sessions/{id}",
                "viewer": "GET /ui",
                "health": "GET /healthz",
            },
        }
    )
