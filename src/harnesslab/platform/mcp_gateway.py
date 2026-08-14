from __future__ import annotations

import asyncio
import inspect
import time
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from harnesslab.agentic.schema import ToolExposure, expose_tools
from harnesslab.models import ToolSpec


class GatewayServerState(StrEnum):
    REGISTERED = "registered"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


class MCPGatewayError(RuntimeError):
    pass


class MCPGatewayUnavailable(MCPGatewayError):
    pass


class MCPGatewayCircuitOpen(MCPGatewayUnavailable):
    pass


class MCPGatewayConfig(BaseModel):
    server_id: str = Field(min_length=1, max_length=100)
    namespace: str = Field(min_length=1, max_length=100)
    tags: frozenset[str] = Field(default_factory=frozenset)
    max_concurrency: int = Field(default=4, ge=1, le=256)
    failure_threshold: int = Field(default=3, ge=1, le=100)
    reset_timeout_seconds: float = Field(default=30, gt=0, le=86_400)
    discovery_timeout_seconds: float = Field(default=10, gt=0, le=300)


class MCPGatewayProvider(Protocol):
    async def start(self) -> Any: ...

    async def discover(self) -> list[ToolSpec]: ...

    async def call(self, remote_name: str, arguments: dict[str, Any]) -> Any: ...

    async def close(self) -> None: ...


class GatewayServerSnapshot(BaseModel):
    server_id: str
    namespace: str
    state: GatewayServerState
    tool_count: int = 0
    failure_count: int = 0
    active_calls: int = 0
    last_error: str | None = None
    last_discovered_at: float | None = None


class GatewayToolBinding(BaseModel):
    name: str
    remote_name: str
    server_id: str
    spec: ToolSpec


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_timeout_seconds: float,
        clock=time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self._clock() - self.opened_at >= self.reset_timeout_seconds:
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def allow(self) -> bool:
        return not self.is_open

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self._clock()


class _Server:
    def __init__(self, config: MCPGatewayConfig, provider: MCPGatewayProvider) -> None:
        self.config = config
        self.provider = provider
        self.state = GatewayServerState.REGISTERED
        self.tools: dict[str, GatewayToolBinding] = {}
        self.failure_count = 0
        self.active_calls = 0
        self.last_error: str | None = None
        self.last_discovered_at: float | None = None
        self.breaker = CircuitBreaker(
            failure_threshold=config.failure_threshold,
            reset_timeout_seconds=config.reset_timeout_seconds,
        )
        self.semaphore = asyncio.Semaphore(config.max_concurrency)


class MCPGateway:
    """Server-level MCP governance with bounded discovery and call concurrency."""

    def __init__(
        self,
        *,
        max_servers: int = 128,
        max_tools_per_server: int = 512,
    ) -> None:
        if max_servers < 1 or max_tools_per_server < 1:
            raise ValueError("gateway limits must be positive")
        self.max_servers = max_servers
        self.max_tools_per_server = max_tools_per_server
        self._servers: dict[str, _Server] = {}
        self._bindings: dict[str, GatewayToolBinding] = {}
        self._discovery_lock = asyncio.Lock()

    def register(self, config: MCPGatewayConfig, provider: MCPGatewayProvider) -> None:
        if config.server_id in self._servers:
            raise ValueError(f"MCP server already registered: {config.server_id}")
        if len(self._servers) >= self.max_servers:
            raise RuntimeError("MCP server limit exceeded")
        self._servers[config.server_id] = _Server(config, provider)

    async def discover(self, server_id: str) -> list[GatewayToolBinding]:
        server = self._server(server_id)
        async with self._discovery_lock:
            if server.state == GatewayServerState.STOPPED:
                raise MCPGatewayUnavailable(f"MCP server is stopped: {server_id}")
            server.state = GatewayServerState.STARTING
            try:
                await asyncio.wait_for(
                    self._maybe_await(server.provider.start()),
                    timeout=server.config.discovery_timeout_seconds,
                )
                specs = await asyncio.wait_for(
                    server.provider.discover(),
                    timeout=server.config.discovery_timeout_seconds,
                )
                if len(specs) > self.max_tools_per_server:
                    raise MCPGatewayError(
                        f"server {server_id} exposed {len(specs)} tools, "
                        f"limit is {self.max_tools_per_server}"
                    )
                for binding_name in tuple(server.tools):
                    self._bindings.pop(binding_name, None)
                server.tools = {}
                for spec in specs:
                    local_name = f"{server.config.namespace}__{spec.name}"
                    if local_name in self._bindings:
                        raise MCPGatewayError(f"duplicate gateway tool: {local_name}")
                    binding = GatewayToolBinding(
                        name=local_name,
                        remote_name=spec.name,
                        server_id=server_id,
                        spec=spec.model_copy(update={"name": local_name}),
                    )
                    server.tools[local_name] = binding
                    self._bindings[local_name] = binding
                server.state = GatewayServerState.READY
                server.last_error = None
                server.last_discovered_at = time.time()
                server.breaker.success()
                return list(server.tools.values())
            except Exception as exc:
                server.state = GatewayServerState.FAILED
                server.last_error = f"{type(exc).__name__}: {exc}"
                server.failure_count += 1
                server.breaker.failure()
                raise MCPGatewayError(server.last_error) from exc

    async def discover_all(self) -> dict[str, list[GatewayToolBinding]]:
        results: dict[str, list[GatewayToolBinding]] = {}
        for server_id in self._servers:
            try:
                results[server_id] = await self.discover(server_id)
            except MCPGatewayError:
                results[server_id] = []
        return results

    def expose(
        self,
        *,
        query: str,
        max_tools: int = 12,
        max_schema_tokens: int = 8_000,
        server_ids: set[str] | None = None,
        tags: set[str] | None = None,
    ) -> ToolExposure:
        candidates = []
        for server in self._servers.values():
            if server_ids is not None and server.config.server_id not in server_ids:
                continue
            if tags is not None and not tags.issubset(server.config.tags):
                continue
            if server.state not in {GatewayServerState.READY, GatewayServerState.DEGRADED}:
                continue
            candidates.extend(binding.spec for binding in server.tools.values())
        return expose_tools(
            candidates,
            query=query,
            max_tools=max_tools,
            max_tokens=max_schema_tokens,
        )

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        binding = self._binding(tool_name)
        server = self._server(binding.server_id)
        if not server.breaker.allow():
            raise MCPGatewayCircuitOpen(f"circuit open for MCP server: {server.config.server_id}")
        async with server.semaphore:
            server.active_calls += 1
            try:
                result = await server.provider.call(binding.remote_name, arguments)
            except Exception as exc:
                server.failure_count += 1
                server.last_error = f"{type(exc).__name__}: {exc}"
                server.state = GatewayServerState.DEGRADED
                server.breaker.failure()
                raise MCPGatewayError(
                    f"MCP tool call failed: {tool_name}: {type(exc).__name__}: {exc}"
                ) from exc
            else:
                server.breaker.success()
                if server.state == GatewayServerState.DEGRADED:
                    server.state = GatewayServerState.READY
                return result
            finally:
                server.active_calls -= 1

    def snapshots(self) -> list[GatewayServerSnapshot]:
        return [
            GatewayServerSnapshot(
                server_id=server.config.server_id,
                namespace=server.config.namespace,
                state=server.state,
                tool_count=len(server.tools),
                failure_count=server.failure_count,
                active_calls=server.active_calls,
                last_error=server.last_error,
                last_discovered_at=server.last_discovered_at,
            )
            for server in self._servers.values()
        ]

    def bindings(self) -> list[GatewayToolBinding]:
        return list(self._bindings.values())

    async def close(self) -> None:
        for server in reversed(list(self._servers.values())):
            try:
                await server.provider.close()
            finally:
                server.state = GatewayServerState.STOPPED
        self._bindings.clear()

    def _server(self, server_id: str) -> _Server:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise LookupError(f"MCP server not found: {server_id}") from exc

    def _binding(self, tool_name: str) -> GatewayToolBinding:
        try:
            return self._bindings[tool_name]
        except KeyError as exc:
            raise LookupError(f"MCP gateway tool not found: {tool_name}") from exc

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value
