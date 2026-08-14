from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Protocol

from harnesslab.agentic.contracts import AgentRunReport, AgentRunStatus, StepStatus
from harnesslab.models import TraceEvent


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different request payload."""


class DuplicateRun(ValueError):
    """A caller-provided run ID already exists."""


class AgentRunStore(Protocol):
    def reserve(
        self,
        report: AgentRunReport,
        *,
        request_hash: str,
        idempotency_key: str | None,
    ) -> tuple[AgentRunReport, bool]: ...

    def save(self, report: AgentRunReport) -> None: ...

    def get(self, run_id: str) -> AgentRunReport: ...

    def list(self) -> list[AgentRunReport]: ...

    def append_event(self, event: TraceEvent) -> None: ...

    def events(self, run_id: str, *, after: int = 0) -> list[TraceEvent]: ...

    def recover_incomplete(self) -> list[AgentRunReport]: ...

    def close(self) -> None: ...


class SQLiteAgentRunStore:
    """Small durable store with atomic idempotency and ordered event persistence."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    report_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at
                    ON agent_runs(created_at DESC);
                """
            )

    def reserve(
        self,
        report: AgentRunReport,
        *,
        request_hash: str,
        idempotency_key: str | None,
    ) -> tuple[AgentRunReport, bool]:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = self._connection.execute(
                        """
                        SELECT report_json, request_hash
                        FROM agent_runs WHERE idempotency_key = ?
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    if existing:
                        if existing["request_hash"] != request_hash:
                            raise IdempotencyConflict(
                                "Idempotency-Key was already used with a different request"
                            )
                        self._connection.commit()
                        return AgentRunReport.model_validate_json(
                            existing["report_json"]
                        ), False

                if self._connection.execute(
                    "SELECT 1 FROM agent_runs WHERE run_id = ?", (report.id,)
                ).fetchone():
                    raise DuplicateRun(f"run ID already exists: {report.id}")

                self._connection.execute(
                    """
                    INSERT INTO agent_runs(
                        run_id, report_json, request_hash, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.id,
                        report.model_dump_json(),
                        request_hash,
                        idempotency_key,
                        report.started_at.isoformat(),
                        now,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return report, True

    def save(self, report: AgentRunReport) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_runs
                SET report_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (report.model_dump_json(), datetime.now(UTC).isoformat(), report.id),
            )
            if cursor.rowcount != 1:
                raise LookupError(report.id)

    def get(self, run_id: str) -> AgentRunReport:
        with self._lock:
            row = self._connection.execute(
                "SELECT report_json FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise LookupError(run_id)
        report = AgentRunReport.model_validate_json(row["report_json"])
        report.events = self.events(run_id)
        return report

    def list(self) -> list[AgentRunReport]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT report_json FROM agent_runs ORDER BY created_at DESC"
            ).fetchall()
        reports = [AgentRunReport.model_validate_json(row["report_json"]) for row in rows]
        for report in reports:
            report.events = self.events(report.id)
        return reports

    def append_event(self, event: TraceEvent) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_events(run_id, sequence, event_json)
                VALUES (?, ?, ?)
                """,
                (event.run_id, event.sequence, event.model_dump_json()),
            )

    def events(self, run_id: str, *, after: int = 0) -> list[TraceEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_json FROM agent_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after),
            ).fetchall()
        return [TraceEvent.model_validate_json(row["event_json"]) for row in rows]

    def recover_incomplete(self) -> list[AgentRunReport]:
        recovered: list[AgentRunReport] = []
        for report in self.list():
            if report.status not in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
                continue
            report.completed_at = datetime.now(UTC)
            terminal = [event for event in report.events if event.type in _TERMINAL_EVENTS]
            if terminal:
                self._replay_terminal_checkpoint(report, terminal[-1])
            else:
                for step in report.steps:
                    if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                        step.status = StepStatus.CANCELLED
                        step.error = "worker interrupted before a terminal checkpoint"
                report.status = AgentRunStatus.FAILED
                report.error = "worker interrupted by process restart"
                event = TraceEvent(
                    run_id=report.id,
                    sequence=len(report.events) + 1,
                    type="agent.run.failed",
                    payload={"reason": "worker_interrupted", "recovered": True},
                )
                self.append_event(event)
                report.events.append(event)
            self.save(report)
            recovered.append(report)
        return recovered

    @staticmethod
    def _replay_terminal_checkpoint(
        report: AgentRunReport,
        terminal: TraceEvent,
    ) -> None:
        steps = {step.step_id: step for step in report.steps}
        for event in report.events:
            step = steps.get(str(event.payload.get("step_id")))
            if step is None:
                continue
            if event.type == "agent.step.started":
                step.status = StepStatus.RUNNING
                step.started_at = event.at
            elif event.type == "tool.attempt.completed":
                step.output = event.payload.get("result")
            elif event.type == "agent.step.completed":
                step.status = StepStatus.SUCCEEDED
                step.completed_at = event.at
            elif event.type == "agent.step.failed":
                step.status = StepStatus.FAILED
                step.error = event.payload.get("error")
                step.completed_at = event.at
            elif event.type == "agent.step.blocked":
                step.status = StepStatus.BLOCKED
                step.error = f"blocked by dependencies: {event.payload.get('dependencies', [])}"
                step.completed_at = event.at
            elif event.type == "agent.step.cancelled":
                step.status = StepStatus.CANCELLED
                step.error = "run cancellation requested"
                step.completed_at = event.at
            elif event.type == "recovery.compensation.completed":
                step.status = StepStatus.COMPENSATED
                step.completed_at = event.at

        fallback_status = (
            StepStatus.CANCELLED
            if terminal.type == "agent.run.cancelled"
            else StepStatus.BLOCKED
        )
        for step in report.steps:
            if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                step.status = fallback_status
                step.error = "terminal event persisted before report checkpoint"
        report.status = _STATUS_BY_TERMINAL[terminal.type]
        report.answer = str(terminal.payload.get("answer") or "")
        report.error = terminal.payload.get("error")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


_TERMINAL_EVENTS: Sequence[str] = (
    "agent.run.completed",
    "agent.run.failed",
    "agent.run.cancelled",
)
_STATUS_BY_TERMINAL = {
    "agent.run.completed": AgentRunStatus.COMPLETED,
    "agent.run.failed": AgentRunStatus.FAILED,
    "agent.run.cancelled": AgentRunStatus.CANCELLED,
}
