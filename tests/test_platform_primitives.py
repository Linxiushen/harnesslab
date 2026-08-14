from __future__ import annotations

import pytest

from harnesslab.platform.audit import AuditChain, AuditIntegrityError
from harnesslab.platform.health import ComponentHealth, HealthRegistry, HealthStatus
from harnesslab.platform.observability import MetricRegistry
from harnesslab.platform.quotas import QuotaExceeded, QuotaKind, QuotaLedger, QuotaLimit
from harnesslab.platform.scale import analyze_repository
from harnesslab.platform.tenancy import Permission, Principal, Tenant, TenantRegistry


def test_tenant_registry_denies_unknown_and_grants_role_permission() -> None:
    registry = TenantRegistry()
    registry.register_tenant(Tenant(id="tenant-a", display_name="A"))
    principal = Principal(subject="alice", tenant_id="tenant-a", roles=frozenset({"operator"}))
    registry.bind(principal)

    allowed = registry.authorize(principal, Permission.RUN_CREATE)
    denied = registry.authorize(principal, Permission.TOOL_WRITE)

    assert allowed.allowed is True
    assert allowed.matched_roles == ["operator"]
    assert denied.allowed is False
    with pytest.raises(PermissionError):
        registry.require(principal, Permission.TOOL_WRITE)


def test_quota_reservation_is_atomic_and_release_is_idempotent() -> None:
    ledger = QuotaLedger()
    ledger.configure(
        QuotaLimit(tenant_id="tenant-a", subject="*", limits={QuotaKind.TOOL_CALLS: 2})
    )
    reservation = ledger.reserve("tenant-a", "alice", {QuotaKind.TOOL_CALLS: 1})
    assert ledger.usage("tenant-a", "alice")[QuotaKind.TOOL_CALLS] == 1
    with pytest.raises(QuotaExceeded):
        ledger.reserve("tenant-a", "alice", {QuotaKind.TOOL_CALLS: 2})
    ledger.release(reservation.id)
    ledger.release(reservation.id)
    assert ledger.usage("tenant-a", "alice").get(QuotaKind.TOOL_CALLS, 0) == 0


def test_audit_chain_detects_tampering_and_redacts_payload() -> None:
    chain = AuditChain(signing_secret=b"secret")
    chain.append(
        "policy.decision",
        tenant_id="tenant-a",
        subject="alice",
        payload={"api_key": "sk-live-not-for-export"},
    )
    exported = chain.export()
    assert chain.verify() is True
    assert exported[0]["payload"]["api_key"] == "[REDACTED]"

    imported = AuditChain(signing_secret=b"secret")
    imported.import_records([chain.records()[0]])
    assert imported.verify() is True
    tampered = chain.records()[0].model_copy(update={"payload": {"allowed": False}})
    with pytest.raises(AuditIntegrityError):
        AuditChain(signing_secret=b"secret").import_records([tampered])


def test_metrics_are_bounded_and_spans_capture_errors() -> None:
    registry = MetricRegistry(max_metrics=1, max_spans=2)
    registry.counter("runs").inc(2)
    with registry.trace("agent.run", attributes={"tenant": "tenant-a"}) as span:
        span.set_attribute("scenario", "simple-lookup")
    with pytest.raises(ValueError), registry.trace("failed"):
        raise ValueError("fixture")
    assert registry.snapshot()["metrics"]
    assert len(registry.snapshot()["spans"]) == 2
    with pytest.raises(RuntimeError, match="cardinality"):
        registry.gauge("other")


@pytest.mark.asyncio
async def test_health_registry_isolates_failures_and_allows_degraded_readiness() -> None:
    health = HealthRegistry()
    health.register(
        "database",
        lambda: ComponentHealth(name="database", status=HealthStatus.HEALTHY),
    )

    async def degraded() -> ComponentHealth:
        return ComponentHealth(name="cache", status=HealthStatus.DEGRADED)

    def broken() -> ComponentHealth:
        raise RuntimeError("connection refused")

    health.register("cache", degraded)
    health.register("queue", broken)
    report = await health.readiness()
    assert report.ready is False
    assert report.status == HealthStatus.UNHEALTHY
    assert report.components[-1].detail.startswith("RuntimeError")


def test_scale_report_excludes_build_directories_and_tracks_target(tmp_path) -> None:
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "src" / "demo" / "service.py").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "tests" / "test_service.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / ".venv" / "ignored.py").write_text("ignored\n", encoding="utf-8")

    report = analyze_repository(tmp_path, target_lines=10)

    assert report.total_files == 2
    assert report.total_lines == 3
    assert report.test_lines == 1
    assert report.remaining_lines == 7
