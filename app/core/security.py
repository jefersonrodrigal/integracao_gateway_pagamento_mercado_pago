"""Autenticação por JWT (Bearer) e autorização por papéis.

A API não emite tokens em produção: espera-se um provedor de identidade que assine JWTs
com `sub` (identificador do cliente) e `roles` (ex.: ["customer"] ou ["admin"]).
`create_access_token` existe para desenvolvimento, testes e homologação.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError

ROLE_CUSTOMER = "customer"
ROLE_ADMIN = "admin"
KNOWN_ROLES = frozenset({ROLE_CUSTOMER, ROLE_ADMIN})


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    def can_access_customer(self, customer_id: str) -> bool:
        return self.is_admin or self.subject == customer_id

    def require_admin(self) -> None:
        if not self.is_admin:
            raise PermissionDeniedError()


def create_access_token(
    settings: Settings,
    subject: str,
    roles: list[str] | None = None,
    expires_in: timedelta = timedelta(hours=1),
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "roles": roles or [ROLE_CUSTOMER],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in,
    }
    return jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_access_token(settings: Settings, token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["sub", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError:
        raise AuthenticationError() from None

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 64:
        raise AuthenticationError()
    raw_roles = claims.get("roles", [])
    if not isinstance(raw_roles, list):
        raise AuthenticationError()
    roles = frozenset(role for role in raw_roles if role in KNOWN_ROLES)
    if not roles:
        raise PermissionDeniedError()
    return Principal(subject=subject, roles=roles)
