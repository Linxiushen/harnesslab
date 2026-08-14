from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field

from harnesslab.agentic.contracts import AgentToolSpec, PolicyDecision


class CapabilityError(PermissionError):
    pass


class CapabilityClaims(BaseModel):
    subject: str
    run_id: str
    capabilities: list[str]
    issued_at: int
    expires_at: int
    token_id: str


class CapabilityIssuer:
    """Run-scoped HMAC capability tokens; raw API capabilities are never trusted."""

    def __init__(self, secret: bytes, *, clock_skew_seconds: int = 5) -> None:
        if len(secret) < 32:
            raise ValueError("capability signing secret must contain at least 32 bytes")
        self._secret = secret
        self._clock_skew = clock_skew_seconds

    def issue(
        self,
        *,
        subject: str,
        run_id: str,
        capabilities: list[str],
        ttl_seconds: int = 300,
        now: int | None = None,
    ) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = {
            "subject": subject,
            "run_id": run_id,
            "capabilities": sorted(set(capabilities)),
            "issued_at": issued_at,
            "expires_at": issued_at + ttl_seconds,
            "token_id": uuid4().hex,
        }
        encoded = self._encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        signature = self._encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str, *, run_id: str, now: int | None = None) -> CapabilityClaims:
        try:
            encoded, supplied = token.split(".", 1)
        except ValueError as exc:
            raise CapabilityError("malformed capability token") from exc
        expected = self._encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise CapabilityError("invalid capability signature")
        try:
            payload = json.loads(self._decode(encoded))
            claims = CapabilityClaims.model_validate(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            raise CapabilityError("invalid capability payload") from exc
        timestamp = int(time.time() if now is None else now)
        if claims.expires_at + self._clock_skew < timestamp:
            raise CapabilityError("capability token expired")
        if claims.issued_at - self._clock_skew > timestamp:
            raise CapabilityError("capability token issued in the future")
        if claims.run_id != run_id:
            raise CapabilityError("capability token is scoped to another run")
        return claims

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class InjectionAssessment(BaseModel):
    detected: bool
    score: int
    patterns: list[str] = Field(default_factory=list)


class PromptInjectionDetector:
    _PATTERNS = {
        "instruction-override": re.compile(
            r"ignore (all |any )?(previous|prior|system)( system)? instructions|"
            r"忽略.{0,8}(之前|系统|上面).{0,8}(指令|提示词)",
            re.IGNORECASE,
        ),
        "secret-exfiltration": re.compile(
            r"(reveal|print|send|exfiltrate).{0,24}(secret|token|credential|system prompt)|"
            r"(泄露|输出|发送).{0,20}(密钥|令牌|凭证|系统提示词)",
            re.IGNORECASE,
        ),
        "tool-escalation": re.compile(
            r"(call|invoke|use).{0,20}(delete|deploy|payment|admin).{0,20}(tool|function)|"
            r"调用.{0,20}(删除|部署|支付|管理员).{0,12}(工具|函数)",
            re.IGNORECASE,
        ),
        "role-spoofing": re.compile(
            r"\b(system|developer|administrator)\s*:\s*|"
            r"(系统|开发者|管理员)\s*[：:]",
            re.IGNORECASE,
        ),
    }

    def inspect(self, value: Any) -> InjectionAssessment:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        matches = [name for name, pattern in self._PATTERNS.items() if pattern.search(text)]
        return InjectionAssessment(
            detected=bool(matches), score=min(100, len(matches) * 35), patterns=matches
        )


class PolicyEngine:
    def __init__(self, detector: PromptInjectionDetector | None = None) -> None:
        self.detector = detector or PromptInjectionDetector()

    def authorize(
        self,
        tool: AgentToolSpec,
        claims: CapabilityClaims,
        arguments: dict[str, Any],
        *,
        tainted_dependencies: bool = False,
    ) -> PolicyDecision:
        required = set(tool.required_capabilities)
        if not tool.read_only:
            required.add("tool.write")
        granted = set(claims.capabilities)
        missing = required - granted
        risk = "low" if tool.read_only else "high"
        if tool.trust_boundary == "external":
            risk = "medium" if tool.read_only else "critical"
        if missing:
            return PolicyDecision(
                allowed=False,
                reason=f"missing capabilities: {', '.join(sorted(missing))}",
                required=sorted(required),
                granted=sorted(granted),
                risk=risk,
            )
        if tainted_dependencies and not tool.read_only:
            return PolicyDecision(
                allowed=False,
                reason="untrusted tool output cannot drive a side-effecting tool",
                required=sorted(required),
                granted=sorted(granted),
                risk="critical",
            )
        if tool.allowed_hosts:
            for value in self._url_values(arguments):
                host = (urlparse(value).hostname or "").lower()
                if host not in {item.lower() for item in tool.allowed_hosts}:
                    return PolicyDecision(
                        allowed=False,
                        reason=f"egress host is not allowlisted: {host or '<missing>'}",
                        required=sorted(required),
                        granted=sorted(granted),
                        risk="high",
                    )
        return PolicyDecision(
            allowed=True,
            reason="capabilities and trust boundary satisfied",
            required=sorted(required),
            granted=sorted(granted),
            risk=risk,
        )

    @staticmethod
    def _url_values(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for nested in value.values():
                found.extend(PolicyEngine._url_values(nested))
        elif isinstance(value, list):
            for nested in value:
                found.extend(PolicyEngine._url_values(nested))
        elif isinstance(value, str) and value.lower().startswith(("http://", "https://")):
            found.append(value)
        return found
