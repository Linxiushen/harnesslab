from __future__ import annotations

import pytest

from harnesslab.platform.auth import APIKeyStore, AuthError, CredentialKind, TokenSigner
from harnesslab.platform.mcp_manifest import (
    EgressPolicy,
    ManifestError,
    ManifestSigner,
    default_manifest,
)
from harnesslab.platform.storage import SQLitePlatformStore, StorageConflict


def test_api_keys_store_only_digest_and_revoke() -> None:
    store = APIKeyStore()
    raw, record = store.issue(tenant_id="tenant-a", subject="alice")
    claims = store.authenticate(raw)
    assert claims.key_id == record.key_id
    assert raw not in store.records()[0].model_dump_json()
    store.revoke(record.key_id)
    with pytest.raises(AuthError, match="revoked"):
        store.authenticate(raw)


def test_signed_bearer_tokens_reject_tamper_and_expiry() -> None:
    now = [100.0]
    signer = TokenSigner(b"s" * 32, clock=lambda: now[0])
    token = signer.issue(
        tenant_id="tenant-a",
        subject="alice",
        scopes=frozenset({"run:create"}),
    )
    assert signer.verify(token).subject == "alice"
    with pytest.raises(AuthError, match="signature"):
        signer.verify(token[:-1] + ("0" if token[-1] != "0" else "1"))
    now[0] = 1_000
    with pytest.raises(AuthError, match="expired"):
        signer.verify(token)
    assert CredentialKind.BEARER.value == "bearer"


def test_manifest_signature_and_egress_policy() -> None:
    manifest = default_manifest(
        server_id="repo",
        namespace="repo",
        endpoint="https://mcp.example.com",
        tool_schema_fingerprint="sha256:tools",
        egress_hosts=frozenset({"api.example.com"}),
    )
    signer = ManifestSigner(b"m" * 32)
    signed = signer.sign(manifest)
    assert signer.verify(signed).server_id == "repo"
    policy = EgressPolicy(allowed_hosts=frozenset({"*.example.com"}))
    assert policy.allows("https://api.example.com/v1") is True
    assert policy.allows("http://api.example.com/v1") is False
    assert policy.allows("https://localhost/admin") is False
    with pytest.raises(ManifestError):
        signer.verify(signed.model_copy(update={"signature": "bad"}))


def test_sqlite_platform_store_supports_cas_ttl_and_prune(tmp_path) -> None:
    now = [100.0]
    store = SQLitePlatformStore(tmp_path / "platform.db", clock=lambda: now[0])
    first = store.put("manifests", "repo", {"version": "1"})
    second = store.put("manifests", "repo", {"version": "2"}, expected_version=1)
    assert first.version == 1
    assert second.version == 2
    with pytest.raises(StorageConflict):
        store.put("manifests", "repo", {"version": "bad"}, expected_version=1)
    store.put("sessions", "short", {"ok": True}, ttl_seconds=10)
    now[0] = 111
    assert store.prune() == 1
    assert store.get("sessions", "short") is None
    assert store.delete("manifests", "repo") is True
    store.close()
