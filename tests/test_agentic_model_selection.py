from __future__ import annotations

import pytest

from harnesslab.agentic.model_selection import (
    ModelSelector,
    ModelWorkload,
    NoEligibleModel,
    reference_profiles,
)


def test_tool_workload_selects_reliable_base_over_domain_fine_tune() -> None:
    decision = ModelSelector().select(
        reference_profiles(),
        ModelWorkload(
            input_tokens=10_000,
            expected_output_tokens=1_000,
            requires_tools=True,
            domain="agentic-tools",
            min_tool_success=0.9,
        ),
    )

    assert decision.selected_model == "general-base"
    fine_tuned = next(
        candidate for candidate in decision.candidates if candidate.model_id == "domain-sft"
    )
    assert fine_tuned.eligible is False
    assert any("tool success" in reason for reason in fine_tuned.reasons)


def test_model_selection_fails_closed_when_no_candidate_meets_constraints() -> None:
    with pytest.raises(NoEligibleModel, match="no model satisfies"):
        ModelSelector().select(
            reference_profiles(),
            ModelWorkload(
                input_tokens=100_000,
                expected_output_tokens=10_000,
                min_quality=0.99,
                max_cost_per_request=0.00001,
            ),
        )
