# Platform Expansion And Scale

HarnessLab is being expanded toward a real multi-package platform. The target of 100k source lines is
tracked as an engineering capacity target, not a reason to generate duplicate code.

## Current platform packages

| Package | Responsibility |
| --- | --- |
| `platform.tenancy` | tenant, principal, roles, permissions, deny-by-default authorization |
| `platform.quotas` | atomic tenant/subject reservations for tokens, tools, cost, and concurrency |
| `platform.audit` | redacted append-only hash-chain records with optional HMAC signatures |
| `platform.observability` | bounded counters, gauges, histograms, and spans |
| `platform.health` | isolated liveness/readiness checks |
| `platform.mcp_gateway` | MCP server lifecycle, discovery cache, circuit breaker, concurrency, routing |
| `platform.evaluation` | versioned JSONL datasets, bounded replay execution, contract assertions |
| `platform.model_router` | hard constraints before quality/latency weighted endpoint selection |
| `platform.scale` | reproducible source/test line-count report |

## Measurement

Run from the repository root:

```powershell
.venv\Scripts\harnesslab.exe scale-report --target-lines 100000
```

The report excludes `.git`, virtual environments, caches, and temporary build directories. It reports
source lines, test lines, files, package buckets, and remaining distance to the target.

## Next real packages

The remaining expansion should come from independently useful boundaries:

1. Durable Postgres/Redis/vector-store implementations behind storage protocols.
2. MCP registry persistence, egress policy, health polling, and signed server manifests.
3. OpenTelemetry/Prometheus exporters and tenant-level SLO aggregation.
4. Provider fixture packs, replay corpora, mutation testing, and regression triage.
5. API authentication middleware, tenant-scoped resource authorization, RBAC administration, and audit export.
6. Pi-compatible session steering, compaction hooks, and extension SDK compatibility fixtures.

Every package must ship with typed contracts, failure-path tests, resource limits, documentation, and an
upgrade/rollback story. A code-line total without those properties is not treated as progress.
