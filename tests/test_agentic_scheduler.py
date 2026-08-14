from __future__ import annotations

import pytest

from harnesslab.agentic.contracts import BudgetLimits, StepStatus
from harnesslab.agentic.evals import execute_scenario, run_reference_evals


@pytest.mark.asyncio
async def test_reference_agentic_evals_are_executable() -> None:
    report = await run_reference_evals()

    assert report.passed == report.total == 9
    assert {check.id for check in report.checks} == {
        "transient-retry",
        "fallback-replan",
        "saga-compensation",
        "injection-containment",
        "budget-enforcement",
        "bounded-dag-parallelism",
        "context-memory-bounds",
        "constraint-model-selection",
        "traffic-abuse-guardrail",
    }


@pytest.mark.asyncio
async def test_enterprise_scenario_retries_and_preserves_dependency_order() -> None:
    report, scenario = await execute_scenario("enterprise-recovery")
    event_types = [event.type for event in report.events]
    search = next(step for step in report.steps if step.step_id == "search")

    assert report.status == "completed"
    assert len(search.attempts) == 2
    assert scenario.state.deployed is True
    assert event_types.count("agent.run.completed") == 1
    assert event_types.index("agent.step.completed") < event_types.index("agent.run.completed")


@pytest.mark.asyncio
async def test_failed_deploy_compensates_effectful_steps() -> None:
    report, scenario = await execute_scenario("rollback-on-deploy-failure")
    patch = next(step for step in report.steps if step.step_id == "patch")
    deploy = next(step for step in report.steps if step.step_id == "deploy")

    assert report.status == "failed"
    assert deploy.failure_kind == "permanent"
    assert patch.status == StepStatus.COMPENSATED
    assert scenario.state.patch_applied is False
    assert scenario.state.compensations == ["repository.patch"]


@pytest.mark.asyncio
async def test_prompt_injection_taint_cannot_reach_deploy() -> None:
    report, scenario = await execute_scenario("prompt-injection")
    search = next(step for step in report.steps if step.step_id == "search")
    deploy = next(step for step in report.steps if step.step_id == "deploy")

    assert search.tainted_output is True
    assert deploy.failure_kind == "policy"
    assert scenario.state.deployed is False
    assert any(event.type == "security.prompt_injection.detected" for event in report.events)


@pytest.mark.asyncio
async def test_tool_budget_fails_before_exceeding_the_configured_limit() -> None:
    report, _ = await execute_scenario(
        "enterprise-recovery",
        limits=BudgetLimits(max_tool_calls=1, max_cost_usd=1.0),
    )

    assert report.status == "failed"
    assert report.budget_usage.tool_calls == 1
    assert any(step.failure_kind == "budget" for step in report.steps)


@pytest.mark.asyncio
async def test_wall_time_budget_still_produces_one_failed_terminal_event() -> None:
    ticks = iter([0.0] * 16 + [1.0] * 10)
    report, _ = await execute_scenario(
        "enterprise-recovery",
        limits=BudgetLimits(max_wall_time_seconds=0.5),
        clock=lambda: next(ticks),
    )

    terminal = [event for event in report.events if event.type.startswith("agent.run.")]
    terminal = [event for event in terminal if event.type != "agent.run.started"]
    assert report.status == "failed"
    assert "wall time budget exceeded" in (report.error or "")
    assert [event.type for event in terminal] == ["agent.run.failed"]
