from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from harnesslab.agentic.budget import BudgetExceeded, BudgetLedger
from harnesslab.agentic.contracts import (
    AgentRunReport,
    AgentRunStatus,
    AgentToolSpec,
    BudgetLimits,
    ExecutionPlan,
    FailureKind,
    PlanStep,
    RecoveryAction,
    StepAttempt,
    StepExecution,
    StepStatus,
)
from harnesslab.agentic.routing import TaskRouter
from harnesslab.agentic.security import CapabilityClaims, PolicyEngine
from harnesslab.events import EventBus
from harnesslab.trace_contract import redact_data

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]
CompensationHandler = Callable[[dict[str, Any], Any], Awaitable[Any] | Any]


class ToolExecutionError(RuntimeError):
    pass


class TransientToolError(ToolExecutionError):
    pass


class PermanentToolError(ToolExecutionError):
    pass


class InvalidToolArguments(ToolExecutionError):
    pass


class InvalidToolOutput(ToolExecutionError):
    pass


class AgentRunCancelled(ToolExecutionError):
    pass


@dataclass
class RegisteredTool:
    spec: AgentToolSpec
    handler: ToolHandler
    compensator: CompensationHandler | None = None


class AgentToolRegistry:
    """Typed tools plus optional compensators for saga-style rollback."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        spec: AgentToolSpec,
        handler: ToolHandler,
        *,
        compensator: CompensationHandler | None = None,
    ) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        if spec.compensatable and compensator is None:
            raise ValueError(f"compensatable tool requires a compensator: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec, handler, compensator)

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise LookupError(f"unknown agent tool: {name}") from exc

    def specs(self) -> list[AgentToolSpec]:
        return [registered.spec for registered in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        registered = self.get(name)
        self._validate_arguments(registered.spec, arguments)
        result = registered.handler(arguments)
        return await result if inspect.isawaitable(result) else result

    async def compensate(self, name: str, arguments: dict[str, Any], output: Any) -> Any:
        registered = self.get(name)
        if registered.compensator is None:
            raise PermanentToolError(f"tool has no compensator: {name}")
        result = registered.compensator(arguments, output)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _validate_arguments(spec: AgentToolSpec, arguments: dict[str, Any]) -> None:
        schema = spec.input_schema
        if schema.get("type") not in (None, "object"):
            raise InvalidToolArguments(f"{spec.name}: input schema must describe an object")
        required = set(schema.get("required", []))
        missing = required - set(arguments)
        if missing:
            raise InvalidToolArguments(f"{spec.name}: missing required fields {sorted(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(arguments) - set(properties)
            if unknown:
                raise InvalidToolArguments(f"{spec.name}: unknown fields {sorted(unknown)}")
        expected_types: dict[str, type | tuple[type, ...]] = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for name, value in arguments.items():
            expected = expected_types.get(properties.get(name, {}).get("type"))
            if expected and not isinstance(value, expected):
                raise InvalidToolArguments(
                    f"{spec.name}: field {name!r} has invalid type {type(value).__name__}"
                )


class AgenticControlPlane:
    """Bounded DAG executor with policy, budget, recovery, and replayable events."""

    def __init__(
        self,
        tools: AgentToolRegistry,
        *,
        policy: PolicyEngine | None = None,
        events: EventBus | None = None,
        router: TaskRouter | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.tools = tools
        self.policy = policy or PolicyEngine()
        self.events = events or EventBus()
        self.router = router or TaskRouter()
        self.clock = clock
        self.runs: dict[str, AgentRunReport] = {}

    async def run(
        self,
        task: str,
        plan: ExecutionPlan,
        *,
        claims: CapabilityClaims,
        scenario: str,
        limits: BudgetLimits | None = None,
        run_id: str | None = None,
        cancellation: asyncio.Event | None = None,
    ) -> AgentRunReport:
        chosen_id = run_id or f"agent_{uuid4().hex[:12]}"
        if claims.run_id != chosen_id:
            raise PermissionError("capability claims are scoped to another run")
        assessment = self.router.assess(task)
        ledger = BudgetLedger(limits or BudgetLimits(), clock=self.clock)
        executions = {
            step.id: StepExecution(step_id=step.id, title=step.title, tool=step.tool)
            for step in plan.steps
        }
        report = AgentRunReport(
            id=chosen_id,
            task=task,
            scenario=scenario,
            status=AgentRunStatus.RUNNING,
            assessment=assessment,
            plan=plan,
            steps=list(executions.values()),
            budget_limits=ledger.limits,
            budget_usage=ledger.usage,
            metadata={
                "max_parallelism": plan.max_parallelism,
                "capability_subject": claims.subject,
                "plan_steps": len(plan.steps),
            },
        )
        self.runs[chosen_id] = report
        await self._emit(
            report,
            "agent.run.started",
            {"task": task, "scenario": scenario, "plan_id": plan.id},
        )
        await self._emit(
            report,
            "agent.task.routed",
            assessment.model_dump(mode="json"),
        )
        await self._emit(
            report,
            "agent.plan.compiled",
            {
                "plan_id": plan.id,
                "steps": [step.model_dump(mode="json") for step in plan.steps],
                "max_parallelism": plan.max_parallelism,
            },
        )

        try:
            ledger.charge_prompt(task)
            await self._execute_plan(
                report,
                executions,
                claims,
                ledger,
                cancellation or asyncio.Event(),
            )
            failed = [
                execution
                for execution in executions.values()
                if execution.status in {StepStatus.FAILED, StepStatus.BLOCKED}
            ]
            cancelled = any(
                execution.status == StepStatus.CANCELLED for execution in executions.values()
            )
            if cancelled:
                report.status = AgentRunStatus.CANCELLED
                report.error = "run cancelled"
                terminal_event = "agent.run.cancelled"
            elif failed:
                report.status = AgentRunStatus.FAILED
                report.error = failed[0].error or f"step failed: {failed[0].step_id}"
                terminal_event = "agent.run.failed"
            else:
                report.status = AgentRunStatus.COMPLETED
                report.answer = self._build_answer(report)
                terminal_event = "agent.run.completed"
        except BudgetExceeded as exc:
            report.status = AgentRunStatus.FAILED
            report.error = str(exc)
            terminal_event = "agent.run.failed"
            await self._emit(report, "budget.exceeded", {"error": str(exc)})
        except Exception as exc:
            report.status = AgentRunStatus.FAILED
            report.error = redact_data(str(exc))
            terminal_event = "agent.run.failed"
            await self._emit(
                report,
                "agent.runtime.error",
                {"kind": type(exc).__name__, "error": str(exc)},
            )
        finally:
            try:
                ledger.check_wall_time()
            except BudgetExceeded as exc:
                report.status = AgentRunStatus.FAILED
                report.error = str(exc)
                terminal_event = "agent.run.failed"
                await self._emit(report, "budget.exceeded", {"error": str(exc)})
            report.budget_usage = ledger.usage.model_copy(deep=True)
            report.steps = list(executions.values())
            report.completed_at = datetime.now(UTC)
        await self._emit(
            report,
            terminal_event,
            {
                "status": report.status,
                "answer": report.answer,
                "error": report.error,
                "budget": report.budget_usage.model_dump(mode="json"),
            },
        )
        report.events = self.events.history(report.id)
        return report

    async def _execute_plan(
        self,
        report: AgentRunReport,
        executions: dict[str, StepExecution],
        claims: CapabilityClaims,
        ledger: BudgetLedger,
        cancellation: asyncio.Event,
    ) -> None:
        abort = False
        while True:
            pending = [
                step
                for step in report.plan.steps
                if executions[step.id].status == StepStatus.PENDING
            ]
            if not pending or abort:
                break
            if cancellation.is_set():
                for step in pending:
                    executions[step.id].status = StepStatus.CANCELLED
                    executions[step.id].error = "cancelled before execution"
                break

            ready = [
                step
                for step in pending
                if all(
                    executions[dependency].status == StepStatus.SUCCEEDED
                    for dependency in step.depends_on
                )
            ]
            if not ready:
                for step in pending:
                    failed_dependencies = [
                        dependency
                        for dependency in step.depends_on
                        if executions[dependency].status
                        in {StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.CANCELLED}
                    ]
                    if failed_dependencies:
                        execution = executions[step.id]
                        execution.status = StepStatus.BLOCKED
                        execution.error = f"blocked by dependencies: {failed_dependencies}"
                        await self._emit(
                            report,
                            "agent.step.blocked",
                            {"step_id": step.id, "dependencies": failed_dependencies},
                        )
                if all(executions[step.id].status != StepStatus.PENDING for step in pending):
                    break
                raise RuntimeError("scheduler deadlock: no runnable steps")

            batch = ready[: report.plan.max_parallelism]
            await self._emit(
                report,
                "agent.scheduler.batch",
                {"steps": [step.id for step in batch], "parallelism": len(batch)},
            )
            await asyncio.gather(
                *(
                    self._execute_step(
                        report,
                        step,
                        executions[step.id],
                        executions,
                        claims,
                        ledger,
                        cancellation,
                    )
                    for step in batch
                )
            )
            for step in batch:
                execution = executions[step.id]
                if execution.status != StepStatus.FAILED:
                    continue
                if step.on_failure == RecoveryAction.CONTINUE:
                    execution.recovery.append(RecoveryAction.CONTINUE)
                    await self._emit(
                        report,
                        "recovery.continued",
                        {"step_id": step.id, "reason": execution.error},
                    )
                    continue
                if step.on_failure == RecoveryAction.ROLLBACK:
                    execution.recovery.append(RecoveryAction.ROLLBACK)
                    await self._rollback(report, executions)
                abort = True
                break

        if abort:
            for execution in executions.values():
                if execution.status == StepStatus.PENDING:
                    execution.status = StepStatus.BLOCKED
                    execution.error = "plan aborted after an unrecovered failure"

    async def _execute_step(
        self,
        report: AgentRunReport,
        step: PlanStep,
        execution: StepExecution,
        executions: dict[str, StepExecution],
        claims: CapabilityClaims,
        ledger: BudgetLedger,
        cancellation: asyncio.Event,
    ) -> None:
        if cancellation.is_set():
            execution.status = StepStatus.CANCELLED
            execution.error = "cancelled before tool execution"
            execution.completed_at = datetime.now(UTC)
            await self._emit(
                report,
                "agent.step.cancelled",
                {"step_id": step.id, "tool": step.tool},
            )
            return
        registered = self.tools.get(step.tool)
        tainted_dependencies = any(executions[item].tainted_output for item in step.depends_on)
        decision = self.policy.authorize(
            registered.spec,
            claims,
            step.arguments,
            tainted_dependencies=tainted_dependencies,
        )
        execution.started_at = datetime.now(UTC)
        execution.status = StepStatus.RUNNING
        await self._emit(
            report,
            "agent.step.started",
            {"step_id": step.id, "tool": step.tool, "dependencies": step.depends_on},
        )
        await self._emit(
            report,
            "policy.decision",
            {"step_id": step.id, "tool": step.tool, **decision.model_dump(mode="json")},
        )
        if not decision.allowed:
            self._fail_execution(execution, FailureKind.POLICY, decision.reason)
            execution.policy_reason = decision.reason
            await self._emit(
                report,
                "agent.step.failed",
                {"step_id": step.id, "kind": FailureKind.POLICY, "error": decision.reason},
            )
            return

        max_attempts = step.max_attempts or registered.spec.max_attempts
        timeout = step.timeout_seconds or registered.spec.timeout_seconds
        for attempt_number in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                if cancellation.is_set():
                    raise AgentRunCancelled("run cancellation requested")
                ledger.charge_tool(registered.spec.cost_units)
                await self._emit(
                    report,
                    "tool.attempt.started",
                    {
                        "step_id": step.id,
                        "tool": step.tool,
                        "attempt": attempt_number,
                        "arguments": step.arguments,
                    },
                )
                output = await self._call_with_cancellation(
                    step.tool,
                    step.arguments,
                    timeout=timeout,
                    cancellation=cancellation,
                )
                self._validate_output(step, output)
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                execution.attempts.append(
                    StepAttempt(
                        attempt=attempt_number,
                        tool=step.tool,
                        completed_at=datetime.now(UTC),
                        duration_ms=duration_ms,
                        status="succeeded",
                    )
                )
                execution.status = StepStatus.SUCCEEDED
                execution.output = redact_data(output)
                execution.completed_at = datetime.now(UTC)
                injection = self.policy.detector.inspect(output)
                execution.tainted_output = injection.detected
                await self._emit(
                    report,
                    "tool.attempt.completed",
                    {
                        "step_id": step.id,
                        "tool": step.tool,
                        "attempt": attempt_number,
                        "result": output,
                    },
                    duration_ms,
                )
                if injection.detected:
                    await self._emit(
                        report,
                        "security.prompt_injection.detected",
                        {
                            "step_id": step.id,
                            "tool": step.tool,
                            "score": injection.score,
                            "patterns": injection.patterns,
                        },
                    )
                await self._emit(
                    report,
                    "agent.step.completed",
                    {"step_id": step.id, "tool": step.tool, "tainted": injection.detected},
                )
                return
            except AgentRunCancelled as exc:
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                execution.attempts.append(
                    StepAttempt(
                        attempt=attempt_number,
                        tool=step.tool,
                        completed_at=datetime.now(UTC),
                        duration_ms=duration_ms,
                        status="failed",
                        failure_kind=FailureKind.CANCELLED,
                        error=str(exc),
                    )
                )
                execution.status = StepStatus.CANCELLED
                execution.failure_kind = FailureKind.CANCELLED
                execution.error = str(exc)
                execution.completed_at = datetime.now(UTC)
                await self._emit(
                    report,
                    "tool.attempt.failed",
                    {
                        "step_id": step.id,
                        "tool": step.tool,
                        "attempt": attempt_number,
                        "kind": FailureKind.CANCELLED,
                        "error": str(exc),
                    },
                    duration_ms,
                )
                await self._emit(
                    report,
                    "agent.step.cancelled",
                    {"step_id": step.id, "tool": step.tool},
                )
                return
            except Exception as exc:  # classification is centralized below
                kind = self._classify_failure(exc)
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                execution.attempts.append(
                    StepAttempt(
                        attempt=attempt_number,
                        tool=step.tool,
                        completed_at=datetime.now(UTC),
                        duration_ms=duration_ms,
                        status="failed",
                        failure_kind=kind,
                        error=str(exc),
                    )
                )
                await self._emit(
                    report,
                    "tool.attempt.failed",
                    {
                        "step_id": step.id,
                        "tool": step.tool,
                        "attempt": attempt_number,
                        "kind": kind,
                        "error": str(exc),
                    },
                    duration_ms,
                )
                can_retry = (
                    kind == FailureKind.TRANSIENT
                    and registered.spec.idempotent
                    and attempt_number < max_attempts
                )
                if can_retry:
                    execution.recovery.append(RecoveryAction.RETRY)
                    await self._emit(
                        report,
                        "recovery.retry.scheduled",
                        {"step_id": step.id, "next_attempt": attempt_number + 1},
                    )
                    continue
                self._fail_execution(execution, kind, str(exc))
                break

        if execution.status == StepStatus.FAILED and step.on_failure == RecoveryAction.REPLAN:
            execution.recovery.append(RecoveryAction.REPLAN)
            await self._emit(
                report,
                "recovery.plan.revised",
                {
                    "step_id": step.id,
                    "failed_tool": step.tool,
                    "fallback_tool": step.fallback_tool,
                },
            )
            fallback = step.model_copy(
                update={
                    "tool": step.fallback_tool,
                    "on_failure": RecoveryAction.ABORT,
                    "fallback_tool": None,
                    "max_attempts": 1,
                }
            )
            execution.status = StepStatus.PENDING
            execution.tool = fallback.tool
            execution.error = None
            execution.failure_kind = None
            await self._execute_step(
                report,
                fallback,
                execution,
                executions,
                claims,
                ledger,
                cancellation,
            )
            return

        if execution.status == StepStatus.FAILED:
            await self._emit(
                report,
                "agent.step.failed",
                {
                    "step_id": step.id,
                    "kind": execution.failure_kind,
                    "error": execution.error,
                },
            )

    async def _call_with_cancellation(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        cancellation: asyncio.Event,
    ) -> Any:
        tool_task = asyncio.create_task(self.tools.call(tool, arguments))
        cancellation_task = asyncio.create_task(cancellation.wait())
        done, pending = await asyncio.wait(
            {tool_task, cancellation_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if tool_task in done:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
            return await tool_task
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancellation_task in done and cancellation.is_set():
            raise AgentRunCancelled("run cancellation requested")
        raise TimeoutError(f"tool timed out after {timeout:g}s")

    async def _rollback(
        self,
        report: AgentRunReport,
        executions: dict[str, StepExecution],
    ) -> None:
        completed = [
            (step, executions[step.id])
            for step in report.plan.steps
            if executions[step.id].status == StepStatus.SUCCEEDED
            and self.tools.get(step.tool).spec.compensatable
        ]
        for step, execution in reversed(completed):
            await self._emit(
                report,
                "recovery.compensation.started",
                {"step_id": step.id, "tool": step.tool},
            )
            try:
                result = await self.tools.compensate(step.tool, step.arguments, execution.output)
                execution.status = StepStatus.COMPENSATED
                execution.recovery.append(RecoveryAction.ROLLBACK)
                await self._emit(
                    report,
                    "recovery.compensation.completed",
                    {"step_id": step.id, "tool": step.tool, "result": result},
                )
            except Exception as exc:
                await self._emit(
                    report,
                    "recovery.compensation.failed",
                    {"step_id": step.id, "tool": step.tool, "error": str(exc)},
                )

    async def _emit(
        self,
        report: AgentRunReport,
        event_type: str,
        payload: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        await self.events.publish(report.id, event_type, redact_data(payload or {}), duration_ms)

    @staticmethod
    def _validate_output(step: PlanStep, output: Any) -> None:
        if not step.expected_output_keys:
            return
        if not isinstance(output, dict):
            raise InvalidToolOutput(f"{step.tool}: expected an object output")
        missing = set(step.expected_output_keys) - set(output)
        if missing:
            raise InvalidToolOutput(f"{step.tool}: missing output fields {sorted(missing)}")

    @staticmethod
    def _classify_failure(exc: Exception) -> FailureKind:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, TransientToolError)):
            return FailureKind.TRANSIENT
        if isinstance(exc, BudgetExceeded):
            return FailureKind.BUDGET
        if isinstance(exc, (InvalidToolArguments, InvalidToolOutput)):
            return FailureKind.INVALID_OUTPUT
        if isinstance(exc, asyncio.CancelledError):
            return FailureKind.CANCELLED
        return FailureKind.PERMANENT

    @staticmethod
    def _fail_execution(execution: StepExecution, kind: FailureKind, error: str) -> None:
        execution.status = StepStatus.FAILED
        execution.failure_kind = kind
        execution.error = redact_data(error)
        execution.completed_at = datetime.now(UTC)

    @staticmethod
    def _build_answer(report: AgentRunReport) -> str:
        succeeded = sum(step.status == StepStatus.SUCCEEDED for step in report.steps)
        retried = sum(RecoveryAction.RETRY in step.recovery for step in report.steps)
        replanned = sum(RecoveryAction.REPLAN in step.recovery for step in report.steps)
        return (
            f"Plan {report.plan.id} completed {succeeded}/{len(report.steps)} steps; "
            f"retries={retried}, replans={replanned}, "
            f"tool_calls={report.budget_usage.tool_calls}."
        )
