from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harnesslab.agentic.memory import ContextCompiler, HybridMemoryStore, MemoryConflict
from harnesslab.models import Message


def test_context_compiler_supports_message_and_token_windows() -> None:
    messages = [
        Message(role="system", content="Keep this instruction."),
        Message(role="user", content="old request " * 40),
        Message(role="assistant", content="old answer " * 40),
        Message(role="user", content="new request"),
    ]
    compiler = ContextCompiler()

    message_window = compiler.compile(messages, max_messages=2)
    token_window = compiler.compile(messages, max_tokens=40, reserve_tokens=8)

    assert [message.role for message in message_window.messages] == ["system", "user"]
    assert message_window.messages[-1].content == "new request"
    assert token_window.messages[0].role == "system"
    assert token_window.messages[-1].content == "new request"
    assert token_window.dropped_messages >= 2
    assert token_window.token_count <= 32


def test_context_compiler_rejects_ambiguous_window_configuration() -> None:
    compiler = ContextCompiler()
    with pytest.raises(ValueError, match="exactly one"):
        compiler.compile([], max_messages=3, max_tokens=100)


def test_memory_deduplicates_and_detects_optimistic_conflicts() -> None:
    store = HybridMemoryStore()
    now = datetime(2026, 8, 10, tzinfo=UTC)
    first = store.remember("user-1", "Authorize before state mutation", now=now)
    duplicate = store.remember(
        "user-1",
        "  authorize BEFORE state mutation ",
        importance=0.9,
        now=now + timedelta(seconds=1),
    )

    assert first.id == duplicate.id
    assert duplicate.version == 2
    assert duplicate.importance == 0.9
    assert store.count("user-1") == 1

    with pytest.raises(MemoryConflict, match="version conflict"):
        store.update(
            "user-1",
            first.id,
            "new text",
            expected_version=1,
            now=now + timedelta(seconds=2),
        )


def test_memory_ttl_retrieval_and_bounded_growth() -> None:
    store = HybridMemoryStore(max_records_per_namespace=2)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    store.remember("team", "checkout authorization incident", ttl_seconds=10, now=now)
    store.remember("team", "database connection pool", importance=0.2, now=now)
    store.remember("team", "checkout rollback runbook", importance=0.9, now=now)

    assert store.count("team") == 2
    hits = store.search("team", "checkout rollback", now=now + timedelta(seconds=5))
    assert hits[0].record.text == "checkout rollback runbook"

    removed = store.prune("team", now=now + timedelta(seconds=11))
    assert removed in {0, 1}
    assert all(
        hit.record.expires_at is None
        for hit in store.search("team", "checkout", now=now + timedelta(seconds=11))
    )
