from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ComponentHealth(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    status: HealthStatus
    latency_ms: float | None = None
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReadinessReport(BaseModel):
    ready: bool
    status: HealthStatus
    components: list[ComponentHealth] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


HealthCheck = Callable[[], ComponentHealth | Awaitable[ComponentHealth]]


class HealthRegistry:
    """Health and readiness checks with isolated component failures."""

    def __init__(self, *, max_components: int = 256) -> None:
        self.max_components = max_components
        self._checks: dict[str, HealthCheck] = {}

    def register(self, name: str, check: HealthCheck) -> None:
        if name in self._checks:
            raise ValueError(f"health check already registered: {name}")
        if len(self._checks) >= self.max_components:
            raise RuntimeError("health component limit exceeded")
        self._checks[name] = check

    async def check(self, name: str) -> ComponentHealth:
        try:
            check = self._checks[name]
        except KeyError as exc:
            raise LookupError(f"health check not found: {name}") from exc
        started = datetime.now(UTC)
        try:
            result = check()
            result = await result if inspect.isawaitable(result) else result
            if result.name != name:
                result.name = name
            result.latency_ms = (datetime.now(UTC) - started).total_seconds() * 1000
            return result
        except Exception as exc:
            return ComponentHealth(
                name=name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )

    async def readiness(self) -> ReadinessReport:
        components = [await self.check(name) for name in self._checks]
        if not components:
            return ReadinessReport(ready=True, status=HealthStatus.UNKNOWN)
        if any(component.status == HealthStatus.UNHEALTHY for component in components):
            status = HealthStatus.UNHEALTHY
        elif any(component.status == HealthStatus.DEGRADED for component in components):
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.HEALTHY
        return ReadinessReport(
            ready=status in {HealthStatus.HEALTHY, HealthStatus.DEGRADED},
            status=status,
            components=components,
        )

    def names(self) -> list[str]:
        return list(self._checks)
