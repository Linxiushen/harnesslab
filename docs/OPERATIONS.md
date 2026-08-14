# Operations Guide

## Safe defaults

The reference runtime is offline and deterministic. Production deployments should
keep the following defaults until an explicit policy provider is configured:

- read-only tools may be auto-approved;
- side-effecting tools require explicit approval;
- live execution is disabled;
- context, tool-call, cost, and wall-time budgets are mandatory;
- raw model context is never written to exported artifacts;
- MCP providers must be closed during process shutdown.

## Production deployment

Run the API behind an authenticated reverse proxy and a process supervisor. Set a
stable `HARNESSLAB_AGENT_SIGNING_SECRET` and a durable `HARNESSLAB_AGENT_DB` path.
Use a separate database per environment; do not share a development SQLite file
with production workers.

```bash
pip install -e ".[dev,mcp]"
harnesslab check --adapter demo
harnesslab agent-eval
harnesslab serve --host 127.0.0.1 --port 4318
```

## Shutdown and recovery

Send a graceful termination signal first. The service marks active cancellation,
waits for worker tasks, closes providers, and then closes its event store. On the
next startup, inspect recovered runs before allowing new side effects. A recovered
run must be reconciled against external state before a non-idempotent action is
resumed.

## Observability

Monitor:

- run status and terminal-event uniqueness;
- budget denials and context exposure drops;
- tool latency, timeout, retry, and failure kind;
- policy denials and prompt-injection detections;
- MCP start, close, and transport failures;
- artifact fingerprint and replay failures.

Never log API keys, bearer tokens, raw prompts, private memory, or full tool
payloads. Use the redaction layer before events reach SSE, logs, or artifacts.

## Incident procedure

1. Freeze side-effecting tools at the policy boundary.
2. Export the affected run artifact and its fingerprint.
3. Record the provider, model, adapter, tool schema fingerprint, and policy version.
4. Reproduce with the deterministic scenario or an approved redacted fixture.
5. Add a regression test before changing the recovery path.
6. Re-enable side effects only after conformance and agent-eval pass.
