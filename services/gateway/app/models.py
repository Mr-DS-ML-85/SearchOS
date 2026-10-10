from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    cast,
    func,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REGCONFIG, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import EMBEDDING_DIM
from app.db import Base


class ApiKeyRole(str, enum.Enum):
    user = "user"
    admin = "admin"


class IngestJobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class IngestKind(str, enum.Enum):
    url = "url"
    sitemap = "sitemap"


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[ApiKeyRole] = mapped_column(Enum(ApiKeyRole, name="apikey_role"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tsv: Mapped[object | None] = mapped_column(TSVECTOR, nullable=True)
    #: 64-bit content fingerprint for near-duplicate detection; see app/search/simhash.py
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="crawler")
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    chunks: Mapped[list[Chunk]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")


class IngestJob(Base):
    __tablename__ = "ingest_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text)
    kind: Mapped[IngestKind] = mapped_column(Enum(IngestKind, name="ingest_kind"))
    status: Mapped[IngestJobStatus] = mapped_column(
        Enum(IngestJobStatus, name="ingest_job_status"), default=IngestJobStatus.pending, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Backoff gate: a requeued job is not claimable until this instant.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# ---------------------------------------------------------------------------
# Functional indexes.
#
# These were previously created only as raw SQL inside migration 0001, which
# left them absent from Base.metadata. Any schema produced by
# ``create_all()`` — used by the integration tests, and by anyone who
# autogenerates against a fresh database — silently had no FTS or vector index,
# so both providers degraded to sequential scans with no error. Declaring them
# here keeps metadata and migrations in sync.
#
# The FTS expression must stay byte-identical to the one in
# app/search/local_index.py: if they diverge the GIN index is still created but
# the planner stops using it.
# ---------------------------------------------------------------------------

DOC_TSVECTOR = func.to_tsvector(
    # Cast required: SQLAlchemy would otherwise bind 'english' as an untyped
    # parameter, which Postgres rejects for regconfig ("No literal value
    # renderer is available ... with datatype REGCONFIG").
    cast(literal_column("'english'"), REGCONFIG),
    func.coalesce(Document.title, "").op("||")(" " + func.coalesce(Document.content_text, "")),
)

Document.__table__.append_constraint(Index("ix_documents_fts", DOC_TSVECTOR, postgresql_using="gin"))

# A second index using the `simple` configuration. `english` applies stemming and
# an English stopword list, which mangles most other languages outright — a
# Bengali or Japanese query returns nothing because its tokens are stemmed away
# or treated as stopwords. `simple` does no stemming and no stopword removal, so
# it works for every language, at the cost of English recall. English is
# therefore tried first and `simple` is the fallback.
#
# Keep this expression byte-identical to the constant used in
# app/search/local_index.py for the same reason as above.
DOC_TSVECTOR_SIMPLE = func.to_tsvector(
    cast(literal_column("'simple'"), REGCONFIG),
    func.coalesce(Document.title, "").op("||")(" " + func.coalesce(Document.content_text, "")),
)

Document.__table__.append_constraint(Index("ix_documents_fts_simple", DOC_TSVECTOR_SIMPLE, postgresql_using="gin"))

# Per-language tsvector, written by the ingest worker.
#
# The two expression indexes above force one regconfig on the whole table, so a
# document cannot be stemmed with the rules of its own language. Postgres cannot
# generate this column either: `to_tsvector(regconfig, text)` is only IMMUTABLE
# for a *literal* config, and here the config varies per row, so it has to be
# computed in the application and stored.
#
# Populated in app/workers/ingest.py via app/search/langdetect.regconfig_for().
# Rows indexed before this column existed have a NULL tsv and are repaired by
# `python -m app.backfill_language`.
Document.__table__.append_constraint(Index("ix_documents_tsv", Document.tsv, postgresql_using="gin"))

# pgvector's HNSW access method takes the opclass as part of the column spec
# (`USING hnsw (embedding vector_cosine_ops)`), not as a WITH parameter, and
# neither SQLAlchemy nor pgvector's Vector type exposes an opclass option. The
# unary operator renders the required syntax; the NULL operand is emitted but
# discarded by Postgres' grammar for an opclass reference.
Chunk.__table__.append_constraint(
    Index(
        "ix_chunks_embedding",
        # Raw text is the only way to express pgvector's opclass reference:
        # `USING hnsw (embedding vector_cosine_ops)`. SQLAlchemy has no opclass
        # option on Index, and pgvector's Vector type does not expose one either.
        text("embedding vector_cosine_ops"),
        postgresql_using="hnsw",
    )
)

# The recrawler scans by (status, fetched_at) and the local provider filters on
# status; without these both are sequential scans on a large corpus.
Index("ix_documents_fetched_at", Document.fetched_at)
Index("ix_documents_status", Document.status)
Index("ix_ingest_jobs_created_at", IngestJob.created_at)
Index("ix_ingest_jobs_next_attempt_at", IngestJob.next_attempt_at)


__all__ = [
    "ApiKey",
    "ApiKeyRole",
    "Document",
    "Chunk",
    "IngestJob",
    "IngestJobStatus",
    "IngestKind",
    "UsageLog",
    "Base",
    "DOC_TSVECTOR",
]
