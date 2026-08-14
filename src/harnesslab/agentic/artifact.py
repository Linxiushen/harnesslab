from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from harnesslab.agentic.contracts import (
    AgentRunReport,
    AgentRunStatus,
    StepAttempt,
    StepStatus,
    utc_now,
)
from harnesslab.trace_contract import redact_data

AGENT_RUN_CONTRACT_VERSION = "harnesslab.agent-run/v1"
_TERMINAL_BY_STATUS = {
    AgentRunStatus.COMPLETED: "agent.run.completed",
    AgentRunStatus.FAILED: "agent.run.failed",
    AgentRunStatus.CANCELLED: "agent.run.cancelled",
}


class AgentInvariant(BaseModel):
    id: str
    passed: bool
    evidence: str


class AgentReplayReport(BaseModel):
    valid: bool
    integrity_match: bool
    execution_fingerprint_match: bool
    checks: list[AgentInvariant] = Field(default_factory=list)


class AgentRunArtifact(BaseModel):
    contract_version: Literal["harnesslab.agent-run/v1"] = AGENT_RUN_CONTRACT_VERSION
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    source_run_id: str
    execution_fingerprint: str
    integrity_fingerprint: str
    invariants: list[AgentInvariant]
    run: AgentRunReport


def build_agent_artifact(run: AgentRunReport) -> AgentRunArtifact:
    clean = AgentRunReport.model_validate(redact_data(run.model_dump(mode="json")))
    checks = check_agent_invariants(clean)
    execution_fingerprint = _fingerprint(_stable_execution(clean))
    created_at = utc_now().isoformat()
    unsigned = {
        "contract_version": AGENT_RUN_CONTRACT_VERSION,
        "created_at": created_at,
        "source_run_id": clean.id,
        "execution_fingerprint": execution_fingerprint,
        "invariants": [check.model_dump(mode="json") for check in checks],
        "run": clean.model_dump(mode="json"),
    }
    return AgentRunArtifact(
        created_at=created_at,
        source_run_id=clean.id,
        execution_fingerprint=execution_fingerprint,
        integrity_fingerprint=_fingerprint(unsigned),
        invariants=checks,
        run=clean,
    )


def replay_agent_artifact(artifact: AgentRunArtifact | Mapping[str, Any]) -> AgentReplayReport:
    parsed = (
        artifact
        if isinstance(artifact, AgentRunArtifact)
        else AgentRunArtifact.model_validate(artifact)
    )
    checks = check_agent_invariants(parsed.run)
    execution_match = parsed.execution_fingerprint == _fingerprint(
        _stable_execution(parsed.run)
    )
    unsigned = {
        "contract_version": parsed.contract_version,
        "created_at": parsed.created_at,
        "source_run_id": parsed.source_run_id,
        "execution_fingerprint": parsed.execution_fingerprint,
        "invariants": [check.model_dump(mode="json") for check in parsed.invariants],
        "run": parsed.run.model_dump(mode="json"),
    }
    integrity_match = parsed.integrity_fingerprint == _fingerprint(unsigned)
    invariants_match = parsed.invariants == checks
    valid = integrity_match and execution_match and invariants_match and all(
        check.passed for check in checks
    )
    return AgentReplayReport(
        valid=valid,
        integrity_match=integrity_match,
        execution_fingerprint_match=execution_match,
        checks=checks,
    )


def check_agent_invariants(run: AgentRunReport) -> list[AgentInvariant]:
    events = run.events
    sequences = [event.sequence for event in events]
    terminal_events = [event.type for event in events if event.type in _TERMINAL_BY_STATUS.values()]
    expected_terminal = _TERMINAL_BY_STATUS.get(run.status)
    step_ids = {step.step_id for step in run.steps}
    attempt_order = all(_attempts_are_ordered(step.attempts) for step in run.steps)
    policy_positions: dict[tuple[str, str], list[int]] = {}
    attempt_positions: dict[tuple[str, str], list[int]] = {}
    for index, event in enumerate(events):
        key = (
            str(event.payload.get("step_id")),
            str(event.payload.get("tool")),
        )
        if event.type == "policy.decision":
            policy_positions.setdefault(key, []).append(index)
        elif event.type == "tool.attempt.started":
            attempt_positions.setdefault(key, []).append(index)
    policy_ordered = all(
        key in policy_positions and min(policy_positions[key]) < min(positions)
        for key, positions in attempt_positions.items()
    )
    usage = run.budget_usage
    limits = run.budget_limits
    within_budget = (
        usage.input_tokens <= limits.max_input_tokens
        and usage.output_tokens <= limits.max_output_tokens
        and usage.tool_calls <= limits.max_tool_calls
        and usage.estimated_cost_usd <= limits.max_cost_usd
        and usage.elapsed_seconds <= limits.max_wall_time_seconds
    )
    budget_enforced = within_budget or (
        run.status == AgentRunStatus.FAILED
        and any(event.type == "budget.exceeded" for event in events)
    )
    return [
        AgentInvariant(
            id="event-sequence",
            passed=sequences == list(range(1, len(events) + 1)),
            evidence=f"sequences={sequences}",
        ),
        AgentInvariant(
            id="single-terminal",
            passed=(
                len(terminal_events) == 1
                and bool(events)
                and events[-1].type == terminal_events[0]
            ),
            evidence=f"terminal_events={terminal_events}",
        ),
        AgentInvariant(
            id="status-terminal-match",
            passed=expected_terminal is not None and terminal_events == [expected_terminal],
            evidence=f"status={run.status}; expected={expected_terminal}",
        ),
        AgentInvariant(
            id="plan-step-identity",
            passed=step_ids == {step.id for step in run.plan.steps},
            evidence=f"report_steps={sorted(step_ids)}",
        ),
        AgentInvariant(
            id="attempt-order",
            passed=attempt_order,
            evidence="attempt numbers are contiguous per step and tool path",
        ),
        AgentInvariant(
            id="policy-before-tool-attempt",
            passed=policy_ordered,
            evidence=(
                "policy_paths="
                f"{sorted(f'{step}:{tool}' for step, tool in policy_positions)}"
            ),
        ),
        AgentInvariant(
            id="budget-envelope",
            passed=budget_enforced,
            evidence=(
                f"tools={usage.tool_calls}/{limits.max_tool_calls}; "
                f"cost=${usage.estimated_cost_usd:.6f}/${limits.max_cost_usd:.6f}"
            ),
        ),
        AgentInvariant(
            id="no-live-steps-after-terminal",
            passed=all(
                step.status not in {StepStatus.PENDING, StepStatus.RUNNING} for step in run.steps
            ),
            evidence="all plan steps reached a terminal state",
        ),
    ]


def _stable_execution(run: AgentRunReport) -> dict[str, Any]:
    payload = run.model_dump(mode="json")
    for key in ("id", "started_at", "completed_at"):
        payload.pop(key, None)
    payload["events"] = [
        {
            "sequence": event.sequence,
            "type": event.type,
            "payload": _without_volatile(event.payload),
        }
        for event in run.events
    ]
    for step in payload["steps"]:
        step.pop("started_at", None)
        step.pop("completed_at", None)
        for attempt in step["attempts"]:
            attempt.pop("started_at", None)
            attempt.pop("completed_at", None)
            attempt.pop("duration_ms", None)
    payload["budget_usage"].pop("elapsed_seconds", None)
    return payload


def _without_volatile(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_volatile(item)
            for key, item in value.items()
            if key not in {"elapsed_seconds", "duration_ms"}
        }
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


def _attempts_are_ordered(attempts: list[StepAttempt]) -> bool:
    by_tool: dict[str, list[int]] = {}
    for attempt in attempts:
        by_tool.setdefault(attempt.tool, []).append(attempt.attempt)
    return all(numbers == list(range(1, len(numbers) + 1)) for numbers in by_tool.values())


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
