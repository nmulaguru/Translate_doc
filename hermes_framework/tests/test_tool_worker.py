"""Regression tests for the ToolWorker's container_id auto-injection.

Only the four MCP tools take `container_id`. The worker auto-injects it
when missing for those tools so the planner can omit it; for any other tool
the worker must pass args through unmodified.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api.sse import EventBus
from app.models import Plan, Session, Task, TaskKind
from app.state.store import Store
from app.workers.base import WorkerContext
from app.workers.tool_worker import ToolWorker


async def _make_ctx(tmp_path, container_id="container_001"):
    store = Store(tmp_path / "test.db")
    await store.connect()
    bus = EventBus(store)
    session = Session(id="sess_test", user_msg="x", container_id=container_id)
    await store.create_session(session)
    plan = Plan(session_id=session.id, goal="t", tasks=[])
    ctx = WorkerContext(
        session_id=session.id,
        container_id=container_id,
        plan=plan,
        upstream_outputs={},
        bus=bus,
    )
    return ctx, store


@pytest.mark.asyncio
async def test_container_id_injected_for_original_tools(tmp_path):
    """The 4 original MCP tools accept container_id — auto-inject is welcome."""
    ctx, store = await _make_ctx(tmp_path)
    try:
        recorded: dict = {}

        async def fake_call_tool(name, args):
            recorded["name"] = name
            recorded["args"] = args
            return {"ok": True}

        with patch("app.workers.tool_worker.get_client") as gc:
            gc.return_value.call_tool = AsyncMock(side_effect=fake_call_tool)
            task = Task(
                id="T1",
                kind=TaskKind.TOOL_CALL,
                title="get insights",
                spec={"tool": "get_document_insights", "args": {}},
            )
            await ToolWorker().execute(ctx, task)

        assert recorded["name"] == "get_document_insights"
        assert recorded["args"].get("container_id") == "container_001"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_container_id_NOT_injected_for_unknown_tools(tmp_path):
    """The auto-inject allowlist is exactly the four MCP tools. If someone
    later adds a tool that doesn't take container_id, the injection MUST
    NOT silently mutate its args."""
    ctx, store = await _make_ctx(tmp_path)
    try:
        recorded: dict = {}

        async def fake_call_tool(name, args):
            recorded["name"] = name
            recorded["args"] = args
            return {}

        with patch("app.workers.tool_worker.get_client") as gc:
            gc.return_value.call_tool = AsyncMock(side_effect=fake_call_tool)
            task = Task(
                id="T1",
                kind=TaskKind.TOOL_CALL,
                title="hypothetical",
                spec={
                    "tool": "some_future_tool_without_container_id",
                    "args": {"foo": "bar"},
                },
            )
            await ToolWorker().execute(ctx, task)

        assert recorded["name"] == "some_future_tool_without_container_id"
        assert "container_id" not in recorded["args"], (
            f"container_id leaked into unknown-tool args: {recorded['args']}"
        )
        assert recorded["args"]["foo"] == "bar"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_explicit_container_id_in_args_always_respected(tmp_path):
    """If the planner explicitly puts container_id in args, never overwrite it."""
    ctx, store = await _make_ctx(tmp_path, container_id="container_001")
    try:
        recorded: dict = {}

        async def fake_call_tool(name, args):
            recorded["args"] = args
            return {}

        with patch("app.workers.tool_worker.get_client") as gc:
            gc.return_value.call_tool = AsyncMock(side_effect=fake_call_tool)
            task = Task(
                id="T1",
                kind=TaskKind.TOOL_CALL,
                title="explicit container",
                spec={
                    "tool": "get_active_documents_metadata",
                    "args": {"container_id": "container_003"},
                },
            )
            await ToolWorker().execute(ctx, task)

        assert recorded["args"]["container_id"] == "container_003"
    finally:
        await store.close()
