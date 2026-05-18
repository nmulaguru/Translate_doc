"""Sandbox runner integration test.

We spawn the runner as a real subprocess (matching what code_worker does) with
a hand-written script. This catches the silent-failure class of bugs that
killed the bulk-translate path: the runner exiting with code 1 and no
##ERROR## marker.

These tests don't need an API key or the MCP server — they exercise the
runner + policy + filtered builtins purely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = [sys.executable, "-m", "app.sandbox.runner"]


def _run_script(script: str, globals_: dict | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="hermes-test-")
    os.close(fd)
    Path(path).write_text(
        json.dumps({"script": script, "globals": globals_ or {}}), encoding="utf-8"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    try:
        return subprocess.run(
            RUNNER + ["--task-file", path, "--session", "test"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
            env=env,
        )
    finally:
        Path(path).unlink(missing_ok=True)


def _markers(stdout: str) -> dict[str, list]:
    out = {"PROGRESS": [], "RESULT": [], "ERROR": [], "LOG": [], "FILTER": [], "EXEC_PLAN": []}
    for line in stdout.splitlines():
        for k in out:
            if line.startswith(f"##{k}##"):
                out[k].append(line[len(f"##{k}##"):].strip())
    return out


def test_filter_and_exec_plan_markers_round_trip():
    """The new visibility helpers must propagate through subprocess stdout
    so the code_worker can parse them and the UI can render them."""
    script = """
import asyncio
import json

async def main():
    _emit_plan(["fetch x2", "translate x2"], {"metadata_fetch": 2, "translate": 2, "mode": "gather"})
    _emit_filter("container_001", scanned=9000, kept=2570, predicate="category in {business,legal}")
    _emit_filter("container_002", scanned=9000, kept=2570, predicate="category in {business,legal}")
    _emit_progress(1, 1, "done")
    _emit_result({"ok": True})

asyncio.run(main())
"""
    proc = _run_script(script)
    assert proc.returncode == 0, f"stderr was:\n{proc.stderr}"
    m = _markers(proc.stdout)
    assert len(m["EXEC_PLAN"]) == 1
    plan = json.loads(m["EXEC_PLAN"][0])
    assert plan["concurrency"]["metadata_fetch"] == 2
    assert len(m["FILTER"]) == 2
    f1 = json.loads(m["FILTER"][0])
    assert f1["container_id"] == "container_001"
    assert f1["scanned"] == 9000
    assert f1["kept"] == 2570
    assert "business" in f1["predicate"]
    assert m["RESULT"] == ['{"ok": true}']
    assert m["ERROR"] == []


def test_hello_world_round_trip():
    """The most basic check: emit a result, exit 0."""
    script = """
import asyncio

async def main():
    _emit_result({"hello": "world"})

asyncio.run(main())
"""
    proc = _run_script(script)
    assert proc.returncode == 0, f"stderr was:\n{proc.stderr}"
    m = _markers(proc.stdout)
    assert m["RESULT"] == ['{"hello": "world"}']
    assert m["ERROR"] == []


def test_upstream_globals_visible():
    """The injected __upstream__ must be readable from inside main()."""
    script = """
import asyncio

async def main():
    rows = __upstream__["T1"]["rows"]
    _emit_result({"row_count": len(rows), "first": rows[0]})

asyncio.run(main())
"""
    proc = _run_script(script, globals_={"__upstream__": {"T1": {"rows": [{"id": "a"}, {"id": "b"}]}}})
    assert proc.returncode == 0, f"stderr was:\n{proc.stderr}"
    m = _markers(proc.stdout)
    assert m["ERROR"] == []
    assert m["RESULT"], "no ##RESULT## emitted"
    result = json.loads(m["RESULT"][-1])
    assert result["row_count"] == 2


def test_progress_helper_works():
    """_emit_progress should produce ##PROGRESS## markers."""
    script = """
import asyncio

async def main():
    for i in range(1, 4):
        _emit_progress(i, 3, f"step {i}")
    _emit_result({"done": True})

asyncio.run(main())
"""
    proc = _run_script(script)
    assert proc.returncode == 0, f"stderr was:\n{proc.stderr}"
    m = _markers(proc.stdout)
    assert len(m["PROGRESS"]) == 3
    payloads = [json.loads(p) for p in m["PROGRESS"]]
    assert payloads[0]["current"] == 1 and payloads[0]["total"] == 3


def test_runtime_error_emits_error_marker():
    """When the script raises, ##ERROR## must be emitted and stderr must
    contain a traceback. This is the regression test for the silent-failure
    bug that hid 'sandbox exited 1: (no ##ERROR## marker)' for hours."""
    script = """
import asyncio

async def main():
    rows = __upstream__["T1"]["rows"]   # KeyError if no T1
    _emit_result({"ok": True})

asyncio.run(main())
"""
    proc = _run_script(script, globals_={"__upstream__": {}})
    assert proc.returncode == 1
    m = _markers(proc.stdout)
    assert m["ERROR"], (
        "##ERROR## must be emitted on uncaught exception. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "KeyError" in m["ERROR"][0]
    assert "KeyError" in proc.stderr  # traceback dumped to stderr


def test_system_exit_emits_error_marker():
    """SystemExit must also surface ##ERROR##, not vanish."""
    script = """
import asyncio

async def main():
    raise SystemExit(7)

asyncio.run(main())
"""
    proc = _run_script(script)
    assert proc.returncode == 7
    m = _markers(proc.stdout)
    assert m["ERROR"], "##ERROR## must be emitted on SystemExit"


def test_reassigning_upstream_is_legal_but_clobbers():
    """Sanity: if the script (against the rules) does `__upstream__ = {}` it
    can no longer see the injected data. We want this to fail loudly with a
    proper ##ERROR## — which is what guards us against silent broken plans."""
    script = """
import asyncio

__upstream__ = {}   # this is the bug we're guarding against

async def main():
    rows = __upstream__["T1"]["rows"]
    _emit_result({"ok": True})

asyncio.run(main())
"""
    proc = _run_script(script, globals_={"__upstream__": {"T1": {"rows": [1, 2]}}})
    assert proc.returncode == 1
    m = _markers(proc.stdout)
    assert m["ERROR"], "must see ##ERROR## marker"
    assert "KeyError" in m["ERROR"][0]


def test_disallowed_import_blocked_by_policy():
    """The AST policy must reject `import os` before exec even runs."""
    script = """
import os

print(os.getcwd())
"""
    proc = _run_script(script)
    assert proc.returncode == 2  # PolicyViolation exit code
    m = _markers(proc.stdout)
    assert m["ERROR"]
    assert "policy" in m["ERROR"][0].lower()


def test_bare_print_falls_through_as_stdout():
    """Non-marker stdout should be visible; the parent treats it as
    task.code_stdout. The runner shouldn't crash on it."""
    script = """
import asyncio

async def main():
    print("hello from the sandbox")
    _emit_result({"ok": True})

asyncio.run(main())
"""
    proc = _run_script(script)
    assert proc.returncode == 0
    assert "hello from the sandbox" in proc.stdout


def test_large_result_does_not_break_runner():
    """Regression for 'Separator is not found, and chunk exceed the limit'
    — a multi-MB ##RESULT## must come through cleanly. The parent's
    create_subprocess_exec uses limit=50MB to handle this; the runner itself
    just writes one big line via json.dumps."""
    script = """
import asyncio

async def main():
    # ~5MB payload (well above asyncio's 64KB default StreamReader limit)
    fake_docs = [{"content_id": f"c_{i:08}", "container_id": "container_001",
                  "document_name": f"doc_{i}.pdf", "category": "financial"}
                 for i in range(50000)]
    _emit_result({"documents": fake_docs, "count": len(fake_docs)})

asyncio.run(main())
"""
    # Use a fatter local limit too (subprocess.run reads everything in one shot
    # so it's not affected, but verify the script itself doesn't crash).
    proc = _run_script(script, timeout=30)
    assert proc.returncode == 0, f"stderr was:\n{proc.stderr[:500]}"
    # The ##RESULT## marker should be present (likely 5-10MB)
    assert "##RESULT##" in proc.stdout
    assert len(proc.stdout) > 2 * 1024 * 1024  # at least 2MB written
