from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Permission(StrEnum):
    RUN_CREATE = "run:create"
    RUN_READ = "run:read"
    RUN_CANCEL = "run:cancel"
    ARTIFACT_READ = "artifact:read"
    TOOL_READ = "tool:read"
    TOOL_WRITE = "tool:write"
    EVAL_RUN = "eval:run"
    ADMIN = "admin:*"


class Role(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: frozenset[Permission] = Field(default_factory=frozenset)

    def allows(self, permission: Permission) -> bool:
        return Permission.ADMIN in self.permissions or permission in self.permissions


class Principal(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=100)
    roles: frozenset[str] = Field(default_factory=frozenset)
    attributes: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False


class Tenant(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    metadata: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False


class AccessDecision(BaseModel):
    allowed: bool
    subject: str
    tenant_id: str
    permission: Permission
    reason: str
    matched_roles: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _PrincipalKey:
    tenant_id: str
    subject: str


BUILTIN_ROLES: dict[str, Role] = {
    "viewer": Role(
        name="viewer",
        permissions=frozenset(
            {Permission.RUN_READ, Permission.ARTIFACT_READ, Permission.TOOL_READ}
        ),
    ),
    "operator": Role(
        name="operator",
        permissions=frozenset(
            {
                Permission.RUN_CREATE,
                Permission.RUN_READ,
                Permission.RUN_CANCEL,
                Permission.ARTIFACT_READ,
                Permission.TOOL_READ,
                Permission.EVAL_RUN,
            }
        ),
    ),
    "developer": Role(
        name="developer",
        permissions=frozenset(
            {
                Permission.RUN_CREATE,
                Permission.RUN_READ,
                Permission.RUN_CANCEL,
                Permission.ARTIFACT_READ,
                Permission.TOOL_READ,
                Permission.TOOL_WRITE,
                Permission.EVAL_RUN,
            }
        ),
    ),
    "admin": Role(name="admin", permissions=frozenset({Permission.ADMIN})),
}


class TenantRegistry:
    """In-memory tenancy and RBAC registry used by API boundaries and tests.

    Persistence belongs behind this interface. Authorization is deliberately deny-by-default:
    an unknown tenant, disabled principal, missing role, or unknown role cannot execute.
    """

    def __init__(self, *, roles: dict[str, Role] | None = None) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._principals: dict[_PrincipalKey, Principal] = {}
        self._roles = dict(roles or BUILTIN_ROLES)

    def register_tenant(self, tenant: Tenant) -> None:
        if tenant.id in self._tenants:
            raise ValueError(f"tenant already exists: {tenant.id}")
        self._tenants[tenant.id] = tenant

    def register_role(self, role: Role) -> None:
        if role.name in self._roles:
            raise ValueError(f"role already exists: {role.name}")
        self._roles[role.name] = role

    def bind(self, principal: Principal) -> None:
        self._require_tenant(principal.tenant_id)
        unknown = sorted(set(principal.roles) - self._roles.keys())
        if unknown:
            raise ValueError(f"unknown roles: {unknown}")
        key = _PrincipalKey(principal.tenant_id, principal.subject)
        self._principals[key] = principal

    def get_principal(self, tenant_id: str, subject: str) -> Principal:
        try:
            return self._principals[_PrincipalKey(tenant_id, subject)]
        except KeyError as exc:
            raise LookupError(f"principal not found: {tenant_id}/{subject}") from exc

    def authorize(self, principal: Principal, permission: Permission) -> AccessDecision:
        if principal.tenant_id not in self._tenants:
            return self._deny(principal, permission, "tenant does not exist")
        tenant = self._tenants[principal.tenant_id]
        if tenant.disabled:
            return self._deny(principal, permission, "tenant is disabled")
        if principal.disabled:
            return self._deny(principal, permission, "principal is disabled")
        matched = [
            role_name
            for role_name in sorted(principal.roles)
            if self._roles[role_name].allows(permission)
        ]
        if not matched:
            return self._deny(principal, permission, "permission is not granted")
        return AccessDecision(
            allowed=True,
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            permission=permission,
            reason="role grants permission",
            matched_roles=matched,
        )

    def require(self, principal: Principal, permission: Permission) -> AccessDecision:
        decision = self.authorize(principal, permission)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return decision

    def list_tenants(self) -> list[Tenant]:
        return list(self._tenants.values())

    def list_principals(self, tenant_id: str) -> list[Principal]:
        return [
            principal
            for key, principal in self._principals.items()
            if key.tenant_id == tenant_id
        ]

    def _require_tenant(self, tenant_id: str) -> Tenant:
        try:
            return self._tenants[tenant_id]
        except KeyError as exc:
            raise LookupError(f"tenant not found: {tenant_id}") from exc

    @staticmethod
    def _deny(principal: Principal, permission: Permission, reason: str) -> AccessDecision:
        return AccessDecision(
            allowed=False,
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            permission=permission,
            reason=reason,
        )


def principal_context(principal: Principal) -> dict[str, Any]:
    """Return a redaction-friendly context for traces and policy evaluation."""
    return {
        "subject": principal.subject,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "attributes": dict(principal.attributes),
    }
