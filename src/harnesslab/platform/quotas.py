from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class QuotaKind(StrEnum):
    REQUESTS = "requests"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOOL_CALLS = "tool_calls"
    COST_USD = "cost_usd"
    CONCURRENCY = "concurrency"


class QuotaLimit(BaseModel):
    tenant_id: str = Field(min_length=1)
    subject: str = Field(default="*", min_length=1)
    limits: dict[QuotaKind, float] = Field(default_factory=dict)
    window_seconds: float = Field(default=86_400, gt=0)


class QuotaReservation(BaseModel):
    id: str
    tenant_id: str
    subject: str
    values: dict[QuotaKind, float]
    expires_at: float
    released: bool = False


class QuotaExceeded(RuntimeError):
    def __init__(self, kind: QuotaKind, requested: float, remaining: float) -> None:
        self.kind = kind
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"quota exceeded for {kind}: requested={requested}, remaining={remaining}"
        )


@dataclass
class _Usage:
    started_at: float
    values: dict[QuotaKind, float]


class QuotaLedger:
    """Atomic in-memory quota reservations with tenant and subject scopes."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._limits: dict[tuple[str, str], QuotaLimit] = {}
        self._usage: dict[tuple[str, str], _Usage] = {}
        self._reservations: dict[str, QuotaReservation] = {}
        self._lock = threading.RLock()

    def configure(self, limit: QuotaLimit) -> None:
        with self._lock:
            self._limits[(limit.tenant_id, limit.subject)] = limit

    def reserve(
        self,
        tenant_id: str,
        subject: str,
        values: dict[QuotaKind, float],
        *,
        ttl_seconds: float | None = None,
    ) -> QuotaReservation:
        if any(value < 0 for value in values.values()):
            raise ValueError("quota reservation values cannot be negative")
        with self._lock:
            now = self._clock()
            self._expire(now)
            limits = self._resolve_limit(tenant_id, subject)
            usage = self._resolve_usage(tenant_id, subject, now, limits)
            for kind, requested in values.items():
                limit = limits.limits.get(kind)
                if limit is None:
                    continue
                remaining = max(0.0, limit - usage.values.get(kind, 0.0))
                if requested > remaining:
                    raise QuotaExceeded(kind, requested, remaining)
            for kind, requested in values.items():
                usage.values[kind] = usage.values.get(kind, 0.0) + requested
            reservation = QuotaReservation(
                id=f"quota_{uuid4().hex[:16]}",
                tenant_id=tenant_id,
                subject=subject,
                values=values,
                expires_at=now + (ttl_seconds or limits.window_seconds),
            )
            self._reservations[reservation.id] = reservation
            return reservation

    def release(self, reservation_id: str) -> None:
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation.released:
                return
            usage_key = (reservation.tenant_id, reservation.subject)
            usage = self._usage.get(usage_key)
            if usage is not None:
                for kind, value in reservation.values.items():
                    usage.values[kind] = max(0.0, usage.values.get(kind, 0.0) - value)
            reservation.released = True

    def usage(self, tenant_id: str, subject: str) -> dict[QuotaKind, float]:
        with self._lock:
            now = self._clock()
            self._expire(now)
            limit = self._resolve_limit(tenant_id, subject)
            usage = self._resolve_usage(tenant_id, subject, now, limit)
            return dict(usage.values)

    def remaining(self, tenant_id: str, subject: str) -> dict[QuotaKind, float]:
        with self._lock:
            limit = self._resolve_limit(tenant_id, subject)
            current = self.usage(tenant_id, subject)
            return {
                kind: max(0.0, value - current.get(kind, 0.0))
                for kind, value in limit.limits.items()
            }

    def _resolve_limit(self, tenant_id: str, subject: str) -> QuotaLimit:
        return self._limits.get((tenant_id, subject)) or self._limits.get(
            (tenant_id, "*"),
            QuotaLimit(tenant_id=tenant_id, subject=subject),
        )

    def _resolve_usage(self, tenant_id: str, subject: str, now: float, limit: QuotaLimit) -> _Usage:
        key = (tenant_id, subject)
        usage = self._usage.get(key)
        if usage is None or now - usage.started_at >= limit.window_seconds:
            usage = _Usage(started_at=now, values={})
            self._usage[key] = usage
        return usage

    def _expire(self, now: float) -> None:
        for reservation_id, reservation in list(self._reservations.items()):
            if reservation.expires_at <= now:
                self.release(reservation_id)
                self._reservations.pop(reservation_id, None)
