# Changelog

## 0.6.0

- Added API-key digest storage, signed bearer claims, signed MCP manifests, egress allowlists, and SQLite CAS/TTL storage.

## 0.5.0

- Added multi-tenant RBAC, atomic quotas, hash-chain audit records, bounded metrics, and health/readiness primitives.
- Added MCP Gateway server registration, discovery, circuit breaking, concurrency limits, and bounded tool exposure.
- Added versioned evaluation datasets, replay runner, constraint-first model routing, and `scale-report`.

All notable changes to HarnessLab are documented here.

## Unreleased

### Added

- Public developer-requirement evidence matrix with links to Pi community issues.
- Bounded query-aware tool exposure with provider-safe JSON Schema normalization.
- Deterministic extension activation, failure isolation, and reverse-order teardown.
- Bounded cross-session event bus with sequence-based replay.
- Reusable MCP stdio session lifecycle with explicit `start()` and `close()`.
- Architecture and operations documents for security and production review.
- Added a 15-slide Chinese architecture review deck with source notes and a realistic scale roadmap.

### Changed

- The runtime records exposed, dropped, and estimated tool-schema budgets per run.

## 0.4.0 - 2026-08-10

### Added

- SQLite-backed Agent Run and ordered event persistence with startup recovery.
- Atomic `Idempotency-Key` submission semantics and duplicate `run_id` protection.
- Background Agent Run execution with `202 Accepted`, live SSE, and public cancellation.
- Portable `harnesslab.agent-run/v1` artifacts with stable execution and integrity fingerprints.
- Offline `agent-replay` invariant verification and tamper detection.
- Live workbench controls for cancellation and Agent Run artifact export.

### Changed

- In-flight tool calls now observe cancellation instead of waiting for the next DAG batch.
- Agent Run reports remain inspectable after server restart.
- The public API exposes durable lifecycle behavior rather than synchronously waiting for a plan.

## 0.3.0 - 2026-08-10

### Added

- Explainable simple/complex task routing and validated execution DAGs.
- Bounded parallel scheduler with typed tool contracts and output validation.
- Failure classification with idempotent retry, fallback replanning, and saga compensation.
- Run-scoped HMAC capability tokens, egress gates, prompt-injection detection, and taint blocking.
- Message/token context windows and bounded TTL-aware long-term memory.
- Token, tool-call, cost, wall-time, and per-user traffic budgets.
- Constraint-first base-versus-fine-tuned model selection.
- Five deterministic failure scenarios and nine executable Agentic release gates.
- Agentic API, CLI commands, public static demo data, and control-plane workbench view.
- DeepSeek interview question-to-code playbook with timestamped answers.

## 0.2.0 - 2026-08-06

### Added

- Portable `harnesslab.trace/v1` artifacts.
- Stable protocol and content fingerprints.
- Structural comparisons for lifecycle, tool, policy, terminal, and invariant drift.
- `snapshot`, `verify`, and `compare` CLI commands with CI exit codes.
- Run artifact export and retained-run comparison API endpoints.
- Regression diff workbench with desktop and mobile layouts.
- Recursive credential redaction before event publication.
- Explicit `tool.denied` events for fail-closed side-effecting tools.

### Changed

- Raw model context is no longer serialized through run APIs.
- Project positioning now focuses on executable harness regression contracts.

## 0.1.0 - 2026-08-05

- Initial protocol-first runtime, trace console, conformance matrix, adapters, and MCP bridge.
