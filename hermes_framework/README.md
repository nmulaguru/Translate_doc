# Hermes Framework

API-driven, Hermes/OpenCode/DeepAgents-style multi-agent agentic framework that
wraps the four MCP tools in [`Assessment/Sample_FastMCP.py`](../Assessment/Sample_FastMCP.py)
and intelligently solves user queries against the enterprise document corpus
in [`Assessment/fake_database.db`](../Assessment/fake_database.db).

**Design intent:** scale to 1M+ documents without context explosion by
generating Python on demand and running it in a sandbox subprocess that calls
the MCP tools directly, streaming `##PROGRESS##` markers back to the user via
Server-Sent Events. The planner LLM never sees more than O(1) document IDs.

Full design rationale is in [ARCHITECTURE.md](./ARCHITECTURE.md).

---

## Quick start

### 0. Prerequisites
- Python 3.11+
- Docker + Docker Compose (for the polished demo)
- An Anthropic API key

### 1. Local dev (no Docker)

```bash
cd hermes_framework
python -m venv .venv && source .venv/bin/activate     # or .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env
# edit .env — at minimum set ANTHROPIC_API_KEY=sk-ant-...

# Terminal 1: MCP server (reads the existing Sample_FastMCP.py + fake_database.db)
./scripts/run_mcp.sh           # or: python -m app.mcp_server

# Terminal 2: agent API
./scripts/run_api.sh           # or: python -m app.main
```

Open <http://localhost:8080/ui> in a browser and try one of the suggested queries.

### 2. Docker Compose (full stack with Jaeger)

```bash
cd hermes_framework
cp .env.example .env
# put ANTHROPIC_API_KEY in .env
docker compose up --build
```

Then:
- **Agent UI**: <http://localhost:8080/ui>
- **Jaeger traces**: <http://localhost:16686> (service: `hermes-agent`)
- **MCP server**: <http://localhost:7700/mcp/> (direct probe; expects MCP client)

### 3. Tests

```bash
pip install -e ".[dev]"
pytest -q
```

The test suite focuses on the load-bearing logic (sandbox AST policy, plan
validator/auto-repair, DAG scheduler topology, SSE replay, marker parsing) —
not the LLM-dependent paths, which are exercised via the demo script.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/sessions` | Create session. Body: `{container_id?}`. Returns `{session_id, status}`. |
| POST | `/v1/sessions/{id}/messages` | Send the user message. Body: `{message, container_id?}`. Returns 202. |
| GET | `/v1/sessions/{id}/events?cursor=N` | SSE stream of agent events. Replayable. |
| POST | `/v1/sessions/{id}/answer` | Answer a Plan Mode clarifying question. Body: `{question_id, answer}`. |
| GET | `/v1/sessions/{id}` | Session snapshot + question list. |
| GET | `/ui` | Single-page HTML viewer (vanilla JS EventSource). |
| GET | `/healthz` | Liveness probe. |

### Minimal curl example

```bash
# 1) create session
SID=$(curl -s -X POST localhost:8080/v1/sessions \
        -H 'content-type: application/json' \
        -d '{"container_id":"container_001"}' \
        | python -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 2) open the SSE stream (Terminal A, leave running)
curl -N localhost:8080/v1/sessions/$SID/events

# 3) post the message (Terminal B)
curl -X POST localhost:8080/v1/sessions/$SID/messages \
     -H 'content-type: application/json' \
     -d '{"message":"Translate all financial documents to German"}'
```

---

## Demo queries

The system handles the 6 prompts from the assessment PDF. Run them all:

```bash
./scripts/demo_queries.sh
```

| Query | Expected behavior |
|---|---|
| "Translate all my financial documents to German" | `CODE_TRANSFORM` plan; sandbox iterates ~5,140 docs in chunks; `##PROGRESS##` streams every 200 |
| "What are my payment terms?" | Single `RAG_QUERY` via `aiagent` |
| "Create an HTML dashboard from all my documents" | Triggers Plan Mode (scope/charts/categories), then `SUBAGENT` writes HTML artifact |
| "Convert all my PDF documents to DOCX" | Planner surfaces that the tool surface doesn't support format conversion |
| "Find all documents with high-risk indemnification clauses" | `CODE_TRANSFORM` filters by keyword relevance |
| "Summary report of all legal agreements with high PII exposure" | `CODE_TRANSFORM` filters by `pii_count`, then `SYNTHESIZE` |

---

## Observability

- **Live**: open `/ui` in a browser — every `task.started` / `task.tool_call` /
  `task.code_progress` / `task.completed` / `plan.repaired` event shows up in
  real time. Full event log at the bottom of the page.
- **Traces**: Jaeger UI at `:16686`. One root span per session, with child
  spans for each task and each MCP tool call (FastAPI + httpx are
  auto-instrumented).
- **Logs**: structured JSON via loguru on stdout. Includes `cache_read_input_tokens`
  / `cache_creation_input_tokens` for the planner so you can see prompt-cache
  hit rate.
- **Event log**: every event is persisted to SQLite. Late SSE subscribers
  pass `?cursor=N` to replay from any point — useful for reconnects.

---

## Layout

```
hermes_framework/
├── README.md, ARCHITECTURE.md
├── pyproject.toml
├── docker-compose.yml, Dockerfile.api, Dockerfile.mcp
├── .env.example
├── app/
│   ├── api/server.py         # FastAPI routes + SSE
│   ├── api/sse.py            # event bus, encoder, replay
│   ├── engine/
│   │   ├── orchestrator.py   # top-level loop
│   │   ├── interrogator.py   # Plan Mode (asks clarifications)
│   │   ├── planner.py        # adaptive thinking + native tool_use + caching
│   │   ├── validator.py      # auto-repair >20-doc TOOL_CALL → CODE_TRANSFORM
│   │   ├── router.py         # task_kind → worker
│   │   ├── scheduler.py      # topo-wave DAG executor with retries
│   │   ├── synthesizer.py    # final-answer composer
│   │   └── prompts.py        # planner / interrogator / code-gen / sub-agent
│   ├── workers/
│   │   ├── tool_worker.py    # one MCP call
│   │   ├── code_worker.py    # generates Python, runs in sandbox subprocess
│   │   └── subagent_worker.py # child Claude session (HTML, synthesis)
│   ├── sandbox/
│   │   ├── runner.py         # `python -I -m app.sandbox.runner` entrypoint
│   │   ├── policy.py         # AST allowlist, builtins filter
│   │   └── mcp_shim.py       # synthetic `mcp` module injected into sandbox
│   ├── mcp_client/client.py  # streamable-HTTP MCP client + circuit breaker
│   ├── mcp_server.py         # entrypoint that runs Sample_FastMCP over HTTP
│   ├── state/                # SQLite (WAL) — sessions, plans, tasks, events
│   ├── telemetry/otel.py     # OpenTelemetry → Jaeger
│   ├── llm/anthropic_client.py
│   ├── ui/                   # static HTML + JS SSE viewer
│   ├── models.py             # Pydantic shapes
│   ├── config.py             # settings (env-driven)
│   └── main.py
├── scripts/
│   ├── run_mcp.sh, run_api.sh
│   └── demo_queries.sh       # hits all 6 PDF queries
└── tests/
    ├── test_sandbox_policy.py
    ├── test_validator.py
    ├── test_code_worker_markers.py
    ├── test_scheduler_dag.py
    ├── test_sse_replay.py
    └── test_planner_schema.py
```

---

## Notable design choices, briefly

1. **The planner never sees more than O(1) document IDs.** Discovery and
   actions both flow through the four MCP tools in `Sample_FastMCP.py`.
   Anything iterative (filter by category, fan out across containers, bulk
   translate, scan insights) becomes a `CODE_TRANSFORM` task whose generated
   Python runs in a subprocess sandbox and calls `mcp.get_active_documents_metadata`
   / `mcp.translate_document_preserving_structure` / `mcp.get_document_insights`
   / `mcp.aiagent` directly. Progress markers stream back via stdout → SSE.
   The LLM context stays tiny whether the corpus is 9K, 36K, or 1M documents.

   The framework treats the MCP server as the *only* path to the data — no
   parallel SQL agent reaching past it. Filtering happens in code on the
   per-container result returned by `get_active_documents_metadata`. This
   keeps the architecture grounded in the assessment's tool surface.

2. **Belt-and-suspenders on the 20-doc rule.** The planner prompt forbids
   `TOOL_CALL` with >20 doc IDs, but Sonnet 4.6 violates it ~3-5% of the time
   on edge cases. The plan validator catches and auto-rewrites these as
   `CODE_TRANSFORM`, emitting a `plan.repaired` SSE event. Prompt discipline
   alone is not sufficient.

3. **Native tool use + adaptive thinking, not XML-tagged Hermes parsing.** The
   "Hermes feel" comes from the planner/router/worker decomposition at the
   *system* level; on the wire we use Claude's native `tool_use` API with
   `thinking: {type: "adaptive"}` and `effort: "high"` for the planner only.
   Workers use plain Sonnet without extended thinking for latency.

4. **Prompt caching.** The planner system prompt + `emit_plan` schema are
   stable bytes; `cache_control={"type": "ephemeral"}` at the request level
   auto-caches them. Repeat calls in the same session amortise the write.

5. **Two layers of sandbox defense.** AST-based import allowlist (script side)
   + `python -I` isolated mode + filtered builtins dict + `RLIMIT_AS` on Linux
   (runtime side). The AST check rejects `import os`, `__import__('os')`,
   `eval`, `exec`, `getattr(__builtins__, ...)`, etc.

6. **DAG scheduler with bounded replan.** Tasks run in topological waves with
   a concurrency cap; failed tasks retry with exponential backoff. After
   terminal failure with downstream work remaining, the orchestrator
   re-invokes the planner once with the failure context, then halts. No
   unbounded replan loops.

7. **Resumable & replayable.** Every event is appended to SQLite (WAL); SSE
   clients reconnect via `?cursor=N` and get every event they missed. Plans,
   tasks, and questions are persisted, so an interrupted Plan Mode session
   can be answered later and resumed via `POST /v1/sessions/{id}/answer`.

---

## Known limitations

- The MCP server runs `Sample_FastMCP.py` as-is over HTTP — the bulk-translate
  tool has a hardcoded 3% simulated failure rate (see
  [`Sample_FastMCP.py:640`](../Assessment/Sample_FastMCP.py#L640)). That's
  expected; failures are reported in the task output.
- The sandbox import policy is AST-based; a determined adversary could find
  bypasses despite the runtime layer. For a production deployment, swap the
  subprocess sandbox for a real container (E2B, Firecracker, gVisor).
- The Anthropic API key is read once at startup. No per-request auth — this
  is a single-tenant demo.
- Windows: `resource.setrlimit` is unavailable, so sandbox RSS limit is
  best-effort (timeout still enforced via `asyncio.wait_for`).
