from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from harnesslab.adapters.base import HarnessAdapter
from harnesslab.agentic.schema import expose_tools
from harnesslab.events import EventBus
from harnesslab.models import Message, RunRecord, RunStatus, ToolCall, TraceEvent, utc_now
from harnesslab.tools import ToolRegistry
from harnesslab.trace_contract import redact_data


class HarnessRuntime:
    """Protocol-neutral agent loop with ordered, inspectable execution events."""

    def __init__(self, tools: ToolRegistry, event_bus: EventBus | None = None) -> None:
        self.tools = tools
        self.events = event_bus or EventBus()
        self.adapters: dict[str, HarnessAdapter] = {}
        self.runs: dict[str, RunRecord] = {}

    def register_adapter(self, adapter: HarnessAdapter) -> None:
        if adapter.name in self.adapters:
            raise ValueError(f"Adapter already registered: {adapter.name}")
        self.adapters[adapter.name] = adapter

    def list_runs(self) -> list[RunRecord]:
        return sorted(self.runs.values(), key=lambda run: run.started_at, reverse=True)

    def get_run(self, run_id: str) -> RunRecord:
        try:
            run = self.runs[run_id]
        except KeyError as exc:
            raise LookupError(run_id) from exc
        run.events = self.events.history(run_id)
        return run

    async def _emit(
        self,
        run: RunRecord,
        event_type: str,
        payload: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> TraceEvent:
        event = await self.events.publish(
            run.id,
            event_type,
            redact_data(payload or {}),
            duration_ms,
        )
        run.events.append(event)
        return event

    async def run(
        self,
        task: str,
        *,
        adapter_name: str = "demo",
        max_turns: int = 6,
        run_id: str | None = None,
        max_exposed_tools: int = 12,
        max_tool_schema_tokens: int = 8_000,
    ) -> RunRecord:
        if adapter_name not in self.adapters:
            raise ValueError(f"Unknown adapter: {adapter_name}")
        run = RunRecord(
            id=run_id or f"run_{uuid4().hex[:12]}",
            task=task,
            adapter=adapter_name,
            status=RunStatus.RUNNING,
            messages=[
                Message(
                    role="system",
                    content=(
                        "You are an evidence-first engineering agent. Use tools before making "
                        "claims, keep tool arguments minimal, and report file-level evidence."
                    ),
                ),
                Message(role="user", content=task),
            ],
            metadata={"max_turns": max_turns, "message_count": 2},
        )
        self.runs[run.id] = run
        exposure = expose_tools(
            self.tools.specs(),
            query=task,
            max_tools=max_exposed_tools,
            max_tokens=max_tool_schema_tokens,
        )
        run.metadata["tool_exposure"] = {
            "max_tools": max_exposed_tools,
            "max_schema_tokens": max_tool_schema_tokens,
            "exposed": [tool.name for tool in exposure.tools],
            "dropped": exposure.dropped_tools,
            "estimated_tokens": exposure.estimated_tokens,
        }
        await self._emit(
            run,
            "run.started",
            {"adapter": adapter_name, "task": task, "max_turns": max_turns},
        )

        adapter = self.adapters[adapter_name]
        try:
            for turn_index in range(1, max_turns + 1):
                await self._emit(
                    run,
                    "model.requested",
                    {
                        "turn": turn_index,
                        "message_count": len(run.messages),
                        "tool_count": len(exposure.tools),
                        "dropped_tool_count": len(exposure.dropped_tools),
                        "tool_schema_tokens": exposure.estimated_tokens,
                    },
                )
                started = time.perf_counter()
                turn = await adapter.complete(list(run.messages), exposure.tools)
                model_ms = round((time.perf_counter() - started) * 1000, 2)
                await self._emit(
                    run,
                    "model.completed",
                    {
                        "turn": turn_index,
                        "finish_reason": turn.finish_reason,
                        "tool_calls": len(turn.tool_calls),
                        "usage": turn.usage,
                        "text": turn.text,
                    },
                    model_ms,
                )
                run.messages.append(
                    Message(
                        role="assistant",
                        content=turn.text,
                        tool_calls=(
                            [self._openai_tool_call(call) for call in turn.tool_calls] or None
                        ),
                    )
                )
                run.metadata["message_count"] = len(run.messages)

                if not turn.tool_calls:
                    run.answer = turn.text
                    run.status = RunStatus.COMPLETED
                    run.completed_at = utc_now()
                    await self._emit(
                        run,
                        "run.completed",
                        {"turns": turn_index, "answer": turn.text},
                    )
                    return run

                for call in turn.tool_calls:
                    await self._execute_tool(run, call, turn_index)

            raise RuntimeError(f"Maximum turn limit reached ({max_turns})")
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = redact_data(str(exc))
            run.completed_at = utc_now()
            await self._emit(run, "run.failed", {"error": str(exc)})
            return run

    async def _execute_tool(self, run: RunRecord, call: ToolCall, turn_index: int) -> None:
        spec = self.tools.get(call.name)
        await self._emit(
            run,
            "tool.requested",
            {
                "turn": turn_index,
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "source": spec.source,
            },
        )
        if not spec.read_only:
            await self._emit(
                run,
                "tool.denied",
                {
                    "call_id": call.id,
                    "name": call.name,
                    "policy": "fail-closed",
                    "reason": "explicit approval provider required",
                },
            )
            raise PermissionError(
                f"Tool '{call.name}' requires an explicit approval provider; fail-closed by default"
            )
        await self._emit(
            run,
            "tool.approved",
            {"call_id": call.id, "name": call.name, "policy": "read-only-auto"},
        )
        started = time.perf_counter()
        result = await self.tools.call(call.name, call.arguments)
        tool_ms = round((time.perf_counter() - started) * 1000, 2)
        await self._emit(
            run,
            "tool.completed",
            {"call_id": call.id, "name": call.name, "result": result},
            tool_ms,
        )
        run.messages.append(
            Message(
                role="tool",
                name=call.name,
                tool_call_id=call.id,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            )
        )
        run.metadata["message_count"] = len(run.messages)

    @staticmethod
    def _openai_tool_call(call: ToolCall) -> dict[str, Any]:
        return {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, separators=(",", ":")),
            },
        }
