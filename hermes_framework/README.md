# Hermes Framework

API-driven, Hermes/OpenCode/DeepAgents-style multi-agent agentic framework that
wraps the MCP tools in [`Assessment/Sample_FastMCP.py`](../Assessment/Sample_FastMCP.py)
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
python -m venv .venv && source .venv/bin/activate     # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"

cp .env.example .env
# edit .env — at minimum set ANTHROPIC_API_KEY=sk-ant-...

# Terminal 1: MCP server (reads Sample_FastMCP.py + fake_database.db)
python -m app.mcp_server

# Terminal 2: agent API
python -m app.main
```

Open <http://localhost:8080/ui> in a browser and try one of the suggested queries.

### 2. Docker Compose (full stack)

```bash
cd hermes_framework
cp .env.example .env
# put ANTHROPIC_API_KEY in .env
docker compose up --build
```

Then:
- **Agent UI**: <http://localhost:8080/ui>
- **MCP server**: <http://localhost:7700/mcp/> (direct probe; expects MCP client)

### 3. Tests

```bash
pip install -e ".[dev]"
pytest -q
```

The test suite focuses on the load-bearing logic (sandbox AST policy,
`validate_and_repair` auto-repair, DAG scheduler topology, SSE replay, marker
parsing) — not the LLM-dependent paths, which are exercised via the demo script.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/sessions` | Create session. Body: `{container_id?}`. Returns `{session_id, status}`. |
| POST | `/v1/sessions/{id}/messages` | Send the user message. Body: `{message, container_id?}`. Returns 202. |
| GET | `/v1/sessions/{id}/events?cursor=N` | SSE stream of agent events. Replayable from any cursor. |
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

The system handles the 6 prompts from the assessment PDF:

| Query | Expected behavior |
|---|---|
| "Translate all my financial documents to German" | `CODE_TRANSFORM` plan; sandbox iterates docs in 200-doc chunks across all containers in parallel; `##PROGRESS##` streams per chunk |
| "What are my payment terms?" | Single `RAG_QUERY` via `aiagent` MCP tool |
| "Create an HTML dashboard from all my documents" | Triggers Plan Mode (scope/charts/categories), then `SUBAGENT` writes HTML artifact |
| "Convert all my PDF documents to DOCX" | Planner surfaces that the tool surface doesn't support format conversion |
| "Find all documents with high-risk indemnification clauses" | `CODE_TRANSFORM` filters by keyword/classification fields |
| "Summary report of all legal agreements with high PII exposure" | `CODE_TRANSFORM` filters by `piiCount`, then `SYNTHESIZE` |

---

## Execution flow

For every user message:

```
POST /messages
    │
    ▼
Interrogator (1 LLM call)
    → auto-resolves container OR asks clarifying question(s)
    → if AWAITING_ANSWER: pause until POST /answer
    │
    ▼
Planner (1 LLM call, streaming with adaptive thinking)
    → emits DAG plan via emit_plan tool
    → validate_and_repair() auto-rewrites >20-doc TOOL_CALLs → CODE_TRANSFORM
    │
    ▼
Scheduler (topo-wave, up to 8 concurrent tasks)
    │
    ├── TOOL_CALL / RAG_QUERY  → ToolWorker (1 direct MCP call)
    ├── CODE_TRANSFORM / BULK  → CodeWorker
    │       │
    │       ├── mcp_list_tools() → build dynamic system prompt
    │       ├── LLM call → generate Python script
    │       └── subprocess sandbox → script calls MCP tools, streams ##PROGRESS##
    └── SUBAGENT / SYNTHESIZE  → SubAgentWorker (child Claude session)
    │
    ▼
_synthesize_final_answer() (1 LLM call)
    → compose user-facing answer from all task outputs
```

Minimum LLM calls for a simple query: **3** (Interrogator + Planner + Synthesizer).
Each `CODE_TRANSFORM` task adds 1 more. MCP tool calls happen inside the sandbox
subprocess — the planner LLM is never in the per-document loop.

---

## Observability

- **Live UI**: open `/ui` — every `task.started` / `task.mcp_call` /
  `task.code_progress` / `task.completed` / `plan.repaired` event shows up in
  real time. Full event log at the bottom of the page.
- **Logs**: structured via loguru on stdout. Includes `cache_read_input_tokens`
  / `cache_creation_input_tokens` for the planner so you can see prompt-cache
  hit rate.
- **Event log**: every event is persisted to SQLite. Late SSE subscribers
  pass `?cursor=N` to replay from any point — useful for reconnects.
- **Checkpoints**: `##CHECKPOINT##` markers from sandbox scripts are persisted
  to `task.checkpoint` and passed to the replanner on failure so bulk jobs
  can resume from their last known offset.

---

## Layout

```
hermes_framework/
├── README.md, ARCHITECTURE.md
├── pyproject.toml
├── docker-compose.yml, Dockerfile.api, Dockerfile.mcp
├── .env.example
├── app/
│   ├── api/
│   │   ├── server.py         # FastAPI routes + SSE streaming
│   │   └── sse.py            # event bus, encoder, SSE replay
│   ├── engine/
│   │   ├── orchestrator.py   # top-level loop + _synthesize_final_answer()
│   │   ├── interrogator.py   # Plan Mode — ask_clarifications or proceed
│   │   ├── planner.py        # adaptive thinking + native tool_use + validate_and_repair()
│   │   ├── router.py         # task_kind → worker (stateless)
│   │   ├── scheduler.py      # topo-wave DAG executor, retries, semaphore(8)
│   │   ├── containers.py     # discover available container IDs
│   │   └── prompts.py        # dynamic tool signatures + static strategy prompts
│   ├── workers/
│   │   ├── tool_worker.py    # one direct MCP call
│   │   ├── code_worker.py    # generates Python, runs in sandbox subprocess
│   │   └── subagent_worker.py # child Claude session (HTML, synthesis tasks)
│   ├── sandbox/
│   │   ├── runner.py         # subprocess entrypoint + marker helpers
│   │   ├── policy.py         # AST allowlist + filtered_builtins()
│   │   └── mcp_shim.py       # synthetic `mcp` module injected into sandbox
│   ├── mcp_client/client.py  # streamable-HTTP MCP client
│   ├── mcp_server.py         # entrypoint that runs Sample_FastMCP over HTTP
│   ├── state/
│   │   ├── store.py          # SQLite (WAL) — sessions, plans, tasks, events
│   │   ├── checkpoint.py     # artifact spill logic (>8KB → artifacts/)
│   │   └── schema.sql        # DB schema
│   ├── llm/anthropic_client.py
│   ├── ui/                   # static HTML + JS SSE viewer
│   ├── models.py             # Pydantic shapes (Task, Plan, Session, Event, …)
│   ├── config.py             # pydantic-settings — all config from .env
│   └── main.py               # uvicorn entrypoint
├── scripts/
│   ├── run_mcp.sh, run_api.sh
│   └── demo_queries.sh
└── tests/
    ├── test_sandbox_policy.py
    ├── test_validator.py          # validate_and_repair() unit tests
    ├── test_code_worker_markers.py
    ├── test_scheduler_dag.py
    ├── test_sse_replay.py
    ├── test_planner_schema.py
    ├── test_sandbox_runner.py
    └── test_tool_worker.py
```

---

## Notable design choices

1. **The planner never sees more than O(1) document IDs.** Anything iterative
   becomes a `CODE_TRANSFORM` task whose generated Python runs in a subprocess
   sandbox and calls MCP tools directly. Progress markers stream back via
   stdout → SSE. The LLM context stays tiny whether the corpus is 9K, 36K, or
   1M documents.

2. **Belt-and-suspenders on the 20-doc rule.** The planner prompt forbids
   `TOOL_CALL` with >20 doc IDs, but Sonnet 4.6 violates it ~3-5% of the time
   on edge cases. `validate_and_repair()` (in `planner.py`) catches and
   auto-rewrites these as `CODE_TRANSFORM`, emitting a `plan.repaired` SSE
   event. Prompt discipline alone is not sufficient.

3. **Dynamic tool signatures, not hardcoded.** `build_code_gen_system(live_tools)`
   in `prompts.py` builds the code-gen system prompt at runtime from
   `mcp_list_tools()` output. If a tool's input schema changes on the MCP
   server, the next code generation automatically reflects it.

4. **Native tool use + adaptive thinking.** `thinking={"type": "adaptive"}` with
   `tool_choice={"type": "auto"}` for the planner (forced tool use is
   incompatible with adaptive thinking). Workers use plain Sonnet without
   extended thinking for latency.

5. **Prompt caching.** Planner system prompt + `emit_plan` schema are stable
   bytes; `cache_control={"type": "ephemeral"}` amortises the prefix cost
   across repeat calls in the same process.

6. **Two layers of sandbox defense.** AST import allowlist (script side) +
   filtered builtins dict (runtime side). `python -I` and `RLIMIT_AS` were
   both removed — the former broke module loading on Python 3.11+, the latter
   killed the process at startup due to virtual address space reservation.
   Wall-clock timeout via `asyncio.wait_for` is the enforced safety net.

7. **DAG scheduler with bounded replan.** Tasks run in topological waves with
   concurrency cap of 8. Failed tasks retry with exponential backoff. After
   terminal failure with downstream work remaining, the orchestrator replans
   once (`MAX_REPLANS = 1`) with the failure + checkpoint context, then halts.

8. **Resumable & replayable.** Every event appended to SQLite (WAL); SSE
   clients reconnect via `?cursor=N`. Plans, tasks, and questions are
   persisted. `##CHECKPOINT##` markers allow replanner to resume bulk jobs
   from last known offset after timeout.

---

## Known limitations

- The MCP server runs `Sample_FastMCP.py` as-is — the bulk-translate tool has
  a hardcoded ~3% simulated failure rate. That's expected; failures are reported
  in the task output with full document ID lists.
- The sandbox AST policy is defense-in-depth for a trusted-but-confused LLM,
  not a hardened security boundary. For production, replace with E2B or
  Firecracker.
- Single-tenant demo — one Anthropic API key, no per-request auth.
- Windows: `resource.setrlimit` unavailable; timeout via `asyncio.wait_for`
  is the only enforced limit.
- Each `POST /messages` starts a fresh orchestrator run — no multi-turn
  conversation history across messages within a session.
