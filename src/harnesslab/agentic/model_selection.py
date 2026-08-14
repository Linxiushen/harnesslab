from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ModelProfile(BaseModel):
    id: str
    family: str
    variant: Literal["base", "fine-tuned"]
    context_window: int = Field(gt=0)
    input_cost_per_million: float = Field(ge=0)
    output_cost_per_million: float = Field(ge=0)
    p95_latency_ms: float = Field(gt=0)
    quality_score: float = Field(ge=0, le=1)
    tool_success_rate: float = Field(ge=0, le=1)
    safety_score: float = Field(ge=0, le=1)
    domains: list[str] = Field(default_factory=list)


class ModelWorkload(BaseModel):
    input_tokens: int = Field(ge=1)
    expected_output_tokens: int = Field(ge=1)
    requires_tools: bool = False
    domain: str | None = None
    min_quality: float = Field(default=0.75, ge=0, le=1)
    min_tool_success: float = Field(default=0.8, ge=0, le=1)
    min_safety: float = Field(default=0.8, ge=0, le=1)
    max_p95_latency_ms: float = Field(default=10_000, gt=0)
    max_cost_per_request: float = Field(default=0.1, gt=0)


class CandidateScore(BaseModel):
    model_id: str
    eligible: bool
    score: float
    estimated_cost: float
    reasons: list[str]


class ModelSelectionDecision(BaseModel):
    selected_model: str
    candidates: list[CandidateScore]
    rationale: str


class NoEligibleModel(RuntimeError):
    pass


class ModelSelector:
    """Constraint-first selector; weighted scoring only runs after hard gates pass."""

    def select(
        self,
        profiles: list[ModelProfile],
        workload: ModelWorkload,
    ) -> ModelSelectionDecision:
        scored = [self._score(profile, workload) for profile in profiles]
        eligible = [candidate for candidate in scored if candidate.eligible]
        if not eligible:
            reasons = "; ".join(
                f"{candidate.model_id}: {', '.join(candidate.reasons)}" for candidate in scored
            )
            raise NoEligibleModel(f"no model satisfies workload constraints; {reasons}")
        selected = max(eligible, key=lambda candidate: candidate.score)
        return ModelSelectionDecision(
            selected_model=selected.model_id,
            candidates=scored,
            rationale=(
                "Hard context, quality, safety, latency, tool, and cost gates passed; "
                f"{selected.model_id} has the highest quality/reliability/cost score."
            ),
        )

    @staticmethod
    def _score(profile: ModelProfile, workload: ModelWorkload) -> CandidateScore:
        reasons: list[str] = []
        total_tokens = workload.input_tokens + workload.expected_output_tokens
        estimated_cost = (
            workload.input_tokens / 1_000_000 * profile.input_cost_per_million
            + workload.expected_output_tokens / 1_000_000 * profile.output_cost_per_million
        )
        if total_tokens > profile.context_window:
            reasons.append(f"context {total_tokens}>{profile.context_window}")
        if profile.quality_score < workload.min_quality:
            reasons.append(f"quality {profile.quality_score:.3f}<{workload.min_quality:.3f}")
        if profile.safety_score < workload.min_safety:
            reasons.append(f"safety {profile.safety_score:.3f}<{workload.min_safety:.3f}")
        if profile.p95_latency_ms > workload.max_p95_latency_ms:
            reasons.append(f"p95 latency {profile.p95_latency_ms:.0f}ms exceeds limit")
        if workload.requires_tools and profile.tool_success_rate < workload.min_tool_success:
            reasons.append(
                f"tool success {profile.tool_success_rate:.3f}<{workload.min_tool_success:.3f}"
            )
        if estimated_cost > workload.max_cost_per_request:
            reasons.append(f"cost ${estimated_cost:.6f} exceeds limit")

        eligible = not reasons
        domain_bonus = 0.05 if workload.domain and workload.domain in profile.domains else 0.0
        reliability = (
            profile.tool_success_rate if workload.requires_tools else profile.quality_score
        )
        cost_efficiency = max(0.0, 1 - estimated_cost / workload.max_cost_per_request)
        latency_efficiency = max(0.0, 1 - profile.p95_latency_ms / workload.max_p95_latency_ms)
        score = (
            0.38 * profile.quality_score
            + 0.24 * reliability
            + 0.16 * profile.safety_score
            + 0.12 * cost_efficiency
            + 0.10 * latency_efficiency
            + domain_bonus
        )
        return CandidateScore(
            model_id=profile.id,
            eligible=eligible,
            score=round(score if eligible else 0.0, 6),
            estimated_cost=round(estimated_cost, 8),
            reasons=reasons or ["all hard constraints passed"],
        )


def reference_profiles() -> list[ModelProfile]:
    """Synthetic fixtures for tests and demos; they are not vendor benchmark claims."""

    return [
        ModelProfile(
            id="general-base",
            family="reference",
            variant="base",
            context_window=64_000,
            input_cost_per_million=0.28,
            output_cost_per_million=0.42,
            p95_latency_ms=820,
            quality_score=0.89,
            tool_success_rate=0.94,
            safety_score=0.93,
            domains=["agentic-tools"],
        ),
        ModelProfile(
            id="domain-sft",
            family="reference",
            variant="fine-tuned",
            context_window=32_000,
            input_cost_per_million=0.36,
            output_cost_per_million=0.52,
            p95_latency_ms=690,
            quality_score=0.93,
            tool_success_rate=0.77,
            safety_score=0.88,
            domains=["classification"],
        ),
    ]
