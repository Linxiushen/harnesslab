# HarnessLab Architecture

HarnessLab is a provider-neutral control plane for Agent runs. It borrows the
useful shape of a Pi-style runtime loop—messages, tools, extensions, steering,
compaction, and session state—while keeping provider wire formats behind small
adapters. The public DeepSeek API is supported through its OpenAI-compatible
surface. The project does not claim compatibility with an unpublished DeepSeek
Harness protocol.

## Design goals

- Make the orchestration layer testable without an LLM key.
- Make every policy, tool, recovery, and terminal decision observable.
- Keep context, tool exposure, cost, wall time, and concurrency bounded.
- Recover interrupted work without replaying non-idempotent side effects.
- Export redacted, portable evidence that can be compared in CI.
- Keep vendor and provider changes at adapter boundaries.

## Runtime layers

```text
API / CLI / Workbench
        |
AgenticService              durable submission, cancellation, recovery
        |
AgenticControlPlane         routing, DAG scheduling, retry/replan/rollback
        |
Policy + Budget + Context   capabilities, taint, token/tool/cost/time limits
        |
Tool Registry + MCP         normalized JSON Schema and explicit approval
        |
HarnessAdapter              DeepSeek API, OpenAI-compatible, deterministic demo
```

The platform package adds the deployment plane around this execution core:
tenant/RBAC authorization, atomic quotas, hash-chain audit, bounded metrics,
readiness checks, an MCP server gateway, versioned evaluation datasets, and
constraint-first model endpoint routing. These are intentionally separate from
the run scheduler so a storage or transport implementation can evolve without
changing run semantics.

The trace and artifact contracts cross all layers. They are intentionally not a
copy of a provider's private protocol.

## Context and tool exposure

The runtime does not expose every registered MCP tool on every turn. It creates a
task-scoped tool view using:

1. lexical task relevance;
2. normalized JSON Schema with unsupported provider metadata removed;
3. a maximum tool count;
4. a maximum estimated schema-token budget.

The selected and dropped tool names, budget, and estimate are recorded in run
metadata and the `model.requested` event. This makes context control inspectable.
The same principle applies to message context through `ContextCompiler` and to
tool results through redaction and artifact projection.

## MCP lifecycle

`MCPStdioProvider` has an explicit `start()` and `close()` lifecycle. Discovery and
tool calls reuse one initialized stdio session. A caller that owns the provider is
responsible for closing it during application shutdown. This avoids starting a
new child process for every call and gives transport failures a clear boundary.

## Extension lifecycle

Extensions are registered before activation. `ExtensionManager` starts them in
registration order, dispatches events only to active extensions, isolates callback
failures, and stops active extensions in reverse order. Discovery is therefore not
activation, and resource ownership has a deterministic teardown path.

## Session events

`SessionEventBus` is a bounded coordination primitive, not a raw transcript bus.
Events have a global sequence, a session scope, a bounded replay window, and a
bounded subscriber queue. Consumers can resume from a sequence number. Payloads
should be redacted before publication when they contain user or provider data.

## Recovery and side effects

The scheduler distinguishes transient, permanent, policy, budget, invalid-output,
and cancellation failures. Retry is allowed only for idempotent tools. Replanning
is explicit in the plan. Rollback executes registered compensations in reverse
completion order. Non-read-only tools fail closed without an approval provider.

## DeepSeek boundary

The adapter surface currently supports the public DeepSeek API. A future native
Harness adapter must enter through fixtures and conformance tests:

1. freeze capability and lifecycle fixtures;
2. map native events to the stable trace vocabulary;
3. run generic contract tests;
4. add provider-specific tests without weakening generic invariants;
5. publish only the compatibility scope proven by fixtures.

## Review checklist

- Does a new provider change only an adapter and fixture set?
- Is every new side effect represented by a policy decision?
- Is every unbounded collection given a limit and eviction policy?
- Can an interrupted run resume without duplicate side effects?
- Can a reviewer reproduce the behavior offline from an artifact?
- Does the failure path have a test, not just the happy path?
