from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class ManifestError(ValueError):
    pass


class MCPServerManifest(BaseModel):
    server_id: str = Field(min_length=1, max_length=100)
    namespace: str = Field(min_length=1, max_length=100)
    protocol_version: str = Field(min_length=1, max_length=50)
    endpoint: str = Field(min_length=1, max_length=2000)
    tool_schema_fingerprint: str = Field(min_length=1, max_length=200)
    egress_hosts: frozenset[str] = Field(default_factory=frozenset)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    metadata: dict[str, str] = Field(default_factory=dict)


class SignedMCPManifest(BaseModel):
    manifest: MCPServerManifest
    algorithm: str = "hmac-sha256"
    signature: str


class ManifestSigner:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("manifest secret must be at least 32 bytes")
        self._secret = secret

    def sign(self, manifest: MCPServerManifest) -> SignedMCPManifest:
        return SignedMCPManifest(
            manifest=manifest,
            signature=_signature(_canonical(manifest), self._secret),
        )

    def verify(
        self,
        signed: SignedMCPManifest,
        *,
        now: datetime | None = None,
    ) -> MCPServerManifest:
        expected = _signature(_canonical(signed.manifest), self._secret)
        if signed.algorithm != "hmac-sha256" or not hmac.compare_digest(signed.signature, expected):
            raise ManifestError("invalid MCP manifest signature")
        if signed.manifest.expires_at <= (now or datetime.now(UTC)):
            raise ManifestError("MCP manifest expired")
        return signed.manifest


class EgressPolicy(BaseModel):
    allowed_hosts: frozenset[str] = Field(default_factory=frozenset)
    allowed_schemes: frozenset[str] = Field(default_factory=lambda: frozenset({"https"}))
    allow_private_networks: bool = False

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if not any(_host_matches(host, allowed) for allowed in self.allowed_hosts):
            return False
        return self.allow_private_networks or not _is_private_host(host)


def default_manifest(
    *,
    server_id: str,
    namespace: str,
    endpoint: str,
    tool_schema_fingerprint: str,
    egress_hosts: frozenset[str],
    ttl: timedelta = timedelta(hours=1),
) -> MCPServerManifest:
    now = datetime.now(UTC)
    return MCPServerManifest(
        server_id=server_id,
        namespace=namespace,
        protocol_version="mcp/1",
        endpoint=endpoint,
        tool_schema_fingerprint=tool_schema_fingerprint,
        egress_hosts=egress_hosts,
        issued_at=now,
        expires_at=now + ttl,
    )


def _canonical(manifest: MCPServerManifest) -> str:
    payload = manifest.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _signature(value: str, secret: bytes) -> str:
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _host_matches(host: str, allowed: str) -> bool:
    normalized = allowed.lower().rstrip(".")
    return host == normalized or (normalized.startswith("*.") and host.endswith(normalized[1:]))


def _is_private_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith(("10.", "192.168."))
