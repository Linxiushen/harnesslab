from __future__ import annotations

import asyncio

import pytest

from harnesslab.models import ToolSpec
from harnesslab.platform.mcp_gateway import (
    GatewayServerState,
    MCPGateway,
    MCPGatewayCircuitOpen,
    MCPGatewayConfig,
)


class FakeProvider:
    def __init__(self, *, fail_calls: int = 0) -> None:
        self.started = 0
        self.closed = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_calls = fail_calls

    async def start(self) -> None:
        self.started += 1

    async def discover(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search",
                description="Search repository evidence",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
            ToolSpec(
                name="write",
                description="Write a deployment change",
                input_schema={"type": "object", "properties": {"patch": {"type": "string"}}},
                read_only=False,
            ),
        ]

    async def call(self, remote_name: str, arguments: dict[str, object]) -> object:
        self.calls.append((remote_name, arguments))
        if self.fail_calls:
            self.fail_calls -= 1
            raise RuntimeError("upstream unavailable")
        return {"tool": remote_name, "arguments": arguments}

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_gateway_discovers_namespaced_tools_and_bounds_exposure() -> None:
    provider = FakeProvider()
    gateway = MCPGateway()
    gateway.register(
        MCPGatewayConfig(server_id="repo", namespace="repo", tags=frozenset({"code"})),
        provider,
    )

    discovered = await gateway.discover("repo")
    exposure = gateway.expose(query="search repository", max_tools=1, max_schema_tokens=500)

    assert provider.started == 1
    assert {binding.name for binding in discovered} == {"repo__search", "repo__write"}
    assert [tool.name for tool in exposure.tools] == ["repo__search"]
    assert gateway.snapshots()[0].state == GatewayServerState.READY


@pytest.mark.asyncio
async def test_gateway_routes_calls_limits_concurrency_and_closes_provider() -> None:
    provider = FakeProvider()
    gateway = MCPGateway()
    gateway.register(
        MCPGatewayConfig(server_id="repo", namespace="repo", max_concurrency=1),
        provider,
    )
    await gateway.discover("repo")
    results = await asyncio.gather(
        gateway.call("repo__search", {"query": "one"}),
        gateway.call("repo__search", {"query": "two"}),
    )

    assert len(results) == 2
    assert len(provider.calls) == 2
    await gateway.close()
    assert provider.closed == 1
    assert gateway.snapshots()[0].state == GatewayServerState.STOPPED


@pytest.mark.asyncio
async def test_gateway_opens_circuit_after_repeated_upstream_failures() -> None:
    provider = FakeProvider(fail_calls=3)
    gateway = MCPGateway()
    gateway.register(
        MCPGatewayConfig(server_id="repo", namespace="repo", failure_threshold=2),
        provider,
    )
    await gateway.discover("repo")

    with pytest.raises(Exception, match="upstream unavailable"):
        await gateway.call("repo__search", {})
    with pytest.raises(Exception, match="upstream unavailable"):
        await gateway.call("repo__search", {})
    with pytest.raises(MCPGatewayCircuitOpen):
        await gateway.call("repo__search", {})
    assert gateway.snapshots()[0].state == GatewayServerState.DEGRADED
