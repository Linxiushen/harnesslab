from __future__ import annotations

import inspect
from enum import StrEnum
from typing import Any, Protocol


class ExtensionState(StrEnum):
    REGISTERED = "registered"
    ACTIVE = "active"
    STOPPED = "stopped"
    FAILED = "failed"


class HarnessExtension(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def on_event(self, event: dict[str, Any]) -> None: ...


class ExtensionError(RuntimeError):
    pass


class ExtensionRecord:
    def __init__(self, name: str, extension: HarnessExtension) -> None:
        self.name = name
        self.extension = extension
        self.state = ExtensionState.REGISTERED
        self.error: str | None = None


class ExtensionManager:
    """Deterministic extension lifecycle with failure isolation and reverse teardown."""

    def __init__(self) -> None:
        self._records: dict[str, ExtensionRecord] = {}
        self._activation_order: list[str] = []

    def register(self, name: str, extension: HarnessExtension) -> ExtensionRecord:
        if not name or name in self._records:
            raise ValueError(f"extension name is unavailable: {name!r}")
        record = ExtensionRecord(name, extension)
        self._records[name] = record
        return record

    def records(self) -> list[ExtensionRecord]:
        return list(self._records.values())

    async def start(self, name: str) -> ExtensionRecord:
        record = self._records[name]
        if record.state == ExtensionState.ACTIVE:
            return record
        try:
            result = record.extension.start()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # lifecycle isolation is an explicit boundary
            record.state = ExtensionState.FAILED
            record.error = str(exc)
            raise ExtensionError(f"extension {name!r} failed to start") from exc
        record.state = ExtensionState.ACTIVE
        record.error = None
        if name not in self._activation_order:
            self._activation_order.append(name)
        return record

    async def stop(self, name: str) -> ExtensionRecord:
        record = self._records[name]
        if record.state not in {ExtensionState.ACTIVE, ExtensionState.FAILED}:
            record.state = ExtensionState.STOPPED
            return record
        try:
            result = record.extension.stop()
            if inspect.isawaitable(result):
                await result
        finally:
            record.state = ExtensionState.STOPPED
            if name in self._activation_order:
                self._activation_order.remove(name)
        return record

    async def start_all(self) -> list[ExtensionRecord]:
        active: list[ExtensionRecord] = []
        for name in self._records:
            active.append(await self.start(name))
        return active

    async def stop_all(self) -> list[ExtensionRecord]:
        stopped: list[ExtensionRecord] = []
        for name in reversed(self._activation_order[:]):
            stopped.append(await self.stop(name))
        for record in self._records.values():
            if record.state == ExtensionState.REGISTERED:
                record.state = ExtensionState.STOPPED
        return stopped

    async def dispatch(self, event: dict[str, Any]) -> list[str]:
        delivered: list[str] = []
        for name in self._activation_order[:]:
            record = self._records[name]
            if record.state != ExtensionState.ACTIVE:
                continue
            try:
                result = record.extension.on_event(event)
                if inspect.isawaitable(result):
                    await result
                delivered.append(name)
            except Exception as exc:  # one extension must not stop the harness
                record.state = ExtensionState.FAILED
                record.error = str(exc)
        return delivered
