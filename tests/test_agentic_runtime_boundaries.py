from __future__ import annotations

import pytest

from harnesslab.agentic.extensions import ExtensionManager, ExtensionState
from harnesslab.agentic.schema import expose_tools, normalize_tool_schema
from harnesslab.agentic.session_bus import SessionEventBus
from harnesslab.models import AdapterTurn, ToolSpec
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import ToolRegistry


class RecordingExtension:
    def __init__(self, name: str, events: list[str], *, fail_event: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail_event = fail_event

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")

    async def on_event(self, event: dict[str, object]) -> None:
        self.events.append(f"event:{self.name}:{event['type']}")
        if self.fail_event:
            raise RuntimeError("extension callback failed")


@pytest.mark.asyncio
async def test_extension_manager_stops_in_reverse_and_isolates_callback_failure() -> None:
    events: list[str] = []
    manager = ExtensionManager()
    manager.register("first", RecordingExtension("first", events))
    manager.register("second", RecordingExtension("second", events, fail_event=True))

    await manager.start_all()
    delivered = await manager.dispatch({"type": "run.completed"})
    await manager.stop_all()

    assert delivered == ["first"]
    assert events == [
        "start:first",
        "start:second",
        "event:first:run.completed",
        "event:second:run.completed",
        "stop:second",
        "stop:first",
    ]
    assert manager.records()[1].state == ExtensionState.STOPPED
    assert manager.records()[1].error == "extension callback failed"

    delivered_again = await manager.dispatch({"type": "run.completed"})
    assert delivered_again == []
    assert events.count("event:second:run.completed") == 1


def test_schema_normalization_removes_provider_metadata_and_bounds_descriptions() -> None:
    result = normalize_tool_schema(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "A noisy tool",
            "type": "object",
            "properties": {"q": {"type": "string", "description": "x" * 1000}},
        },
        max_description_chars=20,
    )
    assert "$schema" not in result.normalized_schema
    assert "title" not in result.normalized_schema
    assert len(result.normalized_schema["properties"]["q"]["description"]) == 20
    assert result.estimated_tokens > 0
    assert result.removed_keys


def test_tool_exposure_is_bounded_and_returns_dropped_names() -> None:
    tools = [
        ToolSpec(
            name=f"tool_{index}",
            description=("repository search " if index == 0 else "generic ") + "x" * 200,
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )
        for index in range(8)
    ]
    exposure = expose_tools(tools, query="repository search", max_tools=3, max_tokens=220)
    assert len(exposure.tools) <= 3
    assert exposure.estimated_tokens <= 220
    assert "tool_0" in [tool.name for tool in exposure.tools]
    assert exposure.dropped_tools


@pytest.mark.asyncio
async def test_session_bus_replays_history_and_keeps_sessions_isolated() -> None:
    bus = SessionEventBus(history_limit=3, subscriber_queue_limit=2)
    await bus.publish("session-a", "started")
    await bus.publish("session-a", "tool.started", {"name": "search"})
    subscription = bus.subscribe("session-a", after_sequence=0)
    live = await bus.publish("session-a", "completed")
    other = await bus.publish("session-b", "started")

    first = await subscription.get()
    second = await subscription.get()
    assert [first.event_type, second.event_type] == ["tool.started", "completed"]
    assert live.sequence < other.sequence
    assert bus.history("session-b")[0].session_id == "session-b"
    subscription.close()


@pytest.mark.asyncio
async def test_runtime_exposes_only_a_bounded_tool_view() -> None:
    registry = ToolRegistry()
    for index in range(6):
        registry.register(
            ToolSpec(
                name=f"tool_{index}",
                description="repository search" if index == 0 else "unrelated tool",
                input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
            ),
            lambda arguments: arguments,
        )

    class CaptureAdapter:
        name = "capture"

        async def complete(self, messages, tools):
            self.tools = list(tools)
            return AdapterTurn(text="done")

    adapter = CaptureAdapter()
    runtime = HarnessRuntime(registry)
    runtime.register_adapter(adapter)
    run = await runtime.run(
        "repository search",
        adapter_name="capture",
        max_exposed_tools=2,
        max_tool_schema_tokens=150,
    )

    assert run.status == "completed"
    assert len(adapter.tools) <= 2
    assert run.metadata["tool_exposure"]["dropped"]
