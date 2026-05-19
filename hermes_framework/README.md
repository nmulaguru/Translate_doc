# Hermes Framework

API-driven, Hermes/OpenCode/DeepAgents-style multi-agent agentic framework that
wraps the MCP tools in [`Assessment/Sample_FastMCP.py`](../Assessment/Sample_FastMCP.py)
and intelligently solves user queries against the enterprise document corpus
in [`Assessment/fake_database.db`](../Assessment/fake_database.db).

**Design intent:** scale to 1M+ documents without context explosion by
generating Python on demand and running it in a sandbox subprocess that calls
the MCP tools directly, streaming `##PROGRESS##` markers back to the user via
Server-Sent Events. The planner LLM never sees more than O(1) document IDs.

**LLM call count is fixed at 4** (interrogator + planner + code-gen +
synthesizer) regardless of corpus size, that's the core scalability
invariant. For a 1M-doc translation the agent makes ~104 MCP calls
(100 paginated metadata fetches + 4 bulk-translate calls); the MCP server
does the 1M actual translations internally with `Semaphore(200)`.

**Survives process death.** Every `##CHECKPOINT##` marker is persisted to
SQLite immediately. On startup the orchestrator's lifespan re-executes any
session left in `EXECUTING` from its last checkpoint, generated code reads
`__resume_from__` and skips already-processed pages.

Full design rationale, architecture diagrams, and the assessment aligned
report are in [ARCHITECTURE_VIEW.md](./ARCHITECTURE_VIEW.md).
The browser UI is branded **Argos** (the many-eyed multi-agent watcher);
internals and infra stay named `hermes_*`.

---

## Quick start

After any of the three paths below the UI is at **<http://localhost:8080/ui>**.

You need an Anthropic API key (`sk-ant-…`), every path uses it the same way.

### Option 1, Docker Compose (recommended)

The cleanest path. One command brings up the MCP server + agent API + UI in
their own containers with the right ports, mounts, and durable state volume.
Nothing to install on your machine besides Docker.

**Prereqs:** Docker Desktop (Windows/Mac) or `docker` + `docker compose` (Linux).

```bash
cd hermes_framework
cp .env.example .env            # then edit .env and set ANTHROPIC_API_KEY=sk-ant-...

docker compose up --build       # first run takes 1-2 min to build images
```

Open <http://localhost:8080/ui>, click a suggested query, watch it run.

**When you're done:** to stop AND wipe persisted session state (recommended
between demos so old sessions don't auto-resume), use the `-v` flag:

```bash
docker compose down -v          # the -v removes the hermes-state volume
```

Plain `docker compose down` keeps the state volume, your previous sessions
will be resumed on the next startup. That's a feature (durability across
restarts), but a footgun if you want a clean slate.

### Option 2, Local Python (no Docker)

Faster iteration when you're editing code. Two terminals required: one for
the MCP server, one for the agent API.

**Prereqs:** Python 3.11+ (`python --version` to check).

```bash
cd hermes_framework                                # ← critical, see note below
python -m venv .venv
.venv\Scripts\activate                             # Windows
# source .venv/bin/activate                        # macOS/Linux
pip install -e ".[dev]"

cp .env.example .env                               # then edit .env, set ANTHROPIC_API_KEY=...
```

Then in **Terminal A**:

```bash
cd hermes_framework
python -m app.mcp_server
```

And in **Terminal B**:

```bash
cd hermes_framework
python -m app.main
```

Open <http://localhost:8080/ui>.

> **The `cd hermes_framework` matters in BOTH terminals.** `.env` is loaded
> relative to your current directory. Running from one level up causes:
> `TypeError: Could not resolve authentication method. Expected either
> api_key or auth_token to be set.` That's not a code bug, it just means
> the `.env` wasn't found.

### Option 3, Run tests only

If you just want to verify the codebase compiles and the load-bearing logic
works without spinning up the stack or burning API tokens:

```bash
cd hermes_framework
pip install -e ".[dev]"
python -m pytest -q              # 37 tests, ~20s, zero LLM calls
```

The suite covers sandbox AST policy, `validate_and_repair` auto-repair, DAG
scheduler topology, SSE replay, marker parsing, and the resume-on-startup
plumbing. LLM-dependent paths are exercised via the live demo, not unit tests.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/sessions` | Create session. Body: `{container_id?, webhook_url?}`. Returns `{session_id, status}`. |
| POST | `/v1/sessions/{id}/messages` | Send the user message. Body: `{message, container_id?}`. Returns 202. |
| GET | `/v1/sessions/{id}/events?cursor=N` | SSE stream of agent events. Replayable from any cursor. |
| GET | `/v1/sessions/{id}/status` | Polling snapshot, status, task counts, progress %, latest event cursor. Cheap; use when SSE can't be held open. |
| POST | `/v1/sessions/{id}/answer` | Answer a Plan Mode clarifying question. Body: `{question_id, answer}`. |
| GET | `/v1/sessions/{id}` | Session snapshot + question list. |
| GET | `/artifacts/{session_id}/{file}` | Static-serves task artifacts (HTML dashboards, JSON spills). Synthesizer emits absolute URLs to this path. |
| GET | `/ui` | Single-page HTML viewer, dark-first, markdown-rendered answers, polling fallback. |
| GET | `/healthz` | Liveness probe. |

**Optional `webhook_url`** posted on session creation: receives the final
`session.completed` / `session.error` payload via HTTP POST when the
session terminates. Fire-and-forget with retry, max 3 attempts. Useful for
multi-hour jobs that outlive the user's SSE connection.

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
subprocess, the planner LLM is never in the per-document loop.

---

## Observability

- **Live UI**: open `/ui`, every `task.started` / `task.mcp_call` /
  `task.code_progress` / `task.completed` / `plan.repaired` event shows up in
  real time. Full event log at the bottom of the page.
- **Logs**: structured via loguru on stdout. Includes `cache_read_input_tokens`
  / `cache_creation_input_tokens` for the planner so you can see prompt-cache
  hit rate.
- **Event log**: every event is persisted to SQLite. Late SSE subscribers
  pass `?cursor=N` to replay from any point, useful for reconnects.
- **Checkpoints**: `##CHECKPOINT##` markers from sandbox scripts are persisted
  to `task.checkpoint` and passed to the replanner on failure so bulk jobs
  can resume from their last known offset.

---

## Layout

```
hermes_framework/
├── README.md, ARCHITECTURE_VIEW.md
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
   both removed, the former broke module loading on Python 3.11+, the latter
   killed the process at startup due to virtual address space reservation.
   The heartbeat watchdog (below) is the liveness backstop.

7. **Heartbeat-based liveness, not a hard timeout.** For bulk tasks
   (`CODE_TRANSFORM` / `BULK_TOOL_CALL`) the sandbox is killed only after
   `sandbox_heartbeat_timeout_seconds` (default 300 s) of stdout silence,
   not after a fixed wall-clock duration. A real 17-hour translation runs
   to completion as long as `##PROGRESS##` or `##CHECKPOINT##` keeps firing;
   a genuinely hung script dies in 5 minutes. The 24-hour
   `sandbox_bulk_timeout_seconds` is just a backstop.

8. **DAG scheduler with bounded replan + INTERRUPTED status.** Tasks run in
   topological waves with concurrency cap of 8. Failed tasks retry with
   exponential backoff. After terminal failure, the orchestrator replans
   once (`MAX_REPLANS = 1`) with checkpoint context. A new `INTERRUPTED`
   status marks tasks that were RUNNING when the process died; the scheduler
   promotes them back to PENDING (preserving checkpoint) on resume.

9. **Durable execution across process restarts.** Every `##CHECKPOINT##`
   marker is persisted to `tasks.checkpoint_json` immediately (not just
   at task end). On startup, the FastAPI lifespan calls
   `resume_interrupted_sessions()`, any session left in EXECUTING is
   re-executed from its last checkpoint, with full upstream artifact data
   reloaded so downstream tasks see real data not just an inline preview.

10. **Hybrid streaming UX.** Three channels for the same terminal payload:
    SSE for live UIs, `GET /v1/sessions/{id}/status` for polling clients
    that can't hold connections open, and optional `webhook_url` for
    fire-on-done. Pick what fits the consumer's latency / connection model.

11. **Clickable absolute artifact URLs.** The synthesizer prompt is strict
    about markdown style (no emojis, clean tables, no backticks around URLs).
    A post-processor (`_absolutize_artifact_paths`) rewrites any bare
    `artifacts/...` paths to full `http://host/artifacts/...` URLs. The
    `/artifacts/*` route static-serves the directory so users click straight
    to the report HTML.

12. **Resumable & replayable.** Every event appended to SQLite (WAL); SSE
    clients reconnect via `?cursor=N`. Plans, tasks, questions, and
    checkpoints are all persisted.

---

## Known limitations

- The MCP server runs `Sample_FastMCP.py` as-is, the bulk-translate tool has
  a hardcoded ~3% simulated failure rate. That's expected; failures are reported
  in the task output with full document ID lists.
- The sandbox AST policy is defense-in-depth for a trusted-but-confused LLM,
  not a hardened security boundary. For production, replace with E2B or
  Firecracker.
- Single-tenant demo, one Anthropic API key, no per-request auth.
- Retrieval is BM25 (FTS5 `search_documents`), not vector-based. There is
  no embedding model, no vector store. For semantic recall, add a
  `search_documents_semantic` MCP tool backed by PgVector or Milvus.
- Resume-on-startup recovers a single-process orchestrator from uvicorn
  restart. For multi-process / multi-host orchestration replace the
  in-process loop with Temporal or Argo Workflows; the state model (Store)
  is the only seam that needs to change.
- MCP server is single-replica in the demo compose. `Sample_FastMCP.py`
  reads SQLite read-only so N replicas behind nginx round-robin work
  without code changes, that's a deployment swap, not a code one.
- Each `POST /messages` starts a fresh orchestrator run, no multi-turn
  conversation history across messages within a session.
- Windows: `resource.setrlimit` unavailable; heartbeat watchdog is the
  enforced liveness check.
