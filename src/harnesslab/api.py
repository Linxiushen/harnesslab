from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from harnesslab import __version__
from harnesslab.adapters import (
    DeepSeekAPIAdapter,
    DemoAdapter,
    OpenAICompatibleAdapter,
    RegressionFixtureAdapter,
)
from harnesslab.agentic.artifact import AgentRunArtifact
from harnesslab.agentic.contracts import AgentRunReport, AgentRunRequest, EvalReport
from harnesslab.agentic.service import AgenticService
from harnesslab.agentic.store import DuplicateRun, IdempotencyConflict
from harnesslab.conformance import ConformanceReport, run_conformance
from harnesslab.models import RunRecord, RunRequest, TraceCompareRequest
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import build_demo_registry
from harnesslab.trace_contract import (
    TRACE_CONTRACT_VERSION,
    TraceArtifact,
    TraceComparison,
    build_trace_artifact,
    compare_trace_artifacts,
)

STATIC_DIR = Path(__file__).parent / "static"
TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}


def build_runtime() -> HarnessRuntime:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())
    runtime.register_adapter(RegressionFixtureAdapter())
    if api_key := os.getenv("DEEPSEEK_API_KEY"):
        runtime.register_adapter(DeepSeekAPIAdapter(api_key=api_key))
    if base_url := os.getenv("HARNESSLAB_BASE_URL"):
        runtime.register_adapter(
            OpenAICompatibleAdapter(
                base_url=base_url,
                api_key=os.getenv("HARNESSLAB_API_KEY", ""),
                model=os.getenv("HARNESSLAB_MODEL", "default"),
            )
        )
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime: HarnessRuntime = app.state.runtime
    seed = await runtime.run(
        "Review the checkout authorization change and report the highest-risk regression.",
        adapter_name="demo",
    )
    app.state.conformance = await run_conformance(runtime, "demo")
    regression = await runtime.run(
        "Review the checkout authorization change and report the highest-risk regression.",
        adapter_name="regression-fixture",
    )
    app.state.featured_run_id = seed.id
    app.state.regression_run_id = regression.id
    agent_service: AgenticService = app.state.agent_service
    if not agent_service.list_runs():
        await agent_service.execute(
            AgentRunRequest(
                task=(
                    "Investigate the production checkout authorization regression, compare "
                    "evidence, apply a safe fix, verify it, and deploy with rollback protection."
                ),
                scenario="enterprise-recovery",
            )
        )
    await agent_service.evaluate()
    try:
        yield
    finally:
        await agent_service.close()


def create_app(
    runtime: HarnessRuntime | None = None,
    *,
    seed_demo: bool = True,
    agent_service: AgenticService | None = None,
) -> FastAPI:
    chosen_lifespan = lifespan if seed_demo else None
    app = FastAPI(
        title="HarnessLab",
        version=__version__,
        description="Protocol-first conformance and trace console for agent harnesses.",
        lifespan=chosen_lifespan,
    )
    app.state.runtime = runtime or build_runtime()
    database_path = (
        os.getenv("HARNESSLAB_AGENT_DB", ".harnesslab/agent-runs.db")
        if seed_demo
        else ":memory:"
    )
    signing_secret = os.getenv("HARNESSLAB_AGENT_SIGNING_SECRET")
    app.state.agent_service = agent_service or AgenticService(
        database_path=database_path,
        signing_secret=signing_secret.encode("utf-8") if signing_secret else None,
    )
    app.state.conformance = None
    app.state.background_runs = set()

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/meta")
    async def meta() -> dict[str, object]:
        active_runtime: HarnessRuntime = app.state.runtime
        return {
            "name": "HarnessLab",
            "version": __version__,
            "mode": "runtime",
            "trace_contract": TRACE_CONTRACT_VERSION,
            "protocol_status": "adapter-ready",
            "adapters": list(active_runtime.adapters),
            "tools": [spec.model_dump() for spec in active_runtime.tools.specs()],
            "featured_run_id": getattr(app.state, "featured_run_id", None),
            "regression_run_id": getattr(app.state, "regression_run_id", None),
            "agentic_control_plane": {
                "contract": "harnesslab.agent-run/v1",
                "features": [
                    "explainable-routing",
                    "bounded-dag-scheduling",
                    "capability-policy",
                    "retry-replan-rollback",
                    "prompt-injection-containment",
                    "token-cost-tool-budgets",
                    "sqlite-durable-runs",
                    "idempotent-submission",
                    "live-sse-and-cancellation",
                    "portable-replay-artifacts",
                ],
                "scenarios": [item["id"] for item in AgenticService.scenarios()],
            },
        }

    @app.get("/api/agent/scenarios")
    async def agent_scenarios() -> list[dict[str, str]]:
        return AgenticService.scenarios()

    @app.get("/api/agent/runs", response_model=list[AgentRunReport])
    async def list_agent_runs() -> list[AgentRunReport]:
        return app.state.agent_service.list_runs()

    @app.get("/api/agent/runs/{run_id}", response_model=AgentRunReport)
    async def get_agent_run(run_id: str) -> AgentRunReport:
        try:
            return app.state.agent_service.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Agent run not found") from exc

    @app.post(
        "/api/agent/runs",
        response_model=AgentRunReport,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_agent_run(
        request: AgentRunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentRunReport:
        try:
            return await app.state.agent_service.submit(
                request,
                idempotency_key=idempotency_key,
            )
        except (DuplicateRun, IdempotencyConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/agent/runs/{run_id}/events")
    async def agent_run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            app.state.agent_service.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Agent run not found") from exc

        async def stream() -> AsyncIterator[str]:
            async for event in app.state.agent_service.subscribe(run_id, after=after):
                yield (
                    f"id: {event.sequence}\nevent: {event.type}\n"
                    f"data: {event.model_dump_json()}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/agent/runs/{run_id}/cancel",
        response_model=AgentRunReport,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_agent_run(run_id: str) -> AgentRunReport:
        try:
            return app.state.agent_service.cancel(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Agent run not found") from exc

    @app.get("/api/agent/runs/{run_id}/artifact", response_model=AgentRunArtifact)
    async def export_agent_run(run_id: str) -> AgentRunArtifact:
        try:
            return app.state.agent_service.artifact(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Agent run not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/agent/evals", response_model=EvalReport | None)
    async def get_agent_evals() -> EvalReport | None:
        return app.state.agent_service.last_eval

    @app.post("/api/agent/evals", response_model=EvalReport)
    async def run_agent_evals() -> EvalReport:
        return await app.state.agent_service.evaluate()

    @app.get("/api/runs", response_model=list[RunRecord])
    async def list_runs() -> list[RunRecord]:
        return app.state.runtime.list_runs()

    @app.get("/api/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: str) -> RunRecord:
        try:
            return app.state.runtime.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.get("/api/runs/{run_id}/artifact", response_model=TraceArtifact)
    async def export_run(run_id: str) -> TraceArtifact:
        try:
            return build_trace_artifact(app.state.runtime.get_run(run_id))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.post("/api/compare", response_model=TraceComparison)
    async def compare_runs(request: TraceCompareRequest) -> TraceComparison:
        try:
            baseline = build_trace_artifact(app.state.runtime.get_run(request.baseline_run_id))
            candidate = build_trace_artifact(app.state.runtime.get_run(request.candidate_run_id))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return compare_trace_artifacts(baseline, candidate)

    @app.post("/api/runs", response_model=RunRecord, status_code=202)
    async def create_run(request: RunRequest) -> RunRecord:
        active_runtime: HarnessRuntime = app.state.runtime
        if request.adapter not in active_runtime.adapters:
            raise HTTPException(status_code=400, detail=f"Unknown adapter: {request.adapter}")
        run_id = f"run_{os.urandom(6).hex()}"
        task = asyncio.create_task(
            active_runtime.run(
                request.task,
                adapter_name=request.adapter,
                max_turns=request.max_turns,
                run_id=run_id,
            )
        )
        app.state.background_runs.add(task)
        task.add_done_callback(app.state.background_runs.discard)
        await asyncio.sleep(0)
        return active_runtime.get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            active_runtime: HarnessRuntime = app.state.runtime
            active_runtime.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

        async def stream() -> AsyncIterator[str]:
            async for event in active_runtime.events.subscribe(run_id, after=after):
                payload = event.model_dump_json()
                yield f"id: {event.sequence}\nevent: {event.type}\ndata: {payload}\n\n"
                if event.type in TERMINAL_EVENTS:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/conformance", response_model=ConformanceReport)
    async def conformance(adapter: str = "demo") -> ConformanceReport:
        active_runtime: HarnessRuntime = app.state.runtime
        if adapter not in active_runtime.adapters:
            raise HTTPException(status_code=400, detail=f"Unknown adapter: {adapter}")
        report = await run_conformance(active_runtime, adapter)
        app.state.conformance = report
        return report

    @app.get("/api/conformance", response_model=ConformanceReport | None)
    async def get_conformance() -> ConformanceReport | None:
        return app.state.conformance

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
