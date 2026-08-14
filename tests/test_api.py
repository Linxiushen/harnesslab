from __future__ import annotations

import httpx
import pytest

from harnesslab.adapters import DemoAdapter
from harnesslab.api import create_app
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import build_demo_registry


@pytest.mark.asyncio
async def test_health_and_meta_are_available_without_model_credentials() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        meta = await client.get("/api/meta")

    assert health.json()["status"] == "ok"
    assert "demo" in meta.json()["adapters"]
    assert {tool["name"] for tool in meta.json()["tools"]} == {
        "search_repository",
        "inspect_change",
    }


@pytest.mark.asyncio
async def test_unknown_adapter_is_rejected() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs",
            json={"task": "Review this change", "adapter": "missing"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown adapter: missing"


@pytest.mark.asyncio
async def test_runs_can_be_exported_and_compared_as_trace_contracts() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())
    first = await runtime.run("Review this change")
    second = await runtime.run("Review this change")
    app = create_app(runtime=runtime, seed_demo=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_response = await client.get(f"/api/runs/{first.id}")
        artifact = await client.get(f"/api/runs/{first.id}/artifact")
        comparison = await client.post(
            "/api/compare",
            json={"baseline_run_id": first.id, "candidate_run_id": second.id},
        )

    assert artifact.status_code == 200
    assert "messages" not in run_response.json()
    assert artifact.json()["contract_version"] == "harnesslab.trace/v1"
    assert comparison.status_code == 200
    assert comparison.json()["compatible"] is True
    assert comparison.json()["protocol_score"] == 100


@pytest.mark.asyncio
async def test_agentic_scenarios_run_through_the_public_api() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        scenarios = await client.get("/api/agent/scenarios")
        run = await client.post(
            "/api/agent/runs",
            json={
                "task": "Investigate production, apply a fix, then deploy with rollback.",
                "scenario": "enterprise-recovery",
            },
        )
        fetched = await client.get(f"/api/agent/runs/{run.json()['id']}")
        evaluations = await client.post("/api/agent/evals")

    assert scenarios.status_code == 200
    assert len(scenarios.json()) == 5
    assert run.status_code == 202
    assert run.json()["status"] == "queued"
    assert fetched.json()["plan"]["max_parallelism"] == 2
    assert evaluations.json()["passed"] == evaluations.json()["total"] == 9


@pytest.mark.asyncio
async def test_agent_run_api_is_idempotent_and_exports_replay_artifact() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    payload = {
        "task": "Find the checkout authorization owner.",
        "scenario": "simple-lookup",
    }
    headers = {"Idempotency-Key": "checkout-owner-001"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/agent/runs", json=payload, headers=headers)
        duplicate = await client.post("/api/agent/runs", json=payload, headers=headers)
        conflict = await client.post(
            "/api/agent/runs",
            json={**payload, "task": "Find a different owner."},
            headers=headers,
        )
        run_id = first.json()["id"]
        await app.state.agent_service.wait(run_id)
        artifact = await client.get(f"/api/agent/runs/{run_id}/artifact")
        stream = await client.get(f"/api/agent/runs/{run_id}/events")

    assert first.status_code == duplicate.status_code == 202
    assert first.json()["id"] == duplicate.json()["id"]
    assert conflict.status_code == 409
    assert artifact.status_code == 200
    assert artifact.json()["contract_version"] == "harnesslab.agent-run/v1"
    assert artifact.json()["integrity_fingerprint"].startswith("sha256:")
    assert "event: agent.run.completed" in stream.text


@pytest.mark.asyncio
async def test_unknown_agentic_scenario_is_rejected() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/agent/runs",
            json={"task": "Run a useful scenario", "scenario": "missing"},
        )

    assert response.status_code == 400
    assert "unknown scenario" in response.json()["detail"]
