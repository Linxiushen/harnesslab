from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harnesslab.agentic.schema import normalize_tool_schema
from harnesslab.models import ToolSpec
from harnesslab.tools import ToolRegistry


class MCPStdioProvider:
    """Optional MCP stdio bridge backed by the official Python SDK."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: Mapping[str, str] | None = None,
        namespace: str = "mcp",
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = dict(env or {})
        self.namespace = namespace
        self._remote_names: dict[str, str] = {}
        self._stdio_context = None
        self._session_context = None
        self._session = None

    async def start(self):
        """Start one MCP process and reuse it for discovery and calls."""
        if self._session is not None:
            return self._session
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError(
                "Install HarnessLab with the 'mcp' extra: pip install harnesslab[mcp]"
            ) from exc
        self._stdio_context = stdio_client(self._params())
        read, write = await self._stdio_context.__aenter__()
        self._session_context = ClientSession(read, write)
        self._session = await self._session_context.__aenter__()
        try:
            await self._session.initialize()
        except Exception:
            await self.close()
            raise
        return self._session

    async def close(self) -> None:
        """Close the MCP session and process, tolerating partial startup."""
        session_context, stdio_context = self._session_context, self._stdio_context
        self._session = None
        self._session_context = None
        self._stdio_context = None
        if session_context is not None:
            await session_context.__aexit__(None, None, None)
        if stdio_context is not None:
            await stdio_context.__aexit__(None, None, None)

    async def attach(self, registry: ToolRegistry) -> list[ToolSpec]:
        specs = await self.discover()
        for spec in specs:
            registry.register(spec, self._handler(spec.name))
        return specs

    async def discover(self) -> list[ToolSpec]:
        tools = (await (await self.start()).list_tools()).tools
        specs = []
        for tool in tools:
            local_name = f"{self.namespace}__{tool.name}"
            spec = ToolSpec(
                name=local_name,
                description=tool.description or f"MCP tool {tool.name}",
                input_schema=normalize_tool_schema(tool.inputSchema).normalized_schema,
                read_only=False,
                source=f"mcp:{self.namespace}",
            )
            self._remote_names[local_name] = tool.name
            specs.append(spec)
        return specs

    async def call(self, remote_name: str, arguments: dict[str, Any]) -> Any:
        return await self._call_tool(remote_name, arguments)

    def _handler(self, local_name: str):
        async def call(arguments: dict[str, Any]) -> Any:
            return await self._call_tool(self._remote_names[local_name], arguments)

        return call

    def _params(self):
        try:
            from mcp import StdioServerParameters
        except ImportError as exc:
            raise RuntimeError(
                "Install HarnessLab with the 'mcp' extra: pip install harnesslab[mcp]"
            ) from exc
        return StdioServerParameters(command=self.command, args=self.args, env=self.env or None)

    async def _list_tools(self):
        return (await (await self.start()).list_tools()).tools

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await (await self.start()).call_tool(name, arguments)
        return [item.model_dump(mode="json") for item in result.content]
