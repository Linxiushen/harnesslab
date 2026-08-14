from __future__ import annotations

import asyncio

import pytest

from harnesslab.agentic.artifact import build_agent_artifact, replay_agent_artifact
from harnesslab.agentic.contracts import (
    AgentRunRequest,
    AgentToolSpec,
    ExecutionPlan,
    PlanStep,
)
from harnesslab.agentic.evals import execute_scenario
from harnesslab.agentic.scenarios import build_scenario
from harnesslab.agentic.scheduler import AgenticControlPlane, AgentToolRegistry
from harnesslab.agentic.security import CapabilityIssuer
from harnesslab.agentic.service import AgenticService
from harnesslab.agentic.store import IdempotencyConflict, SQLiteAgentRunStore


class ServiceExtension:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.events: list[str] = []

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def on_event(self, event: dict[str, object]) -> None:
        self.events.append(str(event["type"]))


@pytest.mark.asyncio
async def test_service_wires_bounded_session_events_and_extension_lifecycle() -> None:
    service = AgenticService(signing_secret=b"s" * 32)
    extension = ServiceExtension()
    service.register_extension("test-observer", extension)

    report = await service.execute(
        AgentRunRequest(
            task="Find the checkout authorization owner.",
            scenario="simple-lookup",
        )
    )
    history = service.session_events.history(report.id)
    subscription = service.session_events.subscribe(
        report.id,
        after_sequence=history[-3].sequence,
    )
    replayed = [await subscription.get() for _ in range(2)]

    assert extension.started == 1
    assert extension.events[0] == "agent.run.started"
    assert extension.events[-1] == "agent.run.completed"
    assert replayed[-1].event_type == "agent.run.completed"
    assert history[-1].event_type == "agent.run.completed"

    subscription.close()
    await service.close()
    assert extension.stopped == 1


@pytest.mark.asyncio
async def test_sqlite_store_survives_service_restart(tmp_path) -> None:
    database = tmp_path / "agent-runs.db"
    first_service = AgenticService(database_path=database, signing_secret=b"s" * 32)
    report = await first_service.execute(
        AgentRunRequest(
            task="Find the checkout authorization owner.",
            scenario="simple-lookup",
            run_id="agent_persist_001",
        )
    )
    event_count = len(report.events)
    await first_service.close()

    restarted = AgenticService(database_path=database, signing_secret=b"s" * 32)
    restored = restarted.get_run(report.id)

    assert restored.status == "completed"
    assert len(restored.events) == event_count
    assert restored.events[-1].type == "agent.run.completed"
    await restarted.close()


@pytest.mark.asyncio
async def test_restart_replays_a_terminal_event_ahead_of_report_checkpoint(tmp_path) -> None:
    database = tmp_path / "terminal-checkpoint.db"
    service = AgenticService(database_path=database, signing_secret=b"s" * 32)
    request = AgentRunRequest(
        task="Find the checkout authorization owner.",
        scenario="simple-lookup",
        run_id="agent_checkpoint_001",
    )
    report = await service.execute(request)
    queued = AgenticService._queued_report(
        request,
        build_scenario(request.scenario),
        request.run_id,
    )
    service.store.save(queued)
    await service.close()

    restarted = AgenticService(database_path=database, signing_secret=b"s" * 32)
    recovered = restarted.get_run(report.id)

    assert recovered.status == "completed"
    assert recovered.answer == report.answer
    assert recovered.steps[0].status == "succeeded"
    assert recovered.steps[0].output == report.steps[0].output
    assert recovered.events[-1].type == "agent.run.completed"
    await restarted.close()


@pytest.mark.asyncio
async def test_idempotency_key_reuses_only_the_same_request() -> None:
    service = AgenticService(signing_secret=b"s" * 32)
    request = AgentRunRequest(
        task="Find the checkout authorization owner.",
        scenario="simple-lookup",
    )

    first = await service.submit(request, idempotency_key="owner-lookup")
    duplicate = await service.submit(request, idempotency_key="owner-lookup")

    assert duplicate.id == first.id
    with pytest.raises(IdempotencyConflict):
        await service.submit(
            AgentRunRequest(task="Find another service owner.", scenario="simple-lookup"),
            idempotency_key="owner-lookup",
        )
    await service.wait(first.id)
    await service.close()


def test_startup_recovery_terminates_an_abandoned_run(tmp_path) -> None:
    database = tmp_path / "recovery.db"
    store = SQLiteAgentRunStore(database)
    request = AgentRunRequest(
        task="Find the checkout authorization owner.",
        scenario="simple-lookup",
        run_id="agent_abandoned_001",
    )
    scenario = build_scenario(request.scenario)
    queued = AgenticService._queued_report(request, scenario, request.run_id)
    store.reserve(queued, request_hash="sha256:fixture", idempotency_key=None)
    store.close()

    restarted = AgenticService(database_path=database)
    recovered = restarted.get_run(request.run_id)

    assert recovered.status == "failed"
    assert recovered.error == "worker interrupted by process restart"
    assert recovered.events[-1].type == "agent.run.failed"
    assert recovered.events[-1].payload["recovered"] is True
    restarted.store.close()


@pytest.mark.asyncio
async def test_agent_artifact_has_stable_execution_fingerprint_and_detects_tampering() -> None:
    first, _ = await execute_scenario("enterprise-recovery")
    await asyncio.sleep(0)
    second, _ = await execute_scenario("enterprise-recovery")
    first_artifact = build_agent_artifact(first)
    second_artifact = build_agent_artifact(second)

    assert first_artifact.execution_fingerprint == second_artifact.execution_fingerprint
    assert replay_agent_artifact(first_artifact).valid is True

    tampered = first_artifact.model_dump(mode="json")
    tampered["run"]["answer"] = "tampered output"
    replay = replay_agent_artifact(tampered)

    assert replay.valid is False
    assert replay.integrity_match is False
    assert replay.execution_fingerprint_match is False


@pytest.mark.parametrize(
    "scenario",
    [
        "enterprise-recovery",
        "rollback-on-deploy-failure",
        "prompt-injection",
        "replan-fallback",
        "simple-lookup",
    ],
)
@pytest.mark.asyncio
async def test_every_reference_scenario_exports_a_valid_artifact(scenario: str) -> None:
    report, _ = await execute_scenario(scenario)

    replay = replay_agent_artifact(build_agent_artifact(report))

    assert replay.valid is True, replay.model_dump_json(indent=2)
    assert all(check.passed for check in replay.checks)


@pytest.mark.asyncio
async def test_cancellation_interrupts_an_inflight_tool() -> None:
    started = asyncio.Event()
    cancellation = asyncio.Event()

    async def slow_tool(_: dict[str, object]) -> dict[str, bool]:
        started.set()
        await asyncio.sleep(10)
        return {"finished": True}

    registry = AgentToolRegistry()
    registry.register(
        AgentToolSpec(name="fixture.slow", description="Wait until cancelled"),
        slow_tool,
    )
    run_id = "agent_cancel_001"
    issuer = CapabilityIssuer(b"s" * 32)
    claims = issuer.verify(
        issuer.issue(subject="test", run_id=run_id, capabilities=[]),
        run_id=run_id,
    )
    plane = AgenticControlPlane(registry)
    task = asyncio.create_task(
        plane.run(
            "Run a slow cancellable tool.",
            ExecutionPlan(
                id="cancel-test",
                objective="Prove in-flight cancellation",
                steps=[
                    PlanStep(id="wait", title="Wait", tool="fixture.slow")
                ],
            ),
            claims=claims,
            scenario="test",
            run_id=run_id,
            cancellation=cancellation,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    cancellation.set()
    report = await asyncio.wait_for(task, timeout=1)

    assert report.status == "cancelled"
    assert report.steps[0].status == "cancelled"
    assert report.events[-1].type == "agent.run.cancelled"
