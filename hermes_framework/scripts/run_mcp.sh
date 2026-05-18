#!/usr/bin/env bash
# Start the FastMCP server locally (without Docker).
# Assumes the Assessment/ folder sits next to hermes_framework/.
set -euo pipefail

cd "$(dirname "$0")/.."
exec python -m app.mcp_server
