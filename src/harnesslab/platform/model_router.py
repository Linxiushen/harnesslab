from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from harnesslab.platform.mcp_gateway import CircuitBreaker


class ModelCapability(StrEnum):
    CHAT = "chat"
    TOOLS = "tools"
    JSON = "json"
    VISION = "vision"
    LONG_CONTEXT = "long_context"


class ModelEndpoint(BaseModel):
    endpoint_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    capabilities: frozenset[ModelCapability] = Field(
        default_factory=lambda: frozenset({ModelCapability.CHAT})
    )
    context_tokens: int = Field(default=16_000, ge=1)
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)
    quality_score: float = Field(default=0.5, ge=0, le=1)
    latency_score: float = Field(default=0.5, ge=0, le=1)
    enabled: bool = True


class ModelRequestConstraints(BaseModel):
    required_capabilities: frozenset[ModelCapability] = Field(default_factory=frozenset)
    min_context_tokens: int = Field(default=1, ge=1)
    max_input_cost_per_million: float | None = Field(default=None, ge=0)
    max_output_cost_per_million: float | None = Field(default=None, ge=0)
    preferred_provider: str | None = None
    min_quality_score: float = Field(default=0, ge=0, le=1)


class ModelRoutingDecision(BaseModel):
    selected_endpoint: str
    provider: str
    model: str
    candidates: list[str]
    rejected: dict[str, str] = Field(default_factory=dict)
    reason: str


class ModelRouter:
    """Constraint-first endpoint selector with per-endpoint circuit state."""

    def __init__(self, *, failure_threshold: int = 3, reset_timeout_seconds: float = 30) -> None:
        self._endpoints: dict[str, ModelEndpoint] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._reset_timeout_seconds = reset_timeout_seconds

    def register(self, endpoint: ModelEndpoint) -> None:
        if endpoint.endpoint_id in self._endpoints:
            raise ValueError(f"model endpoint already registered: {endpoint.endpoint_id}")
        self._endpoints[endpoint.endpoint_id] = endpoint
        self._breakers[endpoint.endpoint_id] = CircuitBreaker(
            failure_threshold=self._failure_threshold,
            reset_timeout_seconds=self._reset_timeout_seconds,
        )

    def select(self, constraints: ModelRequestConstraints) -> ModelRoutingDecision:
        candidates: list[tuple[float, ModelEndpoint]] = []
        rejected: dict[str, str] = {}
        for endpoint in self._endpoints.values():
            reason = self._reject_reason(endpoint, constraints)
            if reason is None and not self._breakers[endpoint.endpoint_id].allow():
                reason = "circuit is open"
            if reason is not None:
                rejected[endpoint.endpoint_id] = reason
                continue
            provider_bonus = 1.0 if endpoint.provider == constraints.preferred_provider else 0.0
            score = (
                endpoint.quality_score * 0.6
                + endpoint.latency_score * 0.2
                + provider_bonus * 0.2
            )
            candidates.append((score, endpoint))
        if not candidates:
            raise LookupError(f"no model endpoint satisfies constraints: {rejected}")
        candidates.sort(key=lambda item: (item[0], item[1].endpoint_id), reverse=True)
        selected = candidates[0][1]
        return ModelRoutingDecision(
            selected_endpoint=selected.endpoint_id,
            provider=selected.provider,
            model=selected.model,
            candidates=[endpoint.endpoint_id for _, endpoint in candidates],
            rejected=rejected,
            reason="highest weighted quality/latency score after hard constraints",
        )

    def record_success(self, endpoint_id: str) -> None:
        self._breaker(endpoint_id).success()

    def record_failure(self, endpoint_id: str) -> None:
        self._breaker(endpoint_id).failure()

    def endpoints(self) -> list[ModelEndpoint]:
        return list(self._endpoints.values())

    def _reject_reason(
        self,
        endpoint: ModelEndpoint,
        constraints: ModelRequestConstraints,
    ) -> str | None:
        if not endpoint.enabled:
            return "endpoint is disabled"
        missing = constraints.required_capabilities - endpoint.capabilities
        if missing:
            return f"missing capabilities: {sorted(missing)}"
        if endpoint.context_tokens < constraints.min_context_tokens:
            return "context window is too small"
        if (
            constraints.max_input_cost_per_million is not None
            and endpoint.input_cost_per_million > constraints.max_input_cost_per_million
        ):
            return "input cost exceeds limit"
        if (
            constraints.max_output_cost_per_million is not None
            and endpoint.output_cost_per_million > constraints.max_output_cost_per_million
        ):
            return "output cost exceeds limit"
        if endpoint.quality_score < constraints.min_quality_score:
            return "quality score is below minimum"
        return None

    def _breaker(self, endpoint_id: str) -> CircuitBreaker:
        try:
            return self._breakers[endpoint_id]
        except KeyError as exc:
            raise LookupError(f"model endpoint not found: {endpoint_id}") from exc
