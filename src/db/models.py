import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ScrapeJob(Base):
    """Tracks a batch scraping job dispatched via the API."""

    __tablename__ = "scrape_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    total_urls = Column(Integer, nullable=False, default=0)
    completed_urls = Column(Integer, nullable=False, default=0)
    schema_hint = Column(Text, nullable=True)

    records = relationship("ScrapedRecord", back_populates="job", lazy="selectin")

    def __repr__(self) -> str:
        return f"<ScrapeJob(id={self.id}, status='{self.status}', {self.completed_urls}/{self.total_urls})>"


class ScrapedRecord(Base):
    """Stores scraped data with idempotency guarantees."""

    __tablename__ = "scraped_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(UUID(as_uuid=True), ForeignKey("scrape_jobs.id"), nullable=True, index=True)
    source_url = Column(String, nullable=False, unique=True, index=True)
    data_hash = Column(String, nullable=False, unique=True)
    payload = Column(JSONB, nullable=False)
    price = Column(Float, nullable=True)
    scraped_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    status = Column(String, nullable=False, default="success")
    ai_fallback_used = Column(Boolean, nullable=False, default=False)

    job = relationship("ScrapeJob", back_populates="records")

    __table_args__ = (
        Index("ix_scraped_records_data_hash", "data_hash", unique=True),
    )

    def __repr__(self) -> str:
        return f"<ScrapedRecord(id={self.id}, source_url='{self.source_url}', status='{self.status}')>"
