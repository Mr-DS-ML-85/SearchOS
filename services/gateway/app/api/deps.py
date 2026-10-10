from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AuthError
from app.models import ApiKey, ApiKeyRole
from app.security import get_security_service


async def extract_token(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def current_api_key(
    request: Request,
    token: Annotated[str | None, Depends(extract_token)] = None,
    session: Annotated[AsyncSession, Depends(get_session)] = None,
) -> ApiKey:
    security = get_security_service()
    client_host = request.client.host if request.client else "unknown"
    try:
        api_key = await security.authenticate(session, token)
    except AuthError:
        # Throttle *failures only*: a limiter checked before authenticate
        # counted every successful request too, locking out legitimate clients
        # after 10 requests a minute.
        await security.check_auth_failures(client_host)
        raise
    await security.check_rate_limit(api_key)
    return api_key


async def current_admin_key(
    api_key: Annotated[ApiKey, Depends(current_api_key)],
) -> ApiKey:
    security = get_security_service()
    await security.require_role(api_key, ApiKeyRole.admin)
    return api_key
