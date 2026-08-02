from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = None
async_session_factory = None


def init_engine(url: str | None = None) -> None:
    global engine, async_session_factory
    settings = get_settings()
    db_url = url or settings.db_url
    engine = create_async_engine(
        db_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=settings.app_debug,
    )
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_session_factory() -> async_sessionmaker:
    if async_session_factory is None:
        init_engine()
    return async_session_factory


async def get_session():
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def close_engine() -> None:
    global engine
    if engine is not None:
        await engine.dispose()
        engine = None
