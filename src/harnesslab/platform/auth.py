from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from enum import StrEnum

from pydantic import BaseModel, Field


class AuthError(PermissionError):
    pass


class CredentialKind(StrEnum):
    API_KEY = "api_key"
    BEARER = "bearer"


class APIKeyRecord(BaseModel):
    key_id: str
    tenant_id: str
    subject: str
    prefix: str
    digest: str
    created_at: float
    expires_at: float | None = None
    revoked_at: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    def active(self, now: float) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


class AuthClaims(BaseModel):
    subject: str
    tenant_id: str
    issued_at: float
    expires_at: float
    key_id: str | None = None
    scopes: frozenset[str] = Field(default_factory=frozenset)


class APIKeyStore:
    """Credential store that only retains keyed digests, never raw API keys."""

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._records: dict[str, APIKeyRecord] = {}

    def issue(
        self,
        *,
        tenant_id: str,
        subject: str,
        ttl_seconds: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> tuple[str, APIKeyRecord]:
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("API key TTL must be positive")
        raw = f"hl_{secrets.token_urlsafe(32)}"
        key_id = f"key_{secrets.token_hex(8)}"
        now = self._clock()
        record = APIKeyRecord(
            key_id=key_id,
            tenant_id=tenant_id,
            subject=subject,
            prefix=raw[:10],
            digest=_digest(raw),
            created_at=now,
            expires_at=now + ttl_seconds if ttl_seconds is not None else None,
            metadata=metadata or {},
        )
        self._records[key_id] = record
        return raw, record

    def authenticate(self, raw_key: str) -> AuthClaims:
        digest = _digest(raw_key)
        now = self._clock()
        for record in self._records.values():
            if hmac.compare_digest(record.digest, digest):
                if not record.active(now):
                    raise AuthError("API key is expired or revoked")
                return AuthClaims(
                    subject=record.subject,
                    tenant_id=record.tenant_id,
                    issued_at=record.created_at,
                    expires_at=record.expires_at or now + 86_400,
                    key_id=record.key_id,
                )
        raise AuthError("invalid API key")

    def revoke(self, key_id: str) -> None:
        try:
            record = self._records[key_id]
        except KeyError as exc:
            raise LookupError(f"API key not found: {key_id}") from exc
        record.revoked_at = self._clock()

    def records(self) -> list[APIKeyRecord]:
        return [record.model_copy(deep=True) for record in self._records.values()]


class TokenSigner:
    """Versioned signed bearer tokens; deployments may replace this with an OIDC verifier."""

    def __init__(self, secret: bytes, *, clock=time.time) -> None:
        if len(secret) < 32:
            raise ValueError("token signing secret must be at least 32 bytes")
        self._secret = secret
        self._clock = clock

    def issue(
        self,
        *,
        tenant_id: str,
        subject: str,
        scopes: frozenset[str] = frozenset(),
        ttl_seconds: float = 900,
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("token TTL must be positive")
        now = self._clock()
        payload = {
            "v": 1,
            "tenant_id": tenant_id,
            "subject": subject,
            "issued_at": now,
            "expires_at": now + ttl_seconds,
            "scopes": sorted(scopes),
        }
        encoded = _encode(payload)
        return f"hlb1.{encoded}.{_sign(encoded, self._secret)}"

    def verify(self, token: str) -> AuthClaims:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "hlb1":
            raise AuthError("invalid bearer token format")
        encoded, signature = parts[1], parts[2]
        if not hmac.compare_digest(signature, _sign(encoded, self._secret)):
            raise AuthError("invalid bearer token signature")
        try:
            payload = json.loads(_decode(encoded))
            if payload.get("v") != 1:
                raise AuthError("unsupported bearer token version")
            claims = AuthClaims.model_validate(payload)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AuthError("invalid bearer token payload") from exc
        if claims.expires_at <= self._clock():
            raise AuthError("bearer token expired")
        return claims


class Authenticator:
    def __init__(self, *, api_keys: APIKeyStore, bearer: TokenSigner) -> None:
        self.api_keys = api_keys
        self.bearer = bearer

    def authenticate(self, credential: str, *, kind: CredentialKind) -> AuthClaims:
        if kind == CredentialKind.API_KEY:
            return self.api_keys.authenticate(credential)
        if kind == CredentialKind.BEARER:
            return self.bearer.verify(credential)
        raise AuthError(f"unsupported credential kind: {kind}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode(encoded: str) -> str:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")


def _sign(encoded: str, secret: bytes) -> str:
    return hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
