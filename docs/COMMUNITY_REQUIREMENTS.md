# Community Requirements Evidence

This document records public developer signals that shape HarnessLab. It is not a
statistical survey and does not claim that issue volume equals market demand. The
links below are representative, public discussions from the Pi agent ecosystem,
reviewed on 2026-08-14.

## Evidence matrix

| Public signal | Developer need | HarnessLab response |
| --- | --- | --- |
| [Pi #550](https://github.com/badlogic/pi-mono/issues/550) reports errors after resuming very large sessions | Resume must preserve diagnostics and fail with actionable provider context when a context window is exceeded | Bounded context compilation, explicit budget events, portable run artifacts, and replayable failure evidence |
| [Pi #803](https://github.com/badlogic/pi-mono/issues/803) requests `compact()` and context usage APIs | Extensions need a safe, observable way to inspect and compact context | `ContextCompiler`, token budgets, and the extension lifecycle boundary |
| [Pi #118](https://github.com/badlogic/pi-mono/issues/118) asks for hooks with UI integration | Harness authors need lifecycle hooks for snapshots, notifications, and operator actions | Ordered trace events and the new extension manager |
| [Pi #2714](https://github.com/badlogic/pi-mono/issues/2714) proposes a cross-session agent event bus | Sessions need durable, bounded coordination without leaking raw context | `SessionEventBus` with sequence numbers, bounded history, and scoped subscriptions |
| [Pi #3673](https://github.com/badlogic/pi-mono/issues/3673) asks for programmatic session control | Orchestrators need explicit session transitions rather than hidden UI state | Session-facing contracts are isolated from provider wire formats |
| [Pi #2928](https://github.com/badlogic/pi-mono/issues/2928) reports MCP stdio EPIPE and startup overhead | MCP processes need lifecycle reuse, clean shutdown, and transport diagnostics | `MCPStdioProvider.start/close`, persistent session reuse, and normalized tool contracts |
| [Pi #3214](https://github.com/badlogic/pi-mono/issues/3214) reports provider failures caused by oversized/unsupported MCP schema metadata | Tool schemas must be portable across providers and bounded before model exposure | Schema normalization strips unsupported metadata and enforces a token budget |
| [Pi #2677](https://github.com/badlogic/pi-mono/issues/2677) reports extensions keeping event-loop handles alive | Extensions need deterministic teardown and ownership of resources | Reverse-order extension shutdown with failure isolation |
| [Pi #776](https://github.com/badlogic/pi-mono/issues/776) reports `--no-extensions` still discovering extensions | Discovery and activation must be separate, explicit policy decisions | Extension manager separates registration from activation |

## Product requirements derived from the evidence

1. Every run has a bounded context, tool, cost, and wall-time budget.
2. Every external tool call is represented by a stable schema and a correlated trace event.
3. Provider-specific schema quirks stay behind an adapter boundary.
4. MCP transports have an explicit start, reuse, health, and close lifecycle.
5. Extensions can be loaded, activated, observed, and stopped without keeping the process alive.
6. Cross-session events are bounded, ordered, scoped, and replayable.
7. A failed or interrupted run exports enough redacted evidence to reproduce the state transition.
8. No compatibility claim is made for an unpublished DeepSeek Harness wire protocol.

## What is deliberately not inferred

- An issue is not proof that a feature is universally required.
- Public DeepSeek API compatibility is not the same as a DeepSeek Harness protocol.
- A passing deterministic fixture is not evidence of production task quality.
- Subscriber count, benchmark score, or prompt quality does not replace operational controls.

## Research follow-up

The repository should continue collecting issue links, RFCs, and adapter requests as
versioned evidence. New requirements should enter the project through an issue or
RFC, then become a contract test before implementation.
