from __future__ import annotations

from collections.abc import Callable
from secrets import token_bytes
from uuid import uuid4

from harnesslab.agentic.budget import RateLimitExceeded, SlidingWindowRateLimiter
from harnesslab.agentic.contracts import BudgetLimits, EvalCheck, EvalReport, StepStatus
from harnesslab.agentic.memory import ContextCompiler, HybridMemoryStore
from harnesslab.agentic.model_selection import (
    ModelSelector,
    ModelWorkload,
    reference_profiles,
)
from harnesslab.agentic.scenarios import build_scenario
from harnesslab.agentic.scheduler import AgenticControlPlane
from harnesslab.agentic.security import CapabilityIssuer
from harnesslab.models import Message


async def run_reference_evals() -> EvalReport:
    checks: list[EvalCheck] = []
    checks.append(await _run_recovery_check())
    checks.append(await _run_replan_check())
    checks.append(await _run_rollback_check())
    checks.append(await _run_injection_check())
    checks.append(await _run_budget_check())
    checks.append(await _run_parallelism_check())
    checks.append(_run_context_memory_check())
    checks.append(_run_model_selection_check())
    checks.append(_run_traffic_guardrail_check())
    return EvalReport(
        passed=sum(check.passed for check in checks),
        total=len(checks),
        checks=checks,
    )


async def execute_scenario(
    name: str,
    *,
    limits: BudgetLimits | None = None,
    clock: Callable[[], float] | None = None,
):
    scenario = build_scenario(name)
    control_plane = AgenticControlPlane(scenario.tools, clock=clock)
    run_id = f"agent_{uuid4().hex[:12]}"
    issuer = CapabilityIssuer(token_bytes(32))
    token = issuer.issue(
        subject="eval-runner",
        run_id=run_id,
        capabilities=scenario.capabilities,
    )
    claims = issuer.verify(token, run_id=run_id)
    report = await control_plane.run(
        scenario.task,
        scenario.plan,
        claims=claims,
        scenario=name,
        limits=limits,
        run_id=run_id,
    )
    return report, scenario


async def _run_recovery_check() -> EvalCheck:
    report, scenario = await execute_scenario("enterprise-recovery")
    retries = [event for event in report.events if event.type == "recovery.retry.scheduled"]
    passed = report.status == "completed" and len(retries) == 1 and scenario.state.deployed
    return EvalCheck(
        id="transient-retry",
        title="Idempotent transient retry",
        passed=passed,
        evidence=(
            f"status={report.status}; retries={len(retries)}; deployed={scenario.state.deployed}"
        ),
        run_id=report.id,
    )


async def _run_replan_check() -> EvalCheck:
    report, _ = await execute_scenario("replan-fallback")
    revised = [event for event in report.events if event.type == "recovery.plan.revised"]
    search = next(step for step in report.steps if step.step_id == "search")
    passed = (
        report.status == "completed"
        and len(revised) == 1
        and search.tool == "repository.cache_search"
    )
    return EvalCheck(
        id="fallback-replan",
        title="Fallback plan revision",
        passed=passed,
        evidence=f"status={report.status}; revised={len(revised)}; final_tool={search.tool}",
        run_id=report.id,
    )


async def _run_rollback_check() -> EvalCheck:
    report, scenario = await execute_scenario("rollback-on-deploy-failure")
    patch = next(step for step in report.steps if step.step_id == "patch")
    passed = (
        report.status == "failed"
        and patch.status == StepStatus.COMPENSATED
        and not scenario.state.patch_applied
    )
    return EvalCheck(
        id="saga-compensation",
        title="Reverse-order compensation",
        passed=passed,
        evidence=(
            f"status={report.status}; patch={patch.status}; "
            f"compensations={scenario.state.compensations}"
        ),
        run_id=report.id,
    )


async def _run_injection_check() -> EvalCheck:
    report, scenario = await execute_scenario("prompt-injection")
    detected = [
        event for event in report.events if event.type == "security.prompt_injection.detected"
    ]
    deploy = next(step for step in report.steps if step.step_id == "deploy")
    passed = (
        report.status == "failed"
        and len(detected) == 1
        and deploy.failure_kind == "policy"
        and not scenario.state.deployed
    )
    return EvalCheck(
        id="injection-containment",
        title="Prompt injection containment",
        passed=passed,
        evidence=(
            f"detected={len(detected)}; deploy_failure={deploy.failure_kind}; "
            f"deployed={scenario.state.deployed}"
        ),
        run_id=report.id,
    )


async def _run_budget_check() -> EvalCheck:
    report, _ = await execute_scenario(
        "enterprise-recovery",
        limits=BudgetLimits(max_tool_calls=1, max_cost_usd=1.0),
    )
    exceeded = [event for event in report.events if event.type == "budget.exceeded"]
    step_budget_failure = any(step.failure_kind == "budget" for step in report.steps)
    passed = report.status == "failed" and (bool(exceeded) or step_budget_failure)
    return EvalCheck(
        id="budget-enforcement",
        title="Hard tool-call budget",
        passed=passed,
        evidence=(
            f"status={report.status}; tool_calls={report.budget_usage.tool_calls}; "
            f"budget_failure={step_budget_failure}"
        ),
        run_id=report.id,
    )


async def _run_parallelism_check() -> EvalCheck:
    report, _ = await execute_scenario("enterprise-recovery")
    batches = [event for event in report.events if event.type == "agent.scheduler.batch"]
    first = batches[0].payload.get("steps", []) if batches else []
    passed = set(first) == {"search", "metrics"} and all(
        event.payload.get("parallelism", 0) <= report.plan.max_parallelism for event in batches
    )
    return EvalCheck(
        id="bounded-dag-parallelism",
        title="Dependency-aware bounded parallelism",
        passed=passed,
        evidence=f"first_batch={first}; max_parallelism={report.plan.max_parallelism}",
        run_id=report.id,
    )


def _run_context_memory_check() -> EvalCheck:
    messages = [
        Message(role="system", content="Use evidence."),
        Message(role="user", content="old context " * 100),
        Message(role="assistant", content="old response " * 100),
        Message(role="user", content="current checkout incident"),
    ]
    context = ContextCompiler().compile(messages, max_tokens=64, reserve_tokens=16)
    store = HybridMemoryStore(max_records_per_namespace=3)
    first = store.remember("eval", "checkout authorization must happen before mutation")
    duplicate = store.remember("eval", "checkout authorization must happen before mutation")
    hits = store.search("eval", "checkout authorization")
    passed = (
        context.token_count <= 48
        and context.messages[-1].content == "current checkout incident"
        and first.id == duplicate.id
        and hits[0].record.id == first.id
    )
    return EvalCheck(
        id="context-memory-bounds",
        title="Bounded context and deduplicated memory",
        passed=passed,
        evidence=(
            f"tokens={context.token_count}/48; dropped={context.dropped_messages}; "
            f"records={store.count('eval')}"
        ),
    )


def _run_model_selection_check() -> EvalCheck:
    decision = ModelSelector().select(
        reference_profiles(),
        ModelWorkload(
            input_tokens=8_000,
            expected_output_tokens=1_000,
            requires_tools=True,
            domain="agentic-tools",
            min_tool_success=0.9,
        ),
    )
    rejected_sft = next(
        candidate for candidate in decision.candidates if candidate.model_id == "domain-sft"
    )
    passed = decision.selected_model == "general-base" and not rejected_sft.eligible
    return EvalCheck(
        id="constraint-model-selection",
        title="Evidence-based base versus fine-tuned selection",
        passed=passed,
        evidence=(
            f"selected={decision.selected_model}; domain-sft={', '.join(rejected_sft.reasons)}"
        ),
    )


def _run_traffic_guardrail_check() -> EvalCheck:
    limiter = SlidingWindowRateLimiter(requests=2, window_seconds=60)
    limiter.acquire("user-1", now=0)
    limiter.acquire("user-1", now=1)
    blocked = False
    try:
        limiter.acquire("user-1", now=2)
    except RateLimitExceeded:
        blocked = True
    recovered = limiter.acquire("user-1", now=61) == 1
    return EvalCheck(
        id="traffic-abuse-guardrail",
        title="Per-user sliding-window abuse control",
        passed=blocked and recovered,
        evidence=f"third request blocked={blocked}; capacity recovered={recovered}",
    )
