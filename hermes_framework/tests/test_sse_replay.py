"""SSE events must be replayable by id cursor.

A late subscriber should be able to ask for events with id > N and get every
event that's been emitted on the session since.
"""

from pathlib import Path

import pytest

from app.api.sse import EventBus, encode_sse
from app.models import Session
from app.state.store import Store


@pytest.mark.asyncio
async def test_replay_after_cursor(tmp_path: Path):
    db = tmp_path / "events.db"
    store = Store(db)
    await store.connect()
    bus = EventBus(store)
    session = Session(id="sess_replay", user_msg="x")
    await store.create_session(session)

    ids = []
    for i in range(5):
        ids.append(await bus.emit(session.id, "task.code_progress", {"i": i}))

    # New subscriber asks from cursor 2 → should see ids 3, 4, 5.
    replayed = await store.events_after(session.id, cursor=ids[1])
    assert [e.id for e in replayed] == ids[2:]

    # Encode produces well-formed SSE frames.
    frame = encode_sse(replayed[0])
    assert frame.startswith(f"id: {replayed[0].id}\n")
    assert "event: task.code_progress" in frame
    assert frame.endswith("\n\n")

    await store.close()
