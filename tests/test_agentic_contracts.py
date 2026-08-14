from __future__ import annotations

import pytest
from pydantic import ValidationError

from harnesslab.agentic.contracts import ExecutionPlan, PlanStep, RecoveryAction, TaskClass
from harnesslab.agentic.routing import TaskRouter


def test_execution_plan_rejects_cycles_and_unknown_dependencies() -> None:
    with pytest.raises(ValidationError, match="dependency cycle"):
        ExecutionPlan(
            id="cyclic",
            objective="reject cycles",
            steps=[
                PlanStep(id="a", title="A", tool="tool.a", depends_on=["b"]),
                PlanStep(id="b", title="B", tool="tool.b", depends_on=["a"]),
            ],
        )

    with pytest.raises(ValidationError, match="unknown dependencies"):
        ExecutionPlan(
            id="missing",
            objective="reject missing nodes",
            steps=[PlanStep(id="a", title="A", tool="tool.a", depends_on=["missing"])],
        )


def test_replan_requires_an_explicit_fallback() -> None:
    with pytest.raises(ValidationError, match="requires fallback_tool"):
        PlanStep(
            id="search",
            title="Search",
            tool="repository.search",
            on_failure=RecoveryAction.REPLAN,
        )


def test_task_router_explains_simple_and_complex_decisions() -> None:
    router = TaskRouter()
    simple = router.assess("Find the owner of checkout.py")
    complex_task = router.assess(
        "Investigate the production payment failure, apply a fix, then deploy it with rollback."
    )

    assert simple.task_class == TaskClass.SIMPLE
    assert simple.recommended_parallelism == 1
    assert complex_task.task_class == TaskClass.COMPLEX
    assert complex_task.requires_planning is True
    assert {signal.name for signal in complex_task.signals} >= {
        "multi-step-language",
        "side-effect",
        "high-risk-domain",
    }
