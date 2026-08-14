from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class StorageConflict(RuntimeError):
    pass


class StoredValue(BaseModel):
    namespace: str
    key: str
    value: dict[str, Any]
    version: int = Field(ge=1)
    expires_at: float | None = None
    updated_at: float


class SQLitePlatformStore:
    """Durable KV/CAS store for platform metadata and provider manifests."""

    def __init__(self, path: str | Path = ":memory:", *, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_values (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                version INTEGER NOT NULL,
                expires_at REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(namespace, key)
            )
            """
        )
        self._connection.commit()

    def put(
        self,
        namespace: str,
        key: str,
        value: dict[str, Any],
        *,
        expected_version: int | None = None,
        ttl_seconds: float | None = None,
    ) -> StoredValue:
        if not namespace or not key:
            raise ValueError("namespace and key are required")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("TTL must be positive")
        now = self._clock()
        with self._lock, self._connection:
            self._prune_locked(now)
            current = self._get_locked(namespace, key)
            if expected_version is not None:
                actual = current.version if current else None
                if actual != expected_version:
                    raise StorageConflict(
                        f"version conflict for {namespace}/{key}: "
                        f"expected={expected_version}, actual={actual}"
                    )
            version = current.version + 1 if current else 1
            expires_at = now + ttl_seconds if ttl_seconds is not None else None
            self._connection.execute(
                """
                INSERT INTO platform_values(
                    namespace, key, value_json, version, expires_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_json=excluded.value_json,
                    version=excluded.version,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    namespace,
                    key,
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    version,
                    expires_at,
                    now,
                ),
            )
            return StoredValue(
                namespace=namespace,
                key=key,
                value=value,
                version=version,
                expires_at=expires_at,
                updated_at=now,
            )

    def get(self, namespace: str, key: str) -> StoredValue | None:
        with self._lock, self._connection:
            self._prune_locked(self._clock())
            return self._get_locked(namespace, key)

    def delete(self, namespace: str, key: str) -> bool:
        with self._lock, self._connection:
            result = self._connection.execute(
                "DELETE FROM platform_values WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            return result.rowcount > 0

    def list(self, namespace: str, *, limit: int = 100) -> list[StoredValue]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock, self._connection:
            self._prune_locked(self._clock())
            rows = self._connection.execute(
                "SELECT * FROM platform_values WHERE namespace = ? ORDER BY key LIMIT ?",
                (namespace, limit),
            ).fetchall()
            return [_row_to_value(row) for row in rows]

    def prune(self) -> int:
        with self._lock, self._connection:
            return self._prune_locked(self._clock())

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _get_locked(self, namespace: str, key: str) -> StoredValue | None:
        row = self._connection.execute(
            "SELECT * FROM platform_values WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
        return _row_to_value(row) if row else None

    def _prune_locked(self, now: float) -> int:
        result = self._connection.execute(
            "DELETE FROM platform_values WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        return result.rowcount


def _row_to_value(row: sqlite3.Row) -> StoredValue:
    return StoredValue(
        namespace=row["namespace"],
        key=row["key"],
        value=json.loads(row["value_json"]),
        version=row["version"],
        expires_at=row["expires_at"],
        updated_at=row["updated_at"],
    )
