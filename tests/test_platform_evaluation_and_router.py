from __future__ import annotations

import pytest

from harnesslab.platform.evaluation import (
    DatasetRegistry,
    EvalCase,
    EvalDataset,
    EvalStatus,
    EvaluationRunner,
)
from harnesslab.platform.model_router import (
    ModelCapability,
    ModelEndpoint,
    ModelRequestConstraints,
    ModelRouter,
)


def test_dataset_versions_are_unique_and_jsonl_round_trips() -> None:
    dataset = EvalDataset(
        name="smoke",
        version="1",
        cases=[EvalCase(id="case-1", task="Review this", scenario="simple-lookup")],
    )
    registry = DatasetRegistry()
    registry.register(dataset)
    restored = EvalDataset.from_jsonl("smoke", "1-copy", dataset.to_jsonl())

    assert registry.get("smoke", "1").cases[0].id == "case-1"
    assert restored.cases == dataset.cases
    with pytest.raises(ValueError):
        registry.register(dataset)


@pytest.mark.asyncio
async def test_evaluation_runner_asserts_status_and_event_contracts() -> None:
    dataset = EvalDataset(
        name="contract",
        version="1",
        cases=[
            EvalCase(
                id="pass",
                task="Run the task",
                scenario="fixture",
                required_events=("agent.run.completed",),
            ),
            EvalCase(
                id="fail",
                task="Run the task",
                scenario="fixture",
                required_events=("agent.run.completed",),
            ),
        ],
    )

    async def execute(case: EvalCase) -> dict[str, object]:
        return {
            "status": "completed" if case.id == "pass" else "failed",
            "events": [{"type": "agent.run.completed"}],
        }

    report = await EvaluationRunner(execute, max_concurrency=2).run(dataset)
    assert report.passed == 1
    assert report.failed == 1
    assert report.results[0].status == EvalStatus.PASSED


def test_model_router_applies_hard_constraints_before_weighted_selection() -> None:
    router = ModelRouter()
    router.register(
        ModelEndpoint(
            endpoint_id="base",
            provider="deepseek",
            model="deepseek-chat",
            capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOLS}),
            context_tokens=64_000,
            quality_score=0.88,
            latency_score=0.70,
        )
    )
    router.register(
        ModelEndpoint(
            endpoint_id="small",
            provider="other",
            model="small-chat",
            capabilities=frozenset({ModelCapability.CHAT}),
            context_tokens=8_000,
            quality_score=0.95,
            latency_score=0.95,
        )
    )
    decision = router.select(
        ModelRequestConstraints(
            required_capabilities=frozenset({ModelCapability.TOOLS}),
            min_context_tokens=32_000,
        )
    )
    assert decision.selected_endpoint == "base"
    assert "small" in decision.rejected


def test_model_router_opens_endpoint_circuit_after_failures() -> None:
    router = ModelRouter(failure_threshold=2)
    router.register(ModelEndpoint(endpoint_id="base", provider="p", model="m"))
    router.record_failure("base")
    router.record_failure("base")
    with pytest.raises(LookupError, match="circuit"):
        router.select(ModelRequestConstraints())
