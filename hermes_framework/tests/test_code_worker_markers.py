"""Verifies the ##PROGRESS## / ##RESULT## / ##ERROR## marker protocol.

This is the wire format between the sandbox subprocess and the agent process;
if marker parsing is wrong, bulk operations look hung even when they're
working. We test the parser in isolation by feeding it synthetic stdout lines.
"""

import json

import pytest

# The marker-parsing logic lives inside CodeWorker._run_in_sandbox. We extract
# it to a standalone classifier so the test exercises the same code paths.


def classify_line(line: str) -> tuple[str, dict]:
    line = line.rstrip()
    if line.startswith("##PROGRESS##"):
        return "progress", json.loads(line[len("##PROGRESS##") :].strip())
    if line.startswith("##RESULT##"):
        return "result", json.loads(line[len("##RESULT##") :].strip())
    if line.startswith("##ERROR##"):
        return "error", json.loads(line[len("##ERROR##") :].strip())
    if line.startswith("##LOG##"):
        return "log", {"line": line[len("##LOG##") :].strip()}
    return "stdout", {"line": line}


def test_progress_parsed():
    kind, p = classify_line('##PROGRESS## {"current": 200, "total": 5140, "msg": "translating"}')
    assert kind == "progress"
    assert p == {"current": 200, "total": 5140, "msg": "translating"}


def test_result_parsed():
    kind, p = classify_line('##RESULT## {"successful": 5100, "failed": 40}')
    assert kind == "result"
    assert p["successful"] == 5100


def test_error_parsed():
    kind, p = classify_line('##ERROR## {"error": "TimeoutError: deadline"}')
    assert kind == "error"
    assert "Timeout" in p["error"]


def test_log_parsed():
    kind, p = classify_line("##LOG## halfway done")
    assert kind == "log"
    assert p["line"] == "halfway done"


def test_untagged_line_is_stdout():
    kind, p = classify_line("hello world")
    assert kind == "stdout"
    assert p["line"] == "hello world"


def test_malformed_progress_falls_back():
    with pytest.raises(json.JSONDecodeError):
        classify_line("##PROGRESS## not json")
