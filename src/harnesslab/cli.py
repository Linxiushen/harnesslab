from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from harnesslab.adapters import DeepSeekHarnessProbe
from harnesslab.agentic.artifact import (
    AgentRunArtifact,
    build_agent_artifact,
    replay_agent_artifact,
)
from harnesslab.agentic.contracts import AgentRunRequest
from harnesslab.agentic.scenarios import SCENARIO_NAMES
from harnesslab.agentic.service import AgenticService
from harnesslab.api import build_runtime
from harnesslab.conformance import run_conformance
from harnesslab.trace_contract import (
    TraceArtifact,
    build_trace_artifact,
    compare_trace_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnesslab",
        description="Run, inspect, and test agent harness adapters.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Start the trace console and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4318)
    serve.add_argument("--reload", action="store_true")

    run = commands.add_parser("run", help="Run one task and print its trace")
    run.add_argument("task")
    run.add_argument("--adapter", default="demo")
    run.add_argument("--max-turns", type=int, default=6)

    check = commands.add_parser("check", help="Run the harness conformance matrix")
    check.add_argument("--adapter", default="demo")

    snapshot = commands.add_parser(
        "snapshot",
        help="Record a portable Trace Contract baseline",
    )
    snapshot.add_argument("task")
    snapshot.add_argument("--adapter", default="demo")
    snapshot.add_argument("--max-turns", type=int, default=6)
    snapshot.add_argument("--output", "-o", default="harnesslab.trace.json")

    verify = commands.add_parser(
        "verify",
        help="Run a baseline task and fail on structural trace regressions",
    )
    verify.add_argument("baseline")
    verify.add_argument("--adapter")
    verify.add_argument("--max-turns", type=int, default=6)
    verify.add_argument("--strict-content", action="store_true")

    compare = commands.add_parser(
        "compare",
        help="Compare two saved Trace Contract artifacts",
    )
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--strict-content", action="store_true")

    probe = commands.add_parser(
        "probe",
        help="Probe a future harness capability document without assuming its protocol",
    )
    probe.add_argument("base_url")
    probe.add_argument("--token")

    agent_run = commands.add_parser(
        "agent-run",
        help="Run a deterministic production Agentic recovery scenario",
    )
    agent_run.add_argument(
        "--scenario",
        choices=SCENARIO_NAMES,
        default="enterprise-recovery",
    )
    agent_run.add_argument(
        "--artifact",
        metavar="PATH",
        help="Write a portable Agent Run artifact after execution",
    )

    agent_replay = commands.add_parser(
        "agent-replay",
        help="Verify and replay Agent Run artifact invariants offline",
    )
    agent_replay.add_argument("artifact")
    agent_run.add_argument(
        "--task",
        default=(
            "Investigate the production checkout authorization regression, compare evidence, "
            "apply a safe fix, verify it, and deploy with rollback protection."
        ),
    )

    commands.add_parser(
        "agent-eval",
        help="Run retry, replan, rollback, injection, budget, and DAG evals",
    )
    return parser


async def run_task(task: str, adapter: str, max_turns: int) -> None:
    runtime = build_runtime()
    record = await runtime.run(task, adapter_name=adapter, max_turns=max_turns)
    print(record.model_dump_json(indent=2))


async def run_check(adapter: str) -> None:
    runtime = build_runtime()
    report = await run_conformance(runtime, adapter)
    print(report.model_dump_json(indent=2))
    if report.passed != report.total:
        raise SystemExit(1)


async def run_probe(base_url: str, token: str | None) -> None:
    document = await DeepSeekHarnessProbe(base_url, token).discover()
    print(document.model_dump_json(indent=2))


async def run_agent_scenario(task: str, scenario: str, artifact_path: str | None) -> None:
    service = AgenticService()
    try:
        report = await service.execute(AgentRunRequest(task=task, scenario=scenario))
        print(report.model_dump_json(indent=2))
        if artifact_path:
            artifact = build_agent_artifact(report)
            Path(artifact_path).write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        if report.status not in {"completed"}:
            raise SystemExit(1)
    finally:
        await service.close()


async def run_agent_evals() -> None:
    service = AgenticService()
    try:
        report = await service.evaluate()
        print(report.model_dump_json(indent=2))
        if report.passed != report.total:
            raise SystemExit(1)
    finally:
        await service.close()


async def snapshot_task(
    task: str,
    adapter: str,
    max_turns: int,
    output: str,
) -> None:
    runtime = build_runtime()
    record = await runtime.run(task, adapter_name=adapter, max_turns=max_turns)
    artifact = build_trace_artifact(record)
    if artifact.projection.violations:
        print(artifact.model_dump_json(indent=2))
        raise SystemExit(1)
    destination = Path(output)
    destination.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"Recorded {artifact.contract_version} baseline to {destination} "
        f"({artifact.protocol_fingerprint})"
    )


async def verify_snapshot(
    baseline_path: str,
    adapter: str | None,
    max_turns: int,
    strict_content: bool,
) -> None:
    baseline = load_artifact(baseline_path)
    runtime = build_runtime()
    record = await runtime.run(
        baseline.task,
        adapter_name=adapter or baseline.adapter,
        max_turns=max_turns,
    )
    comparison = compare_trace_artifacts(baseline, build_trace_artifact(record))
    print(comparison.model_dump_json(indent=2))
    if not comparison.compatible or (strict_content and not comparison.content_match):
        raise SystemExit(1)


def compare_snapshots(baseline_path: str, candidate_path: str, strict_content: bool) -> None:
    comparison = compare_trace_artifacts(
        load_artifact(baseline_path),
        load_artifact(candidate_path),
    )
    print(comparison.model_dump_json(indent=2))
    if not comparison.compatible or (strict_content and not comparison.content_match):
        raise SystemExit(1)


def load_artifact(path: str) -> TraceArtifact:
    return TraceArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))


def replay_agent_run(path: str) -> None:
    artifact = AgentRunArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))
    replay = replay_agent_artifact(artifact)
    print(replay.model_dump_json(indent=2))
    if not replay.valid:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        uvicorn.run("harnesslab.api:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "run":
        asyncio.run(run_task(args.task, args.adapter, args.max_turns))
    elif args.command == "check":
        asyncio.run(run_check(args.adapter))
    elif args.command == "snapshot":
        asyncio.run(snapshot_task(args.task, args.adapter, args.max_turns, args.output))
    elif args.command == "verify":
        asyncio.run(
            verify_snapshot(
                args.baseline,
                args.adapter,
                args.max_turns,
                args.strict_content,
            )
        )
    elif args.command == "compare":
        compare_snapshots(args.baseline, args.candidate, args.strict_content)
    elif args.command == "probe":
        asyncio.run(run_probe(args.base_url, args.token))
    elif args.command == "agent-run":
        asyncio.run(run_agent_scenario(args.task, args.scenario, args.artifact))
    elif args.command == "agent-replay":
        replay_agent_run(args.artifact)
    elif args.command == "agent-eval":
        asyncio.run(run_agent_evals())


if __name__ == "__main__":
    main()
