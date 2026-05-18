from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.state.store import Store

ARTIFACTS_DIR = Path("./artifacts")
INLINE_LIMIT_BYTES = 8 * 1024  # 8KB; larger outputs spill to disk


def _ensure_artifacts_dir(session_id: str) -> Path:
    target = ARTIFACTS_DIR / session_id
    target.mkdir(parents=True, exist_ok=True)
    return target


async def write_task_output(
    store: Store, session_id: str, task_id: str, output: Any
) -> tuple[Any, Optional[str]]:
    """Persist a task output. Small outputs stay inline; large outputs spill to disk.

    Returns (inline_value, artifact_ref). The caller persists both onto the Task.
    """
    if output is None:
        return None, None
    serialized = json.dumps(output, default=str)
    if len(serialized.encode("utf-8")) <= INLINE_LIMIT_BYTES:
        return output, None

    target = _ensure_artifacts_dir(session_id)
    path = target / f"{task_id}.json"
    path.write_text(serialized, encoding="utf-8")
    artifact_ref = str(path)

    await store.conn.execute(
        "INSERT INTO checkpoints (session_id, task_id, output_ref, created_at) VALUES (?, ?, ?, ?)",
        (session_id, task_id, artifact_ref, datetime.now(timezone.utc).isoformat()),
    )
    await store.conn.commit()

    preview: Any
    if isinstance(output, dict):
        # islice stops at 20 without materialising all keys — O(20) not O(N)
        preview = {"_preview": True, "keys": list(itertools.islice(output.keys(), 20))}
    elif isinstance(output, list):
        preview = {"_preview": True, "len": len(output), "first": list(itertools.islice(output, 3))}
    else:
        preview = {"_preview": True, "type": type(output).__name__, "len": len(serialized)}
    return preview, artifact_ref


def write_html_artifact(session_id: str, html: str, name: str = "dashboard.html") -> str:
    target = _ensure_artifacts_dir(session_id)
    path = target / name
    path.write_text(html, encoding="utf-8")
    return str(path)
