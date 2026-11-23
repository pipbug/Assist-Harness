from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .settings import settings

EMBED_DIM = 1536

STARTER_CATEGORIES = [
    ("preferences", "Preferences", 10),
    ("people", "People", 20),
    ("home", "Home", 30),
    ("routines", "Routines", 40),
    ("health", "Health", 50),
    ("projects", "Projects", 60),
    ("other", "Other", 70),
]


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("categories.slug"), nullable=True, index=True
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS memories_embedding_hnsw "
                "ON memories USING hnsw (embedding vector_cosine_ops)"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS memories_tags_gin ON memories USING gin (tags)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS memories_user_category ON memories (user_id, category)")
        )
    with SessionLocal() as session:
        existing = {c.slug for c in session.query(Category).all()}
        for slug, label, order in STARTER_CATEGORIES:
            if slug not in existing:
                session.add(Category(slug=slug, label=label, sort_order=order))
        session.commit()
