#!/usr/bin/env bash
# Start the agent API locally.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && export $(grep -v '^#' .env | xargs) || true
exec python -m app.main
