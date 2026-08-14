from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EvalStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class EvalCase(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=3, max_length=20_000)
    scenario: str = Field(min_length=1, max_length=200)
    expected_status: str = "completed"
    required_events: tuple[str, ...] = ()
    forbidden_events: tuple[str, ...] = ()
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalDataset(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    cases: list[EvalCase] = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def validate_unique_ids(self) -> EvalDataset:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")
        return self

    def labels(self) -> set[str]:
        return {label for case in self.cases for label in case.labels.values()}

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for case in self.cases
        )

    @classmethod
    def from_jsonl(cls, name: str, version: str, content: str) -> EvalDataset:
        cases = [
            EvalCase.model_validate(json.loads(line))
            for line in content.splitlines()
            if line.strip()
        ]
        return cls(name=name, version=version, cases=cases).validate_unique_ids()


class DatasetRegistry:
    def __init__(self, *, max_datasets: int = 256) -> None:
        self.max_datasets = max_datasets
        self._datasets: dict[tuple[str, str], EvalDataset] = {}

    def register(self, dataset: EvalDataset) -> None:
        dataset.validate_unique_ids()
        key = (dataset.name, dataset.version)
        if key in self._datasets:
            raise ValueError(
                f"dataset version already registered: {dataset.name}/{dataset.version}"
            )
        if len(self._datasets) >= self.max_datasets:
            raise RuntimeError("dataset limit exceeded")
        self._datasets[key] = dataset

    def get(self, name: str, version: str) -> EvalDataset:
        try:
            return self._datasets[(name, version)]
        except KeyError as exc:
            raise LookupError(f"dataset not found: {name}/{version}") from exc

    def list(self) -> list[EvalDataset]:
        return list(self._datasets.values())


class EvalCaseResult(BaseModel):
    case_id: str
    status: EvalStatus
    passed: bool
    duration_ms: float
    observed_status: str | None = None
    observed_events: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)


class EvalRunReport(BaseModel):
    dataset: str
    version: str
    started_at: float
    completed_at: float
    results: list[EvalCaseResult]
    passed: int
    failed: int
    errored: int

    @property
    def total(self) -> int:
        return len(self.results)


EvalExecutor = Callable[[EvalCase], Awaitable[Any] | Any]


class EvaluationRunner:
    """Bounded concurrent dataset runner with explicit event/status assertions."""

    def __init__(self, executor: EvalExecutor, *, max_concurrency: int = 8) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.executor = executor
        self.max_concurrency = max_concurrency

    async def run(
        self,
        dataset: EvalDataset,
        *,
        case_ids: set[str] | None = None,
        labels: set[str] | None = None,
    ) -> EvalRunReport:
        selected = [
            case
            for case in dataset.cases
            if (case_ids is None or case.id in case_ids)
            and (labels is None or labels.issubset(case.labels.values()))
        ]
        if not selected:
            raise ValueError("evaluation selection is empty")
        semaphore = asyncio.Semaphore(self.max_concurrency)
        started_at = time.time()

        async def execute(case: EvalCase) -> EvalCaseResult:
            async with semaphore:
                began = time.perf_counter()
                try:
                    output = self.executor(case)
                    output = await output if hasattr(output, "__await__") else output
                    result = _evaluate_output(case, output)
                    result.duration_ms = round((time.perf_counter() - began) * 1000, 2)
                    return result
                except Exception as exc:
                    return EvalCaseResult(
                        case_id=case.id,
                        status=EvalStatus.ERROR,
                        passed=False,
                        duration_ms=round((time.perf_counter() - began) * 1000, 2),
                        failures=[f"{type(exc).__name__}: {exc}"],
                    )

        results = await asyncio.gather(*(execute(case) for case in selected))
        completed_at = time.time()
        return EvalRunReport(
            dataset=dataset.name,
            version=dataset.version,
            started_at=started_at,
            completed_at=completed_at,
            results=results,
            passed=sum(result.passed for result in results),
            failed=sum(
                not result.passed and result.status == EvalStatus.FAILED
                for result in results
            ),
            errored=sum(result.status == EvalStatus.ERROR for result in results),
        )


def _evaluate_output(case: EvalCase, output: Any) -> EvalCaseResult:
    if isinstance(output, BaseModel):
        payload = output.model_dump(mode="json")
    elif isinstance(output, dict):
        payload = dict(output)
    else:
        payload = {"value": output}
    observed_status = str(payload.get("status", "")) or None
    events = payload.get("events", [])
    observed_events = [
        str(event.get("type", event)) if isinstance(event, dict) else str(event)
        for event in events
    ]
    failures: list[str] = []
    if observed_status != case.expected_status:
        failures.append(f"status={observed_status!r}, expected={case.expected_status!r}")
    missing = sorted(set(case.required_events) - set(observed_events))
    if missing:
        failures.append(f"missing events: {missing}")
    forbidden = sorted(set(case.forbidden_events) & set(observed_events))
    if forbidden:
        failures.append(f"forbidden events: {forbidden}")
    return EvalCaseResult(
        case_id=case.id,
        status=EvalStatus.PASSED if not failures else EvalStatus.FAILED,
        passed=not failures,
        duration_ms=0,
        observed_status=observed_status,
        observed_events=observed_events,
        failures=failures,
        output=payload,
    )
