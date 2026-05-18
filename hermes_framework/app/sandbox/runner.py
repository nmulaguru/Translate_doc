"""Sandbox runner — entry point invoked as a subprocess.

Usage:
    python -I -m app.sandbox.runner --task-file <path> [--session <sid>]

The task file is JSON:
    {"script": "<python source>", "globals": {<name>: <value>, ...}}

Diagnostic philosophy: NEVER exit silently. Every non-zero exit path emits a
##ERROR## marker on stdout AND a full traceback to stderr. If you can't see
what failed, fix the runner before fixing anything else — silent failures
waste hours.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Make the project root importable when running with `python -I -m`.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sandbox import mcp_shim  # noqa: E402
from app.sandbox.policy import PolicyViolation, check_script, filtered_builtins  # noqa: E402


def _emit_result(result: Any) -> None:
    sys.stdout.write("##RESULT## " + json.dumps(result, default=str) + "\n")
    sys.stdout.flush()


def _emit_error(msg: str) -> None:
    sys.stdout.write("##ERROR## " + json.dumps({"error": msg}) + "\n")
    sys.stdout.flush()


def _emit_progress(current: int, total: int, msg: str = "") -> None:
    sys.stdout.write(
        "##PROGRESS## " + json.dumps({"current": current, "total": total, "msg": msg}) + "\n"
    )
    sys.stdout.flush()


def _emit_filter(container_id: str, scanned: int, kept: int, predicate: str = "") -> None:
    """Per-container filter summary so the user can see "scanned N, kept M".
    Sandbox scripts should call this once per container after running their
    filter — closes the visibility gap where you only saw the post-filter
    count and never the scan count."""
    sys.stdout.write(
        "##FILTER## " + json.dumps({
            "container_id": container_id,
            "scanned": scanned,
            "kept": kept,
            "predicate": predicate,
        }) + "\n"
    )
    sys.stdout.flush()


def _emit_plan(steps: list, concurrency: dict) -> None:
    """One-line execution plan so the user sees the script's intended
    fan-out before the first MCP call lands. `steps` is a short list of
    human-readable strings; `concurrency` describes the planned parallelism
    (e.g. {"metadata_fetch": 4, "translate": 4, "mode": "asyncio.gather"})."""
    sys.stdout.write(
        "##EXEC_PLAN## " + json.dumps({"steps": list(steps), "concurrency": concurrency}) + "\n"
    )
    sys.stdout.flush()


def _emit_checkpoint(state: dict) -> None:
    """Save intermediate progress so the orchestrator can resume after a timeout.

    Call this after each page/chunk so the latest state survives if the sandbox
    is killed. The orchestrator captures the last checkpoint emitted and passes
    it to the replanner as prior_failure["checkpoint"]. A continuation script
    reads it via __upstream__["T_PRIOR"]["checkpoint"].

    state should contain everything needed to resume: current offset/page,
    any partial accumulated results, and a human-readable "msg" field.

    Example:
        _emit_checkpoint({"page": 3, "partial_docs": results_so_far, "msg": "3/10 pages done"})
    """
    sys.stdout.write("##CHECKPOINT## " + json.dumps(state, default=str) + "\n")
    sys.stdout.flush()


def _dump_traceback(prefix: str) -> None:
    """Always print the traceback to stderr so the parent's stderr capture
    has something useful to show in the failure event."""
    sys.stderr.write(f"[sandbox] {prefix}\n")
    sys.stderr.write(traceback.format_exc())
    sys.stderr.flush()


def run(task_file: Path, session_id: str) -> int:
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _emit_error(f"task_file: {type(e).__name__}: {e}")
        _dump_traceback("task_file load failed")
        return 1

    script: str = payload.get("script", "") or ""
    extra_globals: dict[str, Any] = payload.get("globals", {}) or {}

    if not script.strip():
        _emit_error("empty script in task file")
        return 1

    try:
        check_script(script)
    except PolicyViolation as e:
        _emit_error(f"policy: {e}")
        return 2

    # Make our shim resolvable via the real import machinery too, so that
    # generated `import mcp` statements always bind to our shim and not the
    # official mcp PyPI package (which the runner's own imports loaded into
    # sys.modules earlier in this process).
    sys.modules["mcp"] = mcp_shim

    namespace: dict[str, Any] = {
        "__builtins__": filtered_builtins(),
        "__name__": "__sandbox__",
        "__session_id__": session_id,
        "mcp": mcp_shim,
        "_emit_result": _emit_result,
        "_emit_progress": _emit_progress,
        "_emit_filter": _emit_filter,
        "_emit_plan": _emit_plan,
        "_emit_checkpoint": _emit_checkpoint,
    }
    namespace.update(extra_globals)

    try:
        compiled = compile(script, "<sandbox>", "exec")
        exec(compiled, namespace, namespace)  # noqa: S102 — policy-checked
    except SystemExit as e:
        # An explicit sys.exit() inside the script. Surface it so we know.
        code = int(e.code) if isinstance(e.code, int) else 1
        _emit_error(f"runtime: SystemExit({code})")
        _dump_traceback("script called sys.exit()")
        return code if code != 0 else 1
    except KeyboardInterrupt:
        _emit_error("runtime: KeyboardInterrupt")
        _dump_traceback("script interrupted")
        return 130
    except BaseException as e:  # noqa: BLE001
        # `BaseException` (not Exception) so we also catch things like
        # GeneratorExit / asyncio.CancelledError that aren't subclasses of
        # Exception. The previous code missed these and silently exited 1.
        _emit_error(f"runtime: {type(e).__name__}: {e}")
        _dump_traceback(f"script raised {type(e).__name__}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--session", default="unknown")
    try:
        args = parser.parse_args()
    except SystemExit:
        # argparse on bad args writes its own usage to stderr and exits.
        # Surface that as a ##ERROR## too so the parent doesn't get a silent 2.
        _emit_error("runner: bad arguments")
        return 2

    # NOTE: RLIMIT_AS was previously set here to 512MB on Linux. It was removed
    # because RLIMIT_AS limits virtual address space (not RSS), and modern
    # Python + httpx + the MCP SDK can easily reserve >512MB of virtual
    # address without ever actually using it — this caused the subprocess
    # to die at startup with a fatal Python error that wasn't reachable
    # by our exception handler (silent exit 1). The wall-clock timeout in
    # code_worker is the real safety net.

    try:
        return run(args.task_file, args.session)
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 1
        _emit_error(f"runner: SystemExit({code})")
        _dump_traceback("runner called sys.exit()")
        return code if code != 0 else 1
    except BaseException as e:  # noqa: BLE001
        _emit_error(f"runner: {type(e).__name__}: {e}")
        _dump_traceback(f"runner raised {type(e).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())


# Convenience for direct asyncio context (rare, e.g. tests).
async def run_async(task_file: Path, session_id: str) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run, task_file, session_id)
