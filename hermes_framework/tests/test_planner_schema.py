"""Plan schema sanity: load a fixture plan, coerce into Task objects, run it
through the validator. This catches schema drift between the prompt's example
plans and the Pydantic model.
"""

import json
from pathlib import Path

from app.engine.planner import coerce_task, validate_and_repair
from app.models import Plan


def test_fixture_plan_loads_and_validates():
    fixture = Path(__file__).parent / "fixtures" / "sample_plan.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    tasks = [coerce_task(t) for t in raw["tasks"]]
    plan = Plan(session_id="sess_test", goal=raw["goal"], tasks=tasks)
    out, repairs = validate_and_repair(plan)
    assert out is plan
    assert repairs == []
    assert len(out.tasks) == len(raw["tasks"])
