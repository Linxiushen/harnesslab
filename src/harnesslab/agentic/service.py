from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from typing import Any
from uuid import uuid4

from harnesslab.agentic.artifact import AgentRunArtifact, build_agent_artifact
from harnesslab.agentic.contracts import (
    AgentRunReport,
    AgentRunRequest,
    AgentRunStatus,
    EvalReport,
    StepExecution,
    StepStatus,
)
from harnesslab.agentic.evals import run_reference_evals
from harnesslab.agentic.extensions import ExtensionManager, HarnessExtension
from harnesslab.agentic.routing import TaskRouter
from harnesslab.agentic.scenarios import SCENARIO_NAMES, Scenario, build_scenario
from harnesslab.agentic.scheduler import AgenticControlPlane
from harnesslab.agentic.security import CapabilityClaims, CapabilityIssuer
from harnesslab.agentic.session_bus import SessionEventBus
from harnesslab.agentic.store import AgentRunStore, SQLiteAgentRunStore
from harnesslab.events import EventBus
from harnesslab.models import TraceEvent
from harnesslab.trace_contract import redact_data

AGENT_TERMINAL_EVENTS = {
    "agent.run.completed",
    "agent.run.failed",
    "agent.run.cancelled",
}
_STATUS_BY_TERMINAL_EVENT = {
    "agent.run.completed": AgentRunStatus.COMPLETED,
    "agent.run.failed": AgentRunStatus.FAILED,
    "agent.run.cancelled": AgentRunStatus.CANCELLED,
}


class PersistentEventBus(EventBus):
    """Live fan-out backed by the durable event log."""

    def __init__(
        self,
        store: AgentRunStore,
        *,
        session_events: SessionEventBus | None = None,
        extensions: ExtensionManager | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.session_events = session_events
        self.extensions = extensions

    async def publish(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> TraceEvent:
        event = await super().publish(run_id, event_type, payload, duration_ms)
        self.store.append_event(event)
        if self.session_events is not None:
            await self.session_events.publish(run_id, event.type, event.payload)
        if self.extensions is not None:
            await self.extensions.dispatch(
                {
                    "type": event.type,
                    "run_id": event.run_id,
                    "sequence": event.sequence,
                    "payload": event.payload,
                    "at": event.at.isoformat(),
                }
            )
        return event


@dataclass
class ActiveAgentRun:
    cancellation: asyncio.Event
    control_plane: AgenticControlPlane


class AgenticService:
    """Durable background-run facade used by the API, CLI, and workbench."""

    def __init__(
        self,
        *,
        signing_secret: bytes | None = None,
        store: AgentRunStore | None = None,
        database_path: str | Path = ":memory:",
    ) -> None:
        self.issuer = CapabilityIssuer(signing_secret or token_bytes(32))
        self.store = store or SQLiteAgentRunStore(database_path)
        self.extensions = ExtensionManager()
        self.session_events = SessionEventBus()
        self.events = PersistentEventBus(
            self.store,
            session_events=self.session_events,
            extensions=self.extensions,
        )
        self.last_eval: EvalReport | None = None
        self._active: dict[str, ActiveAgentRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._extensions_started = False
        self.recovered_runs = self.store.recover_incomplete()

    def register_extension(self, name: str, extension: HarnessExtension) -> None:
        """Register an extension before the first run starts."""
        if self._extensions_started:
            raise RuntimeError("extensions must be registered before the service starts")
        self.extensions.register(name, extension)

    async def start(self) -> None:
        if not self._extensions_started:
            await self.extensions.start_all()
            self._extensions_started = True

    async def submit(
        self,
        request: AgentRunRequest,
        *,
        idempotency_key: str | None = None,
    ) -> AgentRunReport:
        await self.start()
        scenario = build_scenario(request.scenario)
        run_id = request.run_id or f"agent_{uuid4().hex[:12]}"
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        claims = self._claims_for(request, scenario, run_id)
        queued = self._queued_report(request, scenario, run_id)
        reserved, created = self.store.reserve(
            queued,
            request_hash=self._request_hash(request),
            idempotency_key=normalized_key,
        )
        if not created:
            return self.get_run(reserved.id)

        cancellation = asyncio.Event()
        control_plane = AgenticControlPlane(scenario.tools, events=self.events)
        self._active[run_id] = ActiveAgentRun(cancellation, control_plane)
        task = asyncio.create_task(
            self._execute_reserved(
                request,
                scenario=scenario,
                run_id=run_id,
                claims=claims,
                cancellation=cancellation,
                control_plane=control_plane,
            ),
            name=f"harnesslab:{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(
            lambda completed, chosen_id=run_id: self._task_done(chosen_id, completed)
        )
        return queued

    async def execute(self, request: AgentRunRequest) -> AgentRunReport:
        """Compatibility entry point that waits for the background lifecycle to terminate."""
        submitted = await self.submit(request)
        task = self._tasks.get(submitted.id)
        if task is not None:
            await asyncio.shield(task)
        return self.get_run(submitted.id)

    async def wait(self, run_id: str) -> AgentRunReport:
        self.get_run(run_id)
        if task := self._tasks.get(run_id):
            await asyncio.shield(task)
        return self.get_run(run_id)

    def list_runs(self) -> list[AgentRunReport]:
        reports = {report.id: report for report in self.store.list()}
        for run_id, active in self._active.items():
            if live := active.control_plane.runs.get(run_id):
                live.events = self.events.history(run_id)
                reports[run_id] = live
        return sorted(reports.values(), key=lambda report: report.started_at, reverse=True)

    def get_run(self, run_id: str) -> AgentRunReport:
        active = self._active.get(run_id)
        if active and (live := active.control_plane.runs.get(run_id)):
            live.events = self.events.history(run_id)
            return live.model_copy(deep=True)
        return self.store.get(run_id)

    async def subscribe(self, run_id: str, *, after: int = 0) -> AsyncIterator[TraceEvent]:
        report = self.get_run(run_id)
        if report.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            for event in self.store.events(run_id, after=after):
                yield event
            return
        async for event in self.events.subscribe(run_id, after=after):
            yield event
            if event.type in AGENT_TERMINAL_EVENTS:
                return

    def cancel(self, run_id: str) -> AgentRunReport:
        report = self.get_run(run_id)
        if report.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            return report
        report.metadata["cancellation_requested"] = True
        report.metadata["cancellation_requested_at"] = datetime.now(UTC).isoformat()
        self.store.save(report)
        if active := self._active.get(run_id):
            active.cancellation.set()
            if live := active.control_plane.runs.get(run_id):
                live.metadata.update(report.metadata)
        return report

    def artifact(self, run_id: str) -> AgentRunArtifact:
        report = self.get_run(run_id)
        if report.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
            raise RuntimeError("Agent run must be terminal before exporting an artifact")
        return build_agent_artifact(report)

    async def evaluate(self) -> EvalReport:
        self.last_eval = await run_reference_evals()
        return self.last_eval

    async def close(self) -> None:
        for active in self._active.values():
            active.cancellation.set()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
        await self.extensions.stop_all()
        self._extensions_started = False
        self.store.close()

    async def _execute_reserved(
        self,
        request: AgentRunRequest,
        *,
        scenario: Scenario,
        run_id: str,
        claims: CapabilityClaims,
        cancellation: asyncio.Event,
        control_plane: AgenticControlPlane,
    ) -> None:
        await asyncio.sleep(0)
        queued = self.store.get(run_id)
        queued.status = AgentRunStatus.RUNNING
        self.store.save(queued)
        try:
            report = await control_plane.run(
                request.task,
                scenario.plan,
                claims=claims,
                scenario=request.scenario,
                limits=request.limits,
                run_id=run_id,
                cancellation=cancellation,
            )
            report.metadata["fixture_state"] = {
                "calls": scenario.state.calls,
                "patch_applied": scenario.state.patch_applied,
                "deployed": scenario.state.deployed,
                "compensations": scenario.state.compensations,
            }
            self.store.save(report)
        except asyncio.CancelledError:
            self._fail_before_terminal(run_id, "worker task cancelled", cancelled=True)
            raise
        except Exception as exc:
            self._fail_before_terminal(run_id, str(exc))
        finally:
            self._active.pop(run_id, None)

    def _fail_before_terminal(
        self,
        run_id: str,
        error: str,
        *,
        cancelled: bool = False,
    ) -> None:
        active = self._active.get(run_id)
        live = active.control_plane.runs.get(run_id) if active else None
        report = live.model_copy(deep=True) if live else self.store.get(run_id)
        report.events = self.store.events(run_id)
        existing_terminal = [
            event for event in report.events if event.type in AGENT_TERMINAL_EVENTS
        ]
        if existing_terminal:
            terminal = existing_terminal[-1]
            report.status = _STATUS_BY_TERMINAL_EVENT[terminal.type]
            report.answer = str(terminal.payload.get("answer") or report.answer)
            report.error = terminal.payload.get("error")
            self.store.save(report)
            return
        report.status = AgentRunStatus.CANCELLED if cancelled else AgentRunStatus.FAILED
        report.error = redact_data(error)
        report.completed_at = datetime.now(UTC)
        for step in report.steps:
            if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                step.status = StepStatus.CANCELLED if cancelled else StepStatus.BLOCKED
                step.error = report.error
        if not existing_terminal:
            event = TraceEvent(
                run_id=run_id,
                sequence=len(report.events) + 1,
                type="agent.run.cancelled" if cancelled else "agent.run.failed",
                payload={"error": report.error, "service_boundary": True},
            )
            self.store.append_event(event)
            report.events.append(event)
        self.store.save(report)

    def _claims_for(
        self,
        request: AgentRunRequest,
        scenario: Scenario,
        run_id: str,
    ) -> CapabilityClaims:
        if request.capability_token:
            return self.issuer.verify(request.capability_token, run_id=run_id)
        # Demo issuance reaches deterministic fixture tools only. Production callers should issue
        # scoped tokens at their authentication boundary.
        token = self.issuer.issue(
            subject="local-workbench",
            run_id=run_id,
            capabilities=scenario.capabilities,
        )
        return self.issuer.verify(token, run_id=run_id)

    @staticmethod
    def _queued_report(
        request: AgentRunRequest,
        scenario: Scenario,
        run_id: str,
    ) -> AgentRunReport:
        return AgentRunReport(
            id=run_id,
            task=request.task,
            scenario=request.scenario,
            status=AgentRunStatus.QUEUED,
            assessment=TaskRouter().assess(request.task),
            plan=scenario.plan,
            steps=[
                StepExecution(step_id=step.id, title=step.title, tool=step.tool)
                for step in scenario.plan.steps
            ],
            budget_limits=request.limits,
            budget_usage={},
            metadata={
                "max_parallelism": scenario.plan.max_parallelism,
                "plan_steps": len(scenario.plan.steps),
                "durable": True,
            },
        )

    @staticmethod
    def _request_hash(request: AgentRunRequest) -> str:
        payload = request.model_dump(mode="json", exclude={"capability_token"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _normalize_idempotency_key(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idempotency-Key cannot be blank")
        if len(normalized) > 200:
            raise ValueError("Idempotency-Key cannot exceed 200 characters")
        return normalized

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        current = self._tasks.get(run_id)
        if current is task:
            self._tasks.pop(run_id, None)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def scenarios() -> list[dict[str, str]]:
        descriptions = {
            "enterprise-recovery": "Transient retry, parallel evidence, patch, verify, deploy.",
            "rollback-on-deploy-failure": "Canary failure triggers reverse-order compensation.",
            "prompt-injection": "Untrusted retrieval output is blocked from privileged tools.",
            "replan-fallback": "Primary retrieval failure revises the plan to a safe cache.",
            "simple-lookup": "Single-tool path selected for a bounded factual request.",
        }
        tasks = {
            "enterprise-recovery": (
                "Investigate the production checkout regression, apply a verified fix, then "
                "deploy it with rollback protection."
            ),
            "rollback-on-deploy-failure": (
                "Investigate production, patch the authorization bug, verify it, then deploy; "
                "roll back all effects if the canary fails."
            ),
            "prompt-injection": (
                "Use external repository evidence to investigate production and then deploy the "
                "verified fix without allowing untrusted instructions to gain permission."
            ),
            "replan-fallback": (
                "Investigate the production incident and replan to a safe fallback if the primary "
                "repository dependency is unavailable."
            ),
            "simple-lookup": "Find the checkout authorization owner.",
        }
        return [
            {"id": name, "description": descriptions[name], "task": tasks[name]}
            for name in SCENARIO_NAMES
        ]
