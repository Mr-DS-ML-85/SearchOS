from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_admin_key
from app.db import get_session
from app.errors import AppError, to_http_error
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


@router.post("/v1/admin/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[ApiKey, Depends(current_admin_key)],
) -> IngestResponse:
    job = IngestJob(
        id=uuid.uuid4(),
        url=req.url,
        kind=IngestKind(req.kind),
        status=IngestJobStatus.pending,
    )
    session.add(job)
    try:
        await session.commit()
    except AppError as e:
        await session.rollback()
        raise to_http_error(e) from e
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
    except AppError as e:
        await session.rollback()
        raise to_http_error(e) from e
    return CreateKeyResponse(api_key=raw_key, name=req.name, role=req.role)


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
