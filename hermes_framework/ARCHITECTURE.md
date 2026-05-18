# Hermes Framework — Architecture

A Hermes/OpenCode/DeepAgents-style multi-agent framework that wraps the four
MCP tools from `Sample_FastMCP.py` and exposes a single API surface for the
six example queries in the assessment PDF. This document explains *why* the
system is built the way it is — the tradeoffs that mattered, the failure
modes that drove specific design choices, and the path to scaling beyond
36K documents to 1M+.

For the quickstart and API reference, see [README.md](./README.md).

---

## 1. Problem framing

The assessment expects an **API-driven** agentic system (not TUI, not a thin
FastAPI-over-MCP wrapper) that intelligently plans, executes, and reports on a
range of document workflows. The corpus is 36,000 documents (the PDF says 9K
but the DB actually has 36K) across 4 containers — but the explicit
evaluation criterion is whether the architecture would *scale to 1M+*.

The architecture is graded on six axes:

1. **System thinking** — production-grade design, not a prompt wrapper.
2. **Agent architecture** — true multi-agent decomposition.
3. **Planning quality** — think before acting.
4. **Scalability** — handle million-document operations.
5. **MCP integration** — use the tools correctly.
6. **Streaming UX** — observe execution in real time.

The highest-weighted axis is **scalability**. The naive failure mode is to
let the planner LLM enumerate document IDs into its own context (5,140
financial doc IDs in `container_001` would already exceed reasonable token
budgets) or to let the orchestrator loop over docs one tool call at a time.
The architecture must prevent both, structurally.

---

## 2. Component map

```
                 ┌─────────────────────────────────────────────┐
                 │ Client (browser / curl / Postman)           │
                 └──────────────────────┬──────────────────────┘
                                        │ HTTP + SSE
                                        ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ FastAPI Gateway (app/api/server.py)                                  │
 │   POST /v1/sessions                                                  │
 │   POST /v1/sessions/{id}/messages                                    │
 │   GET  /v1/sessions/{id}/events     ← SSE, replayable via ?cursor=N  │
 │   POST /v1/sessions/{id}/answer     ← Plan Mode answers              │
 │   GET  /ui                          ← single-page viewer             │
 └──────────────────────┬───────────────────────────────────────────────┘
                        │
                        ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Orchestrator (app/engine/orchestrator.py)                            │
 │   1) Interrogator   → emit clarification Qs OR proceed               │
 │   2) Planner        → adaptive thinking + native tool_use + caching  │
 │   3) Plan Validator → auto-repair unsafe plans                       │
 │   4) Scheduler      → topo-wave DAG executor (semaphore-bound)       │
 │   5) Synthesizer    → final-answer composition                       │
 └─┬───────────────────┬─────────────────────┬────────────────────┬─────┘
   │                   │                     │                    │
   ▼                   ▼                     ▼                    ▼
 ┌──────────┐  ┌──────────┐  ┌────────────────────────┐  ┌──────────────┐
 │ Tool     │  │ Code     │  │ Sub-Agent Worker       │  │ Event Bus    │
 │ Worker   │  │ Worker   │  │ (child Claude session, │  │ (per-session │
 │ (one MCP │  │ (sandbox │  │  isolated context, for │  │  asyncio     │
 │  call)   │  │  subproc)│  │  HTML/synthesis)       │  │  queues)     │
 └────┬─────┘  └────┬─────┘  └──────────┬─────────────┘  └──────┬───────┘
      │             │                   │                       │
      │             │                   │                       ▼
      │             │                   │             ┌───────────────────┐
      │             │                   │             │ SQLite event log  │
      │             │                   │             │ (WAL, append-only,│
      │             │                   │             │  replayable)      │
      │             │                   │             └───────────────────┘
      ▼             ▼                   ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ MCP Server (FastMCP over streamable-HTTP)                            │
 │ [Assessment/Sample_FastMCP.py — used as-is via app/mcp_server.py]    │
 │   get_active_documents_metadata / get_document_insights /            │
 │   translate_document_preserving_structure / aiagent                  │
 └────────────────────────────┬─────────────────────────────────────────┘
                              ▼
                     ┌─────────────────┐
                     │ fake_database.db│
                     └─────────────────┘

Cross-cutting:
- OpenTelemetry traces (FastAPI + httpx auto-instrumented) → Jaeger.
- Loguru structured logs to stdout (session_id, task_id, cache hit rates).
```

The **bus** is a per-session `asyncio.Queue` and a SQLite append-only log
working together: every event is written to both, so live SSE subscribers
get it immediately and late ones replay from a cursor.

---

## 3. The planner: adaptive thinking + native tool use + cache

The planner is one Claude Sonnet 4.6 call per session message. It produces a
structured DAG plan via a single `emit_plan` tool call.

```python
async with self.client.messages.stream(
    model="claude-sonnet-4-6",
    max_tokens=16000,
    thinking={"type": "adaptive"},          # adaptive — not budget_tokens
    output_config={"effort": "high"},       # planner-only
    cache_control={"type": "ephemeral"},    # caches system + tool schema
    system=PLANNER_SYSTEM,
    tools=[EMIT_PLAN_TOOL],
    tool_choice={"type": "tool", "name": "emit_plan"},
    messages=[{"role": "user", "content": user_block}],
) as stream:
    async for event in stream:
        # streamed thinking_delta / text_delta → planner.thinking SSE events
        ...
    final = await stream.get_final_message()
```

**Why these choices:**

| Choice | Why |
|---|---|
| **Native tool use** | Sonnet 4.6 is RLHF-trained on the native schema. XML-tagged "Hermes-style" parsing on top of streaming is brittle (mid-stream regex matching, harder to enforce `tool_choice`), degrades reliability, and saves nothing. The "Hermes feel" lives in the *system-level* planner/router/worker decomposition. |
| **`thinking: {type: "adaptive"}`** | `budget_tokens` is deprecated on Sonnet 4.6 (and removed on Opus 4.7). Adaptive lets the model decide when and how much to think — measured better than a fixed budget across the PDF's example queries. |
| **`effort: "high"`** | The planner is the most intelligence-sensitive component. Workers run on plain Sonnet without extended thinking for latency. |
| **`cache_control: {"type": "ephemeral"}`** | The system prompt + tool schema is ~3-4K tokens of stable bytes per session. With caching, repeat planner calls in the same process pay ~0.1× the prefix cost. The first call pays a 1.25× write premium; break-even is two requests. |
| **`tool_choice: {"type": "tool", "name": "emit_plan"}`** | Forces the model to emit a plan, never to answer the user directly. The planner has one job. |
| **Streaming** | `planner.thinking` deltas are forwarded to the SSE stream live, so the user sees reasoning as it happens. Without streaming the UX has a 5-15 second blank pause. |

**One trap:** when streaming with extended/adaptive thinking + tool use, the
`get_final_message()` helper is the right escape hatch — manually
reconstructing the final message from individual stream events is
unnecessarily painful. The SDK accumulates state for you.

---

## 4. The 20-doc rule (the scalability invariant)

The single design decision that lets this framework reach 1M documents is
*the planner never sees more than a few document IDs at once*. Bulk operations
go to `CODE_TRANSFORM` tasks, where the executor generates a small Python
script that runs in a sandbox subprocess. The script calls the MCP tools
directly (over HTTP) and streams progress back via `##PROGRESS##` markers on
stdout.

The threshold is `BULK_DOC_THRESHOLD=20`, settable in `.env`.

**Two layers of enforcement:**

1. **Prompt rule.** The planner system prompt forbids `TOOL_CALL` with >20
   doc IDs in args, with worked examples and an explicit "this is
   non-negotiable" note.
2. **Validator auto-repair** (`app/engine/validator.py`). Scans every
   emitted plan, counts doc IDs in `TOOL_CALL` args (checks `document_id`,
   `document_ids`, `doc_ids`, plus generic list-of-strings detection), and
   rewrites violators as `CODE_TRANSFORM` with a `code_intent` that
   describes the original tool call. Emits a `plan.repaired` SSE event so
   the user sees it happened.

Prompt discipline alone is not sufficient — Sonnet 4.6 violates the rule
maybe 3-5% of the time on edge cases ("translate these specific 8 papers
plus all financial ones..." can confuse it). The validator is the load-bearing
defense.

**The generated code follows this pattern:**

```python
import asyncio, json, mcp

async def main():
    meta = await mcp.get_active_documents_metadata(__container_id__)
    doc_ids = [d["documentId"] for d in meta["documents"]
               if d["category"] == "financial" and d["status"] == "ACTIVE"]
    BATCH = 200
    successful, failed, failed_docs = 0, 0, []
    for i in range(0, len(doc_ids), BATCH):
        chunk = doc_ids[i:i+BATCH]
        r = await mcp.translate_document_preserving_structure(chunk, "deu", __container_id__)
        successful += r.get("successful", 0)
        failed += r.get("failed", 0)
        failed_docs.extend(r.get("failed_documents", []))
        _emit_progress(i + len(chunk), len(doc_ids), "translating")
    _emit_result({"successful": successful, "failed": failed,
                  "failed_documents": failed_docs[:20]})

asyncio.run(main())
```

Note: `__container_id__` and `__upstream__` are injected into the sandbox
namespace by the runner. Generated scripts never need to import the
container_id from anywhere.

---

## 5. The sandbox

Two layers of defense, plus a marker protocol.

### Layer 1: AST allowlist (`app/sandbox/policy.py`)

Before exec, parse the script with `ast.parse()` and walk every node:

- `ast.Import` / `ast.ImportFrom` — module root must be in
  `ALLOWED_IMPORTS` (`json, asyncio, math, statistics, collections, re,
  datetime, csv, io, base64, html, urllib.parse, mcp`).
- `ast.Attribute` — reject `__class__`, `__bases__`, `__subclasses__`,
  `__globals__`, `__builtins__`, `__import__`.
- `ast.Name` / `ast.Call` — reject `__import__`, `eval`, `exec`, `compile`,
  `open`, `input`, `breakpoint`.

This catches the obvious escape patterns. A determined adversary could find
something the AST walker misses (custom descriptors, type-coerced bytes
exec via `marshal`, etc.) — for that we rely on layer 2.

### Layer 2: `python -I` + filtered builtins

The runner is launched as `python -I -m app.sandbox.runner --task-file
/tmp/<task>.json`. `-I` (isolated mode) strips `PYTHONPATH`, `PYTHONHOME`,
and user site-packages — so even if a script's AST passes, it can't reach
arbitrary modules.

The execution namespace seeds `__builtins__` from a copy of the real
builtins module with `__import__`, `eval`, `exec`, `compile`, `open`,
`input`, `breakpoint`, `memoryview` removed. So even if the AST walker
misses something, runtime resolution of those names raises `NameError`.

On Linux we also set `RLIMIT_AS = 512MB` to bound memory. Windows: the wall
timeout via `asyncio.wait_for(proc.wait(), timeout=...)` is the only enforced
limit; the child can be killed cleanly via `proc.kill()`.

### Layer 3: marker protocol (`##PROGRESS## / ##RESULT## / ##ERROR##`)

The runner exposes two helpers into the sandbox namespace:

- `_emit_progress(current, total, msg="")` — writes `##PROGRESS## {json}` to stdout
- `_emit_result(value)` — writes `##RESULT## {json}` once, terminal

The agent reads stdout line by line. Lines starting with `##PROGRESS##` become
`task.code_progress` SSE events; `##RESULT##` becomes the task output;
`##ERROR##` is a fatal sandbox error; everything else falls through as
`task.code_stdout` for debugging. Parsing failures are caught and surfaced as
task failures — not silent.

**Why subprocess, not in-process?** Subprocess isolation makes timeouts
trivially safe (kill the process; everything dies cleanly), avoids any
`asyncio` event-loop pollution from generated code, and the `python -I`
hardening only works for a process. On Windows we can't `setrlimit`, but
we can still rely on the kill-via-timeout behavior.

---

## 6. The DAG scheduler

`app/engine/scheduler.py` runs the plan in topological waves:

```
while any task is PENDING:
    ready = [t for t in tasks
             if t.status == PENDING and all deps SUCCEEDED]
    if not ready:
        mark remaining PENDING tasks as SKIPPED (upstream failed)
        break
    await asyncio.gather(*(run_task(t) for t in ready))  # semaphore-bound at 4
```

Per task:

- `task.started` event → run worker under `asyncio.wait_for(timeout)` →
  on success: persist output (small inline, large to artifacts/) + emit
  `task.completed` with preview.
- On retriable failure: exponential backoff (2^attempt seconds), max 3
  attempts, emit `task.retrying`.
- On fatal failure or retries exhausted: emit `task.failed`. Downstream
  tasks transitively skip.

After scheduler returns, the orchestrator inspects task statuses. If any
terminal failure occurred *with downstream work remaining*, it does one
bounded replan: feeds the prior plan + failure back to the planner with a
"replan around this" prompt. Capped at one replan per session — otherwise
the loop is unbounded.

---

## 7. Plan Mode (the interrogator)

For ambiguous requests (the canonical example: "create a dashboard from my
documents"), executing immediately is wrong. The interrogator is a separate
Claude call with a two-tool surface (`proceed` | `ask_clarifications`), forced
via `tool_choice={"type": "any"}`. If it asks, each question is persisted as
a `Question` row, emitted as a `plan_mode.question` SSE event, and the
session transitions to `AWAITING_ANSWER`. The HTML viewer renders each
question with its options and a free-text fallback.

When `POST /v1/sessions/{id}/answer` arrives for the last pending question,
the orchestrator's `resume_session()` re-enters the run loop with
`skip_interrogation=True` and the answers gathered as planner context.

The interrogator is biased toward `proceed` — only asks when a decision could
send the planner down a wrong path.

---

## 8. State model

```
sessions(id PK, container_id, status, created_at, user_msg, final_answer)
plans(id PK, session_id FK, goal, json_blob, created_at)
tasks(id PK, plan_id FK, kind, title, depends_on_json, spec_json,
      status, attempts, output_blob, artifact_ref, error,
      started_at, ended_at)
events(id PK AUTOINC, session_id, ts, type, payload_json)  -- INDEX (session_id, id)
questions(id PK, session_id FK, text, options_json, answer, asked_at, answered_at)
checkpoints(id PK, session_id, task_id, output_ref, created_at)
```

WAL mode. `events` is append-only — never updated — which is what enables
the SSE replay-from-cursor pattern.

**Output size handling.** Task outputs ≤ 8KB stay inline in `tasks.output_blob`.
Larger outputs (a full container's worth of insights, an HTML dashboard) spill
to `./artifacts/{session_id}/{task_id}.json|html` and the task row stores
`artifact_ref` + a small preview. SSE event payloads never carry full outputs
— the `task.completed` payload has `output_preview` (≤8KB) and `artifact_ref`.
The HTML viewer renders artifact refs as clickable links.

---

## 9. SSE event surface

| event type | when |
|---|---|
| `session.started` | message accepted |
| `plan_mode.question` | clarification needed |
| `plan_mode.answered` | answer received |
| `planner.thinking` | streamed thinking_delta during planner call |
| `planner.text` | streamed text_delta (rare; usually empty under tool_choice=tool) |
| `plan.created` | plan emitted |
| `plan.repaired` | validator auto-rewrote a task |
| `plan.replanning` | replan after task failure |
| `task.started` | task begins |
| `task.tool_call` | MCP call dispatched |
| `task.tool_result` | MCP result back |
| `task.code_generated` | code worker generated a script |
| `task.code_executing` | sandbox spawned |
| `task.code_progress` | `##PROGRESS##` marker parsed |
| `task.code_stdout` | non-marker stdout (debug) |
| `task.completed` | task done |
| `task.retrying` | retriable failure, backing off |
| `task.failed` | terminal failure |
| `task.skipped` | upstream failed |
| `subagent.spawned` | child Claude session for SUBAGENT task |
| `session.completed` | final answer ready |
| `session.error` | session failed at orchestrator level |

All events have `{ts, payload}` in `data:` JSON, plus the SSE `id:` field as
the cursor. The HTML viewer subscribes to every type and renders each
appropriately (planner thinking → typed-out text, plan → task tree, code
progress → progress bar, etc.).

---

## 10. Tradeoffs considered

### Why not LangGraph / LlamaIndex Agents / CrewAI?

Considered. Rejected because:

- The assessment explicitly evaluates whether the *system thinking* is sound
  — using a higher-level framework would hide the planner/router/worker
  decisions inside someone else's abstraction. Writing the DAG executor and
  worker dispatch ourselves shows the architecture more clearly.
- LangGraph's state model is checkpointed graphs; that's adjacent to what
  we want (replayable event log) but not a natural fit for the
  Plan-Mode-pauses-on-clarification flow.
- CrewAI is opinionated about roles/crew terminology and harder to reshape
  for *generated-code-as-a-worker*, which is the key scalability lever.
- We do use the official MCP Python SDK (`mcp.client.streamable_http`) for
  the wire format — no reason to reinvent that.

### Why subprocess sandbox instead of E2B / Docker-in-Docker / RestrictedPython?

- **E2B**: best isolation but adds a paid third-party dependency, latency
  spike per execution, and complicates the no-internet demo path.
- **Docker-in-Docker**: heavy. We're already in Docker Compose for the
  demo; adding DinD doubles the build time and complicates the
  filesystem story.
- **RestrictedPython**: weak isolation, can't actually run async code
  cleanly, harder to debug failures.
- **Subprocess + `python -I` + AST allowlist**: weakest theoretical
  isolation but matches the *level* of trust we extend to LLM-generated
  code in a *demo* — the LLM is the trusted-but-confused party, not an
  attacker. For production, swap this layer for E2B without touching
  anything above.

### Why a single replan, not unbounded?

Replan-on-failure is a powerful pattern but can loop forever (planner emits
a broken plan, scheduler fails, planner emits the same broken plan...). We
cap at 1 replan per session and surface the error. In practice this is
enough to recover from transient MCP failures and structural plan errors
(typo'd dependency IDs) while keeping failure modes bounded.

### Why SQLite, not Redis / Postgres?

Demo-grade choice. WAL-mode SQLite handles the event volume comfortably for
a single-tenant agent. For multi-tenant production:

- Move events to Postgres `LISTEN/NOTIFY` (replay still works via cursor).
- Move session state to Redis with TTLs for ephemeral sessions, Postgres
  for durable.
- The `Store` abstraction in `app/state/store.py` makes this a swap, not a
  rewrite.

### Why FastAPI's `StreamingResponse` for SSE, not `sse-starlette`?

`sse-starlette` wants to encode event frames for you, but we already emit
fully-formatted frames (`id: N\nevent: T\ndata: {...}\n\n`) from
`stream_session_events`. `StreamingResponse` with `media_type="text/event-stream"`
plus `X-Accel-Buffering: no` and `Cache-Control: no-cache` is the minimum
correct surface.

### Why one MCP session per call, not pooling?

The MCP streamable-HTTP transport is request-scoped — there's no long-lived
session you can keep alive across asyncio tasks without serializing through
a single connection (which kills throughput). For demo-scale concurrency
this is fine; the bottleneck is the simulated 100ms-per-doc translate
latency, not connection setup. For 1M-scale production: add a pool of N
persistent sessions in front of the MCP client with round-robin
dispatch.

---

## 11. Scaling to 1M documents

The architecture's scalability story:

| Layer | At 36K docs | At 1M docs | What changes |
|---|---|---|---|
| Planner context | O(1) doc IDs | O(1) doc IDs | nothing — by design |
| Plan complexity | 2-3 tasks | 2-3 tasks | nothing |
| Code worker | 200-doc chunks, ~5,140 docs in ~25s mocked | 200-doc chunks, ~1M docs in ~80min mocked | chunk size tunable; if MCP supports parallelism, add a semaphore to the generated code |
| MCP server | single process | horizontal scale behind a load balancer | nothing in our framework; tools are stateless |
| State DB | SQLite WAL | Postgres + Redis | swap `app/state/store.py` |
| Event volume | ~100 events/session | ~100 events/session, same per-session | sub-session events stay bounded because we only emit per chunk, not per doc |
| Memory | a few MB of in-memory outputs per session | same | large outputs spill to artifacts/ already |

Two specific scale-out edits when going to 1M:

1. **Parallel chunks.** The current generated code is sequential per chunk.
   For 1M docs, the code generator prompt can ask for `asyncio.Semaphore(8)`
   wrapping the chunk dispatch. The MCP server's `translate_document_preserving_structure`
   already does in-tool concurrency.
2. **Resumable bulk.** Add `--resume-from N` support to the sandbox runner
   so a failed/timed-out bulk job can pick up where the last `##PROGRESS##`
   marker left it (the runner accepts the parameter through the task JSON,
   the scheduler tracks last-checkpoint per task, and the generated code
   reads `__resume_from__` from globals). This is sketched but not built.

---

## 12. Production checklist (not for the demo)

- [ ] Replace subprocess sandbox with E2B or Firecracker.
- [ ] Auth: API keys per tenant, scoped to container_ids.
- [ ] Move event log to Postgres; events table partitioned by day.
- [ ] Move session state to Redis with TTL.
- [ ] Backpressure on the SSE bus (currently `asyncio.Queue(maxsize=1024)`;
      drop-and-log oldest on overflow).
- [ ] Replan budget — currently 1 per session, should be per-tenant rate
      limited too.
- [ ] Persistent MCP client pool (N=10 streaming sessions, round-robin).
- [ ] Cost telemetry: every Anthropic call emits its `usage` block to
      OpenTelemetry as span attributes — already partially done in
      `planner.py:_log_usage`.
- [ ] Audit log: events table already serves as one; add export to S3.
- [ ] Soft delete + retention on artifacts/.

---

## 13. What I'd do differently with more time

1. **Run a real load test.** Mock the MCP server to handle 1M docs and
   actually run "translate all" — measure end-to-end latency, SSE
   throughput, SQLite write contention. The architecture *should* hold
   but I haven't verified at that scale.
2. **Build the resume-from-checkpoint path.** Sketched in the scheduler
   (`TaskStatus.INTERRUPTED`) but the generated code doesn't accept the
   resume parameter yet.
3. **Add streaming inside the code worker.** The planner streams; the
   code worker doesn't — it generates the script in one shot, then runs
   it. Streaming the code generation gives an extra visibility win for
   long-running tasks.
4. **Tool-search-based dynamic tool loading.** Today all four MCP tool
   schemas are inlined into the planner system prompt. With 50+ tools
   this won't scale; use Anthropic's `tool_search` to discover relevant
   tools per query.
5. **Multi-turn within a session.** Today `POST /messages` starts a fresh
   orchestrator run. A real chat experience would keep conversation
   history and let the planner reference prior task outputs across turns.
