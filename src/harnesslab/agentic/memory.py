from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field

from harnesslab.agentic.budget import estimate_tokens
from harnesslab.models import Message


class CompiledContext(BaseModel):
    messages: list[Message]
    token_count: int
    dropped_messages: int
    strategy: Literal["message-window", "token-window"]


class ContextCompiler:
    """Keeps system instructions plus the newest complete tool-call exchanges."""

    def compile(
        self,
        messages: list[Message],
        *,
        max_messages: int | None = None,
        max_tokens: int | None = None,
        reserve_tokens: int = 0,
    ) -> CompiledContext:
        if (max_messages is None) == (max_tokens is None):
            raise ValueError("choose exactly one of max_messages or max_tokens")
        system = [message for message in messages if message.role == "system"][:1]
        remaining = [message for message in messages if message not in system]
        selected: list[Message] = []
        if max_messages is not None:
            available = max(0, max_messages - len(system))
            selected = remaining[-available:] if available else []
            strategy: Literal["message-window", "token-window"] = "message-window"
        else:
            limit = max(0, (max_tokens or 0) - reserve_tokens)
            used = sum(self._message_tokens(message) for message in system)
            for message in reversed(remaining):
                cost = self._message_tokens(message)
                if used + cost > limit:
                    continue
                selected.append(message)
                used += cost
            selected.reverse()
            strategy = "token-window"

        compiled = system + selected
        return CompiledContext(
            messages=compiled,
            token_count=sum(self._message_tokens(message) for message in compiled),
            dropped_messages=len(messages) - len(compiled),
            strategy=strategy,
        )

    @staticmethod
    def _message_tokens(message: Message) -> int:
        return estimate_tokens(message.content) + 4


class MemoryRecord(BaseModel):
    id: str
    namespace: str
    text: str
    fingerprint: str
    terms: dict[str, int]
    importance: float = Field(default=0.5, ge=0, le=1)
    version: int = 1
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryHit(BaseModel):
    record: MemoryRecord
    score: float
    lexical_score: float
    recency_score: float


class MemoryConflict(RuntimeError):
    pass


class HybridMemoryStore:
    """Bounded, TTL-aware lexical memory with optimistic concurrency and deduplication."""

    def __init__(self, *, max_records_per_namespace: int = 500) -> None:
        self.max_records = max_records_per_namespace
        self._records: dict[str, dict[str, MemoryRecord]] = defaultdict(dict)
        self._fingerprints: dict[str, dict[str, str]] = defaultdict(dict)

    def remember(
        self,
        namespace: str,
        text: str,
        *,
        importance: float = 0.5,
        ttl_seconds: int | None = None,
        metadata: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> MemoryRecord:
        timestamp = now or datetime.now(UTC)
        normalized = " ".join(text.lower().split())
        fingerprint = sha256(normalized.encode()).hexdigest()
        if existing_id := self._fingerprints[namespace].get(fingerprint):
            existing = self._records[namespace][existing_id]
            existing.updated_at = timestamp
            existing.importance = max(existing.importance, importance)
            existing.version += 1
            if ttl_seconds is not None:
                existing.expires_at = timestamp + timedelta(seconds=ttl_seconds)
            return existing.model_copy(deep=True)

        record_id = f"mem_{fingerprint[:16]}"
        record = MemoryRecord(
            id=record_id,
            namespace=namespace,
            text=text,
            fingerprint=fingerprint,
            terms=dict(self._terms(text)),
            importance=importance,
            created_at=timestamp,
            updated_at=timestamp,
            expires_at=timestamp + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
            metadata=metadata or {},
        )
        self._records[namespace][record_id] = record
        self._fingerprints[namespace][fingerprint] = record_id
        self.prune(namespace, now=timestamp)
        return record.model_copy(deep=True)

    def update(
        self,
        namespace: str,
        record_id: str,
        text: str,
        *,
        expected_version: int,
        now: datetime | None = None,
    ) -> MemoryRecord:
        record = self._records[namespace][record_id]
        if record.version != expected_version:
            raise MemoryConflict(
                f"memory version conflict: expected {expected_version}, actual {record.version}"
            )
        old_fingerprint = record.fingerprint
        normalized = " ".join(text.lower().split())
        record.text = text
        record.fingerprint = sha256(normalized.encode()).hexdigest()
        record.terms = dict(self._terms(text))
        record.updated_at = now or datetime.now(UTC)
        record.version += 1
        self._fingerprints[namespace].pop(old_fingerprint, None)
        self._fingerprints[namespace][record.fingerprint] = record_id
        return record.model_copy(deep=True)

    def search(
        self,
        namespace: str,
        query: str,
        *,
        limit: int = 5,
        now: datetime | None = None,
    ) -> list[MemoryHit]:
        timestamp = now or datetime.now(UTC)
        self.prune(namespace, now=timestamp)
        query_terms = self._terms(query)
        hits: list[MemoryHit] = []
        for record in self._records[namespace].values():
            lexical = self._cosine(query_terms, Counter(record.terms))
            age_hours = max(0.0, (timestamp - record.updated_at).total_seconds() / 3600)
            recency = math.exp(-age_hours / (24 * 14))
            score = 0.72 * lexical + 0.18 * recency + 0.10 * record.importance
            if lexical > 0:
                hits.append(
                    MemoryHit(
                        record=record.model_copy(deep=True),
                        score=round(score, 6),
                        lexical_score=round(lexical, 6),
                        recency_score=round(recency, 6),
                    )
                )
        return sorted(hits, key=lambda hit: (hit.score, hit.record.updated_at), reverse=True)[
            :limit
        ]

    def prune(self, namespace: str, *, now: datetime | None = None) -> int:
        timestamp = now or datetime.now(UTC)
        records = self._records[namespace]
        expired = [
            record_id
            for record_id, record in records.items()
            if record.expires_at is not None and record.expires_at <= timestamp
        ]
        for record_id in expired:
            self._remove(namespace, record_id)
        overflow = max(0, len(records) - self.max_records)
        if overflow:
            ranked = sorted(
                records.values(),
                key=lambda record: (record.importance, record.updated_at),
            )
            for record in ranked[:overflow]:
                self._remove(namespace, record.id)
        return len(expired) + overflow

    def count(self, namespace: str) -> int:
        return len(self._records[namespace])

    def _remove(self, namespace: str, record_id: str) -> None:
        record = self._records[namespace].pop(record_id)
        self._fingerprints[namespace].pop(record.fingerprint, None)

    @staticmethod
    def _terms(text: str) -> Counter[str]:
        lowered = text.lower()
        words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", lowered)
        cjk = [
            "".join(words[index : index + 2])
            for index in range(len(words) - 1)
            if len(words[index]) == 1
        ]
        return Counter(words + cjk)

    @staticmethod
    def _cosine(left: Counter[str], right: Counter[str]) -> float:
        common = set(left) & set(right)
        numerator = sum(left[term] * right[term] for term in common)
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0
