#!/usr/bin/env bash
# Hit each of the 6 PDF example queries against a running agent at :8080.
# Streams the SSE response for each one and prints task.* events inline.
#
#   ./scripts/demo_queries.sh

set -euo pipefail
API="${API:-http://localhost:8080}"
CONTAINER="${CONTAINER:-container_001}"

QUERIES=(
  "Can you translate all my financial documents in my container to German?"
  "What are my payment terms?"
  "Can you create an HTML dashboard from all my documents?"
  "Can you convert all my PDF documents to DOCX?"
  "Find all documents containing high-risk indemnification clauses"
  "Generate a summary report of all legal agreements with high PII exposure"
)

for q in "${QUERIES[@]}"; do
  echo
  echo "============================================================"
  echo "Q: $q"
  echo "============================================================"

  SID=$(curl -s -X POST "$API/v1/sessions" \
    -H 'content-type: application/json' \
    -d "{\"container_id\": \"$CONTAINER\"}" | python -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
  echo "  session: $SID"

  # Open the stream in the background so we don't miss early events.
  (curl -sN "$API/v1/sessions/$SID/events" | grep --line-buffered -E "^event: " &)

  sleep 0.3

  curl -s -X POST "$API/v1/sessions/$SID/messages" \
    -H 'content-type: application/json' \
    -d "{\"message\": $(printf '%s' "$q" | python -c 'import json,sys; print(json.dumps(sys.stdin.read()))') , \"container_id\": \"$CONTAINER\"}" > /dev/null

  # Crude wait: keep the stream open for up to 90s, kill it once session.completed/session.error shows up.
  sleep 90
  pkill -f "curl -sN $API/v1/sessions/$SID/events" 2>/dev/null || true
done
