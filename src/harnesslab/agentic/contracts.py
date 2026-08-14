from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from harnesslab.models import TraceEvent


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskClass(StrEnum):
    SIMPLE = "simple"
    COMPLEX = "complex"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY = "policy"
    BUDGET = "budget"
    INVALID_OUTPUT = "invalid_output"
    CANCELLED = "cancelled"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    REPLAN = "replan"
    ROLLBACK = "rollback"
    ABORT = "abort"
    CONTINUE = "continue"


class AgentToolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    read_only: bool = True
    idempotent: bool = True
    compensatable: bool = False
    required_capabilities: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    max_attempts: int = Field(default=1, ge=1, le=5)
    cost_units: int = Field(default=1, ge=0, le=1000)
    trust_boundary: Literal["local", "internal", "external"] = "local"
    allowed_hosts: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    on_failure: RecoveryAction = RecoveryAction.ABORT
    fallback_tool: str | None = None
    expected_output_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_recovery(self) -> PlanStep:
        if self.on_failure == RecoveryAction.REPLAN and not self.fallback_tool:
            raise ValueError("on_failure=replan requires fallback_tool")
        if self.id in self.depends_on:
            raise ValueError(f"step {self.id!r} cannot depend on itself")
        return self


class ExecutionPlan(BaseModel):
    id: str
    objective: str
    steps: list[PlanStep] = Field(min_length=1, max_length=100)
    max_parallelism: int = Field(default=4, ge=1, le=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dag(self) -> ExecutionPlan:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step IDs must be unique")
        known = set(step_ids)
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"step {step.id!r} has unknown dependencies: {sorted(missing)}")

        graph = {step.id: step.depends_on for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError(f"plan contains a dependency cycle at {step_id!r}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in graph[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class BudgetLimits(BaseModel):
    max_input_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int = Field(default=8_000, ge=1)
    max_tool_calls: int = Field(default=30, ge=1, le=10_000)
    max_cost_usd: float = Field(default=1.0, gt=0)
    max_wall_time_seconds: float = Field(default=120.0, gt=0, le=3600)


class BudgetUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_cost_units: int = 0
    estimated_cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


class StepAttempt(BaseModel):
    attempt: int
    tool: str
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: float | None = None
    status: Literal["succeeded", "failed"]
    failure_kind: FailureKind | None = None
    error: str | None = None


class StepExecution(BaseModel):
    step_id: str
    title: str
    tool: str
    status: StepStatus = StepStatus.PENDING
    attempts: list[StepAttempt] = Field(default_factory=list)
    output: Any = None
    error: str | None = None
    failure_kind: FailureKind | None = None
    recovery: list[RecoveryAction] = Field(default_factory=list)
    policy_reason: str | None = None
    tainted_output: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskSignal(BaseModel):
    name: str
    weight: int
    evidence: str


class TaskAssessment(BaseModel):
    task_class: TaskClass
    score: int
    signals: list[TaskSignal]
    recommended_parallelism: int
    requires_planning: bool


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    required: list[str] = Field(default_factory=list)
    granted: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high", "critical"] = "low"


class AgentRunRequest(BaseModel):
    task: str = Field(min_length=3, max_length=20_000)
    scenario: str = "enterprise-recovery"
    run_id: str | None = Field(default=None, pattern=r"^agent_[a-zA-Z0-9_-]{6,80}$")
    capability_token: str | None = None
    limits: BudgetLimits = Field(default_factory=BudgetLimits)


class AgentRunReport(BaseModel):
    id: str
    task: str
    scenario: str
    status: AgentRunStatus
    assessment: TaskAssessment
    plan: ExecutionPlan
    steps: list[StepExecution]
    events: list[TraceEvent] = Field(default_factory=list)
    budget_limits: BudgetLimits
    budget_usage: BudgetUsage
    answer: str = ""
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalCheck(BaseModel):
    id: str
    title: str
    passed: bool
    evidence: str
    run_id: str | None = None


class EvalReport(BaseModel):
    passed: int
    total: int
    checks: list[EvalCheck]
    generated_at: datetime = Field(default_factory=utc_now)
