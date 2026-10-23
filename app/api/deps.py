from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.container import Container
from app.core.exceptions import AuthenticationError
from app.core.security import Principal, decode_access_token

_bearer = HTTPBearer(auto_error=False, description="JWT emitido pelo provedor de identidade.")


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_principal(
    container: Annotated[Container, Depends(get_container)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    return decode_access_token(container.settings, credentials.credentials)


ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Chave única por operação (8-128 caracteres). Obrigatória.",
        min_length=1,
        max_length=128,
    ),
]
