from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from app.config import settings
from app.engine.prompts import CODE_GEN_TOOL, build_code_gen_system
from app.llm.anthropic_client import get_async_client
from app.mcp_client.client import list_tools as mcp_list_tools
from app.models import Task, TaskKind
from app.sandbox.policy import PolicyViolation, check_script
from app.workers.base import Worker, WorkerContext

# Tasks whose scripts iterate over docs — get the heartbeat watchdog instead
# of a hard wall-clock deadline. The hard ceiling is still applied as a
# backstop against truly stuck processes.
_BULK_KINDS = {TaskKind.CODE_TRANSFORM, TaskKind.BULK_TOOL_CALL}


class _SandboxLivenessError(Exception):
    """Raised by the watchdog when the sandbox is silent or over the hard
    ceiling. Distinct type so the gather handler doesn't catch unrelated
    exceptions from the readers."""

_PROGRESS_MARKER = "##PROGRESS##"
_RESULT_MARKER = "##RESULT##"
_ERROR_MARKER = "##ERROR##"
_LOG_MARKER = "##LOG##"
_MCP_CALL_MARKER = "##MCP_CALL##"
_MCP_RESULT_MARKER = "##MCP_RESULT##"
_FILTER_MARKER = "##FILTER##"
_EXEC_PLAN_MARKER = "##EXEC_PLAN##"
_CHECKPOINT_MARKER = "##CHECKPOINT##"


class CodeWorker(Worker):
    """Generates Python via Claude, runs it in a subprocess sandbox, parses markers.

    The script is constrained by `CODE_GEN_SYSTEM` and the AST policy in
    `app.sandbox.policy`. The runner emits `##PROGRESS##`, `##RESULT##`,
    `##ERROR##`, `##LOG##` markers on stdout — we parse them line-by-line and
    fan them out as SSE events. The final `##RESULT##` becomes the task output.
    """

    name = "code"

    async def execute(self, ctx: WorkerContext, task: Task) -> Any:
        script = await self._generate_script(ctx, task)
        await ctx.bus.emit(
            ctx.session_id,
            "task.code_generated",
            {"task_id": task.id, "lines": script.count("\n") + 1, "preview": script[:400]},
        )

        try:
            check_script(script)
        except PolicyViolation as e:
            raise RuntimeError(f"generated code violates sandbox policy: {e}") from e

        # Pre-flight: compile the script in-process to catch SyntaxError,
        # IndentationError, and a handful of compile-time semantic errors
        # (return-outside-function, duplicate parameter names, etc.) BEFORE
        # spinning up the sandbox subprocess. Saves ~1-2 s per syntax bug
        # and produces a fatal-classed error that skips retries (see Fix A
        # in scheduler.py `_FATAL_HINTS`). Falls through to the existing
        # sandbox path on success.
        try:
            compile(script, "<sandbox>", "exec")
        except (SyntaxError, ValueError) as e:
            # ValueError covers a small set of compile-time edge cases like
            # null bytes in source. Both are deterministic — retrying with
            # the same script is pointless; only a replan helps.
            raise RuntimeError(
                f"generated code does not compile: {type(e).__name__}: {e}"
            ) from e

        return await self._run_in_sandbox(ctx, task, script)

    async def _generate_script(self, ctx: WorkerContext, task: Task) -> str:
        client = get_async_client()

        # Fetch live tool schemas so the code-gen system prompt always reflects the
        # actual MCP server capabilities — never relies on hardcoded signatures.
        try:
            live_tools = await mcp_list_tools()
        except Exception:  # noqa: BLE001
            live_tools = []
        system = build_code_gen_system(live_tools)

        intent = task.spec.get("code_intent", "")
        expected = task.spec.get("expected_output_schema", {})
        upstream = {
            k: ctx.upstream_outputs[k]
            for k in task.depends_on
            if k in ctx.upstream_outputs
        }
        upstream_brief = self._brief_upstream(upstream)

        user = (
            f"container_id = {ctx.container_id!r}\n\n"
            f"Task intent:\n{intent}\n\n"
            f"Expected output schema: {json.dumps(expected)}\n\n"
            f"Upstream task outputs (you can reference these via __upstream__ dict):\n"
            f"{upstream_brief}\n\n"
            f"Generate the Python script via the emit_code tool."
        )

        msg = await client.messages.create(
            model=settings.planner_model,
            max_tokens=4000,
            system=system,
            tools=[CODE_GEN_TOOL],
            tool_choice={"type": "tool", "name": "emit_code"},
            messages=[{"role": "user", "content": user}],
        )
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "emit_code":
                return block.input["script"]  # type: ignore[index]
        raise RuntimeError("code generator did not emit a script")

    @staticmethod
    def _brief_upstream(upstream: dict[str, Any]) -> str:
        if not upstream:
            return "(none)"
        lines = []
        for tid, out in upstream.items():
            if isinstance(out, dict):
                keys = list(out.keys())[:6]
                lines.append(f"{tid}: dict keys={keys}")
            elif isinstance(out, list):
                lines.append(f"{tid}: list len={len(out)}")
            elif isinstance(out, str):
                lines.append(f"{tid}: str len={len(out)}")
            else:
                lines.append(f"{tid}: {type(out).__name__}")
        return "\n".join(lines)

    async def _run_in_sandbox(
        self,
        ctx: WorkerContext,
        task: Task,
        script: str,
    ) -> Any:
        upstream = {
            k: ctx.upstream_outputs[k]
            for k in task.depends_on
            if k in ctx.upstream_outputs
        }
        # `__resume_from__` is populated when this task is a retry/replan of
        # a previously-interrupted bulk task. The generated script reads its
        # last saved {page, offset, partial_results} from here and skips
        # already-processed pages. See prompts.py Pattern F.
        resume_from = task.checkpoint or {}
        # Also expose upstream checkpoints under T_PRIOR for the canonical
        # resume pattern used in the planner's worked examples.
        upstream_with_prior = dict(upstream)
        if resume_from and "T_PRIOR" not in upstream_with_prior:
            upstream_with_prior["T_PRIOR"] = {"checkpoint": resume_from}

        injected_globals = {
            "__upstream__": upstream_with_prior,
            "__container_id__": ctx.container_id,
            "__available_containers__": list(getattr(ctx, "available_containers", []) or []),
            "__resume_from__": resume_from,
        }

        # Write to a NamedTemporaryFile that we delete ourselves; on Windows the
        # default delete=True breaks because the child reopens the path.
        fd, path = tempfile.mkstemp(prefix=f"hermes-{task.id}-", suffix=".json")
        os.close(fd)
        task_file = Path(path)
        task_file.write_text(
            json.dumps({"script": script, "globals": injected_globals}, default=str),
            encoding="utf-8",
        )

        project_root = Path(__file__).resolve().parent.parent.parent
        cmd = [
            sys.executable,
            "-m",
            "app.sandbox.runner",
            "--task-file",
            str(task_file),
            "--session",
            ctx.session_id,
        ]
        env = os.environ.copy()
        env["SANDBOX_RSS_MB"] = str(settings.sandbox_rss_mb)
        env["MCP_URL"] = settings.mcp_url
        # `-I` (isolated mode) was previously here for defense-in-depth, but on
        # Python 3.11+ it implies `-P`, which strips CWD from sys.path — and
        # `-m app.sandbox.runner` then can't find the `app` package. The
        # subprocess died during module loading with no ##ERROR## marker.
        # The AST allowlist + filtered_builtins are the real sandbox defense;
        # `-I` only provided marginal extra isolation. Set PYTHONPATH explicitly
        # so the package is importable regardless of CWD or Python version.
        env["PYTHONPATH"] = str(project_root)

        await ctx.bus.emit(
            ctx.session_id,
            "task.code_executing",
            {"task_id": task.id, "timeout_s": task.timeout_s},
        )

        # `limit` raises the asyncio StreamReader buffer well above the 64 KB
        # default — without this, a single ##RESULT## line carrying ~10K doc
        # dicts blows up with "Separator is not found, and chunk exceed the
        # limit". 50 MB is plenty for any sane task output and still bounded
        # so a runaway script can't OOM the parent.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            limit=50 * 1024 * 1024,
        )

        result: Any = None
        error: Optional[str] = None
        latest_checkpoint: Optional[dict] = None
        stderr_lines: list[str] = []
        STDERR_KEEP = 50  # last N lines included in the failure RuntimeError

        # Heartbeat watchdog state. Every marker line we parse bumps
        # last_activity. If silence exceeds the heartbeat threshold the
        # watchdog kills the process. For bulk tasks the wall-clock
        # task.timeout_s is a 24h backstop, not the real liveness check.
        start_time = time.monotonic()
        last_activity = time.monotonic()
        is_bulk = task.kind in _BULK_KINDS
        heartbeat_s = settings.sandbox_heartbeat_timeout_seconds if is_bulk else None

        async def _read_stdout() -> None:
            nonlocal result, error, latest_checkpoint, last_activity
            assert proc.stdout is not None
            async for raw in proc.stdout:
                last_activity = time.monotonic()
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                if line.startswith(_CHECKPOINT_MARKER):
                    try:
                        latest_checkpoint = json.loads(line[len(_CHECKPOINT_MARKER):].strip())
                        task.checkpoint = latest_checkpoint
                        # Persist immediately — this is the durability hook
                        # that makes resume-on-startup actually work. Without
                        # this, a crash mid-task loses the offset. Skipped
                        # if ctx.store is None (test contexts).
                        if ctx.store is not None:
                            try:
                                await ctx.store.update_task_checkpoint(task.id, latest_checkpoint)
                            except Exception as e:  # noqa: BLE001
                                logger.warning(f"[checkpoint persist failed] {task.id}: {e}")
                        await ctx.bus.emit(
                            ctx.session_id,
                            "task.checkpoint",
                            {"task_id": task.id, "checkpoint": latest_checkpoint},
                        )
                    except json.JSONDecodeError:
                        pass
                elif line.startswith(_PROGRESS_MARKER):
                    try:
                        payload = json.loads(line[len(_PROGRESS_MARKER):].strip())
                    except json.JSONDecodeError:
                        payload = {"raw": line}
                    payload["task_id"] = task.id
                    await ctx.bus.emit(ctx.session_id, "task.code_progress", payload)
                elif line.startswith(_MCP_CALL_MARKER):
                    try:
                        payload = json.loads(line[len(_MCP_CALL_MARKER):].strip())
                    except json.JSONDecodeError:
                        continue
                    payload["task_id"] = task.id
                    await ctx.bus.emit(ctx.session_id, "task.mcp_call", payload)
                elif line.startswith(_MCP_RESULT_MARKER):
                    try:
                        payload = json.loads(line[len(_MCP_RESULT_MARKER):].strip())
                    except json.JSONDecodeError:
                        continue
                    payload["task_id"] = task.id
                    await ctx.bus.emit(ctx.session_id, "task.mcp_result", payload)
                elif line.startswith(_FILTER_MARKER):
                    try:
                        payload = json.loads(line[len(_FILTER_MARKER):].strip())
                    except json.JSONDecodeError:
                        continue
                    payload["task_id"] = task.id
                    await ctx.bus.emit(ctx.session_id, "task.filter_summary", payload)
                elif line.startswith(_EXEC_PLAN_MARKER):
                    try:
                        payload = json.loads(line[len(_EXEC_PLAN_MARKER):].strip())
                    except json.JSONDecodeError:
                        continue
                    payload["task_id"] = task.id
                    await ctx.bus.emit(ctx.session_id, "task.execution_plan", payload)
                elif line.startswith(_RESULT_MARKER):
                    try:
                        result = json.loads(line[len(_RESULT_MARKER):].strip())
                    except json.JSONDecodeError as e:
                        error = f"bad ##RESULT## json: {e}"
                elif line.startswith(_ERROR_MARKER):
                    try:
                        err_payload = json.loads(line[len(_ERROR_MARKER):].strip())
                        error = err_payload.get("error", "unknown")
                    except json.JSONDecodeError:
                        error = line
                elif line.startswith(_LOG_MARKER):
                    await ctx.bus.emit(
                        ctx.session_id,
                        "task.code_stdout",
                        {"task_id": task.id, "line": line[len(_LOG_MARKER):].strip()},
                    )
                else:
                    # untagged stdout — useful for ad-hoc debug but rate-limited
                    await ctx.bus.emit(
                        ctx.session_id,
                        "task.code_stdout",
                        {"task_id": task.id, "line": line[:500]},
                    )

        async def _read_stderr() -> None:
            nonlocal last_activity
            assert proc.stderr is not None
            emitted = 0
            async for raw in proc.stderr:
                last_activity = time.monotonic()
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                stderr_lines.append(line)
                # Keep the captured buffer bounded — only the tail is included
                # in the failure RuntimeError.
                if len(stderr_lines) > STDERR_KEEP * 2:
                    del stderr_lines[: STDERR_KEEP]
                logger.warning(f"[sandbox stderr {task.id}] {line}")
                # Fan out the first ~30 lines as SSE events for the UI; after
                # that they only stay in the log + the failure error.
                if emitted < 30:
                    await ctx.bus.emit(
                        ctx.session_id,
                        "task.code_stderr",
                        {"task_id": task.id, "line": line[:500]},
                    )
                    emitted += 1

        # Watchdog: polls every 5s. Raises if either the hard ceiling or the
        # heartbeat threshold is exceeded; gather propagates the exception
        # and the outer block kills the process. Exits cleanly when the
        # subprocess finishes on its own.
        async def _watchdog() -> None:
            while True:
                await asyncio.sleep(5.0)
                if proc.returncode is not None:
                    return
                now = time.monotonic()
                if task.timeout_s and (now - start_time) > task.timeout_s:
                    raise _SandboxLivenessError(
                        f"hard ceiling {task.timeout_s}s exceeded"
                    )
                if heartbeat_s and (now - last_activity) > heartbeat_s:
                    silent_s = int(now - last_activity)
                    raise _SandboxLivenessError(
                        f"no progress markers for {silent_s}s "
                        f"(heartbeat threshold {heartbeat_s}s)"
                    )

        try:
            await asyncio.gather(
                _read_stdout(), _read_stderr(), proc.wait(), _watchdog()
            )
        except _SandboxLivenessError as liveness_err:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            tail = "\n".join(stderr_lines[-10:])
            if latest_checkpoint is not None:
                task.checkpoint = latest_checkpoint
            cp_hint = ""
            if isinstance(latest_checkpoint, dict):
                cp_keys = {k: latest_checkpoint.get(k) for k in ("page", "offset", "processed") if k in latest_checkpoint}
                if cp_keys:
                    cp_hint = f" | checkpoint saved: {cp_keys}"
            raise RuntimeError(
                f"sandbox liveness: {liveness_err}{cp_hint}"
                + (f"\nstderr tail:\n{tail}" if tail else "")
            ) from None
        finally:
            try:
                task_file.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-STDERR_KEEP:])
            raise RuntimeError(
                f"sandbox exited {proc.returncode}: "
                f"{error or '(no ##ERROR## marker)'}"
                + (f"\nstderr tail:\n{tail}" if tail else "")
            )
        if error is not None:
            raise RuntimeError(f"sandbox error: {error}")
        if result is None:
            raise RuntimeError("sandbox produced no ##RESULT## marker")
        return result
