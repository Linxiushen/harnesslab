from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.agentic.budget import estimate_tokens
from harnesslab.models import ToolSpec


class ToolSchemaBudgetExceeded(ValueError):
    """Raised when normalized schemas cannot fit the exposure budget."""


class NormalizedToolSchema(BaseModel):
    normalized_schema: dict[str, Any]
    removed_keys: list[str] = Field(default_factory=list)
    estimated_tokens: int


class ToolExposure(BaseModel):
    tools: list[ToolSpec]
    estimated_tokens: int
    dropped_tools: list[str] = Field(default_factory=list)


_UNSUPPORTED_METADATA = {"$comment", "$id", "$schema", "examples", "title"}


def normalize_tool_schema(
    schema: dict[str, Any],
    *,
    max_description_chars: int = 800,
    max_tokens: int | None = None,
) -> NormalizedToolSchema:
    """Make provider-facing JSON Schema smaller without changing validation intent."""

    removed: list[str] = []

    def visit(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if key in _UNSUPPORTED_METADATA:
                    removed.append(f"{path}.{key}")
                    continue
                if key == "description" and isinstance(child, str):
                    result[key] = child[:max_description_chars]
                    if len(child) > max_description_chars:
                        removed.append(f"{path}.description:truncated")
                    continue
                result[key] = visit(child, f"{path}.{key}")
            return result
        if isinstance(value, list):
            return [visit(item, f"{path}[]") for item in value]
        return value

    normalized = visit(schema, "$schema")
    estimated = estimate_tokens(json.dumps(normalized, ensure_ascii=False, sort_keys=True))
    if max_tokens is not None and estimated > max_tokens:
        raise ToolSchemaBudgetExceeded(
            f"normalized tool schema exceeds budget: {estimated}/{max_tokens} tokens"
        )
    return NormalizedToolSchema(
        normalized_schema=normalized,
        removed_keys=removed,
        estimated_tokens=estimated,
    )


def expose_tools(
    tools: Iterable[ToolSpec],
    *,
    query: str = "",
    max_tools: int = 12,
    max_tokens: int = 8_000,
) -> ToolExposure:
    """Select a bounded, query-relevant tool view for one model turn."""

    if max_tools < 1 or max_tokens < 1:
        raise ValueError("max_tools and max_tokens must be positive")
    query_terms = {term.lower() for term in query.split() if term.strip()}

    def rank(tool: ToolSpec) -> tuple[int, int, str]:
        haystack = f"{tool.name} {tool.description}".lower()
        relevance = sum(term in haystack for term in query_terms)
        return (relevance, int(tool.read_only), tool.name)

    ordered = sorted(list(tools), key=rank, reverse=True)
    selected: list[ToolSpec] = []
    dropped: list[str] = []
    used = 0
    for tool in ordered:
        normalized = normalize_tool_schema(tool.input_schema)
        candidate = tool.model_copy(update={"input_schema": normalized.normalized_schema})
        cost = estimate_tokens(
            json.dumps(
                {
                    "name": candidate.name,
                    "description": candidate.description,
                    "input_schema": candidate.input_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if len(selected) >= max_tools or used + cost > max_tokens:
            dropped.append(tool.name)
            continue
        selected.append(candidate)
        used += cost
    return ToolExposure(tools=selected, estimated_tokens=used, dropped_tools=dropped)
