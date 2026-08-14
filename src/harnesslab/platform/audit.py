from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.trace_contract import redact_data


class AuditIntegrityError(ValueError):
    pass


class AuditRecord(BaseModel):
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    previous_hash: str
    record_hash: str
    signature: str | None = None


class AuditChain:
    """Append-only hash chain for security-sensitive control-plane decisions."""

    def __init__(self, *, signing_secret: bytes | None = None) -> None:
        self._secret = signing_secret
        self._records: list[AuditRecord] = []

    def append(
        self,
        event_type: str,
        *,
        tenant_id: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditRecord:
        sequence = len(self._records) + 1
        previous_hash = self._records[-1].record_hash if self._records else "genesis"
        clean_payload = redact_data(payload or {})
        unsigned = {
            "sequence": sequence,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "subject": subject,
            "payload": clean_payload,
            "previous_hash": previous_hash,
        }
        record_hash = _digest(unsigned)
        signature = self._sign(record_hash)
        record = AuditRecord(
            **unsigned,
            record_hash=record_hash,
            signature=signature,
        )
        self._records.append(record)
        return record

    def verify(self) -> bool:
        previous_hash = "genesis"
        for index, record in enumerate(self._records, start=1):
            if record.sequence != index or record.previous_hash != previous_hash:
                return False
            unsigned = record.model_dump(
                mode="json",
                exclude={"created_at", "record_hash", "signature"},
            )
            if _digest(unsigned) != record.record_hash:
                return False
            if self._secret is not None and not hmac.compare_digest(
                self._sign(record.record_hash) or "", record.signature or ""
            ):
                return False
            previous_hash = record.record_hash
        return True

    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def export(self) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self._records]

    def import_records(self, records: list[AuditRecord]) -> None:
        if self._records:
            raise AuditIntegrityError("cannot import into a non-empty audit chain")
        self._records = list(records)
        if not self.verify():
            self._records = []
            raise AuditIntegrityError("audit chain failed integrity verification")

    def _sign(self, value: str) -> str | None:
        if self._secret is None:
            return None
        return hmac.new(self._secret, value.encode("ascii"), hashlib.sha256).hexdigest()


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
