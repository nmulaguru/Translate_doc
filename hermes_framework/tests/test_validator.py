"""The validator is the second line of defense for the 20-doc rule.

The planner system prompt forbids TOOL_CALL with >20 doc IDs, but Sonnet 4.6
will violate the rule occasionally. The validator must auto-rewrite the
offending task as CODE_TRANSFORM and emit a `plan.repaired` record.
"""

import pytest

from app.engine.planner import validate_and_repair
from app.models import Plan, Task, TaskKind


def _make_plan(tasks: list[Task]) -> Plan:
    return Plan(session_id="s_test", goal="test", tasks=tasks)


def test_small_tool_call_is_kept():
    plan = _make_plan(
        [
            Task(
                id="T1",
                kind=TaskKind.TOOL_CALL,
                title="small bulk",
                spec={
                    "tool": "translate_document_preserving_structure",
                    "args": {
                        "document_id": [f"doc_{i:03}" for i in range(10)],
                        "destinationLanguageThreeLetterCode": "deu",
                        "container_id": "c1",
                    },
                },
            )
        ]
    )
    out, repairs = validate_and_repair(plan)
    assert repairs == []
    assert out.tasks[0].kind == TaskKind.TOOL_CALL


def test_oversized_tool_call_is_rewritten():
    plan = _make_plan(
        [
            Task(
                id="T1",
                kind=TaskKind.TOOL_CALL,
                title="huge bulk",
                spec={
                    "tool": "translate_document_preserving_structure",
                    "args": {
                        "document_id": [f"doc_{i:04}" for i in range(5000)],
                        "destinationLanguageThreeLetterCode": "deu",
                        "container_id": "c1",
                    },
                },
            )
        ]
    )
    out, repairs = validate_and_repair(plan)
    assert len(repairs) == 1
    assert out.tasks[0].kind == TaskKind.CODE_TRANSFORM
    assert out.tasks[0].timeout_s >= 600
    assert "5000" in repairs[0]["reason"]


def test_dep_on_unknown_task_raises():
    plan = _make_plan(
        [
            Task(id="T1", kind=TaskKind.RAG_QUERY, title="ask", spec={"prompt": "hi"}),
            Task(
                id="T2",
                kind=TaskKind.SYNTHESIZE,
                title="finish",
                spec={},
                depends_on=["does_not_exist"],
            ),
        ]
    )
    with pytest.raises(ValueError):
        validate_and_repair(plan)


def test_cycle_detection():
    plan = _make_plan(
        [
            Task(id="T1", kind=TaskKind.RAG_QUERY, title="a", spec={}, depends_on=["T2"]),
            Task(id="T2", kind=TaskKind.RAG_QUERY, title="b", spec={}, depends_on=["T1"]),
        ]
    )
    with pytest.raises(ValueError):
        validate_and_repair(plan)


def test_duplicate_ids_raises():
    plan = _make_plan(
        [
            Task(id="T1", kind=TaskKind.RAG_QUERY, title="a", spec={}),
            Task(id="T1", kind=TaskKind.RAG_QUERY, title="b", spec={}),
        ]
    )
    with pytest.raises(ValueError):
        validate_and_repair(plan)
