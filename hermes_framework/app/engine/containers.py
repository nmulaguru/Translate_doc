"""Container discovery — the one piece of state the agent reads outside MCP.

Why this exists: the four MCP tools all require `container_id`, but a user
shouldn't have to know it. Ideally the MCP server would expose a
`list_containers` tool; since `Sample_FastMCP.py` is treated as-is, we read
the same SQLite directly. This is a thin auxiliary path — the agent doesn't
otherwise touch the DB.

If a future MCP server adds a real `list_containers` tool, swap the body of
`discover_containers()` for a single `mcp.call_tool(...)` call and delete the
SQLite import.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path


def _db_path() -> Path | None:
    """Locate fake_database.db. In local dev it's next to Sample_FastMCP.py;
    in Docker it's mounted at /srv/Assessment via the compose volume."""
    env = os.environ.get("ASSESSMENT_DIR")
    candidates = [
        Path(env) / "fake_database.db" if env else None,
        Path(__file__).resolve().parent.parent.parent.parent / "Assessment" / "fake_database.db",
        Path("/srv/Assessment/fake_database.db"),
    ]
    for c in candidates:
        if c and c.exists():
            return c
    return None


@lru_cache(maxsize=1)
def discover_containers() -> list[str]:
    """Return the list of distinct container IDs in the corpus, or [] if the
    DB can't be located. Cached for the lifetime of the process — containers
    don't appear or disappear during a session.
    """
    path = _db_path()
    if path is None:
        return []
    try:
        conn = sqlite3.connect(path)
        try:
            cur = conn.execute("SELECT DISTINCT container_id FROM documents ORDER BY container_id")
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return []
