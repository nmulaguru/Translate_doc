"""Entry point that runs the Sample_FastMCP.py tools over streamable-HTTP.

The original Sample_FastMCP.py is left untouched; this module imports its
`mcp` object and runs it on the streamable-HTTP transport so the agent
service can reach it across the network (and across Docker Compose
services). Default port: 7700, path: /mcp (FastMCP default).

Usage:
    cd <repo root>
    python -m app.mcp_server
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Locate the Assessment/ folder. In local dev hermes_framework/ sits next to
# Assessment/; in Docker we set ASSESSMENT_DIR explicitly because the path
# layout differs (mcp container has /srv/Assessment mounted from a volume).
_env = os.environ.get("ASSESSMENT_DIR")
ASSESSMENT_DIR = Path(_env) if _env else Path(__file__).resolve().parent.parent.parent / "Assessment"
if not ASSESSMENT_DIR.exists():
    raise FileNotFoundError(
        f"Assessment dir not found at {ASSESSMENT_DIR}. Set ASSESSMENT_DIR env var."
    )
if str(ASSESSMENT_DIR) not in sys.path:
    sys.path.insert(0, str(ASSESSMENT_DIR))

# Sample_FastMCP locates fake_database.db relative to its own __file__. We don't
# need to chdir because that resolution is independent of CWD.
import Sample_FastMCP  # noqa: E402

mcp = Sample_FastMCP.mcp


def main() -> None:
    port = int(os.environ.get("MCP_PORT", "7700"))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
