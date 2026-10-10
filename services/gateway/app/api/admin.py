from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_admin_key
from app.cache import get_cache
from app.config import get_settings
from app.db import get_session
from app.errors import ValidationError
from app.models import ApiKey, ApiKeyRole, IngestJob, IngestJobStatus, IngestKind
from app.schemas import IngestRequest, IngestResponse
from app.security import generate_api_key, hash_api_key

router = APIRouter(tags=["admin"])


class CreateKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    role: ApiKeyRole = ApiKeyRole.user


class CreateKeyResponse(BaseModel):
    api_key: str
    name: str
    role: ApiKeyRole


class RevokeKeyResponse(BaseModel):
    id: str
    revoked: bool


@router.post("/v1/admin/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[ApiKey, Depends(current_admin_key)],
) -> IngestResponse:
    from app.netguard import BlockedURLError, validate_url

    try:
        validate_url(req.url)
    except BlockedURLError as exc:
        # Admin-supplied seeds are still attacker-influenceable input.
        raise ValidationError(f"URL refused by egress policy: {exc}") from exc

    job = IngestJob(
        id=uuid.uuid4(),
        url=req.url,
        kind=IngestKind(req.kind),
        status=IngestJobStatus.pending,
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError as e:
        # A commit failure raises IntegrityError/DBAPIError, never AppError, so
        # the previous `except AppError` never fired and DB errors surfaced as
        # blind 500s.
        await session.rollback()
        raise ValidationError(f"Could not enqueue ingest job: {e.orig}") from e
    except SQLAlchemyError:
        await session.rollback()
        raise
    return IngestResponse(job_id=str(job.id), url=req.url, kind=req.kind, status=job.status.value)


@router.post("/v1/admin/keys", response_model=CreateKeyResponse)
async def create_api_key(
    req: CreateKeyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[ApiKey, Depends(current_admin_key)],
) -> CreateKeyResponse:
    raw_key = generate_api_key()
    api_key = ApiKey(
        name=req.name,
        key_hash=hash_api_key(raw_key),
        role=req.role,
        active=True,
    )
    session.add(api_key)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise ValidationError(f"Could not create API key: {e.orig}") from e
    return CreateKeyResponse(api_key=raw_key, name=req.name, role=req.role)


@router.delete("/v1/admin/keys/{key_id}", response_model=RevokeKeyResponse)
async def revoke_api_key(
    key_id: str,
    _: Annotated[ApiKey, Depends(current_admin_key)],
) -> RevokeKeyResponse:
    """Deactivate a key.

    There was previously no revocation path at all: a leaked key stayed valid
    for the lifetime of the database row.
    """
    from uuid import UUID

    from sqlalchemy import update

    from app.db import get_session_factory

    try:
        parsed = UUID(key_id)
    except ValueError as exc:
        raise ValidationError("key_id must be a UUID") from exc

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == parsed).values(active=False))
        await session.commit()
    return RevokeKeyResponse(id=key_id, revoked=True)


@router.post("/v1/admin/cache/flush")
async def flush_cache(_: Annotated[ApiKey, Depends(current_admin_key)]) -> dict[str, int | str]:
    """Drop cached search results and extracts.

    Staleness was previously TTL-only and Cache.delete was dead code, so an
    operator could not force a refresh after re-crawling.
    """
    settings = get_settings()
    cache = get_cache()
    removed = 0
    for pattern in ("search:*", "extract:*"):
        removed += await cache.invalidate(pattern)
    return {"removed": removed, "cache_ttl_seconds": settings.search_cache_ttl_seconds}


@router.get("/v1/admin/keys")
async def list_api_keys(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[ApiKey, Depends(current_admin_key)],
) -> list[dict]:
    keys = (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    return [
        {
            "id": str(k.id),
            "name": k.name,
            "role": k.role.value,
            "active": k.active,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        }
        for k in keys
    ]
