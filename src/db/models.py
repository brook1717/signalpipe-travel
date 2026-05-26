from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ActiveBooking(Base):
    """Tracks an agency's active travel booking for price protection monitoring."""

    __tablename__ = "active_bookings"

    booking_id = Column(String, primary_key=True)
    client_name = Column(String, nullable=False)
    provider_url = Column(Text, nullable=False)
    booked_rate = Column(Numeric(12, 4), nullable=False)
    current_rate = Column(Numeric(12, 4), nullable=True)
    cancellation_deadline = Column(DateTime(timezone=True), nullable=False)
    target_savings_threshold = Column(
        Numeric(12, 2), nullable=False, default=Decimal("50.00")
    )
    room_or_ticket_class = Column(String, nullable=False)
    status = Column(String, nullable=False, default="monitoring")

    __table_args__ = (
        Index("ix_active_bookings_provider_url", "provider_url"),
        Index("ix_active_bookings_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ActiveBooking(booking_id={self.booking_id!r}, "
            f"client={self.client_name!r}, "
            f"status={self.status!r}, "
            f"booked=${self.booked_rate}, current=${self.current_rate})>"
        )


class RateSnapshot(Base):
    """Append-only log of every rate observation for historical trend analysis.

    Written on every scrape cycle regardless of alert outcome so the complete
    price trend is preserved.  alert_triggered distinguishes actionable savings
    drops from silent data points (price rise, sub-threshold drop, wrong status).
    """

    __tablename__ = "rate_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String, nullable=False)
    provider_url = Column(Text, nullable=False)
    observed_rate = Column(Numeric(12, 4), nullable=False)
    booked_rate = Column(Numeric(12, 4), nullable=False)
    savings = Column(Numeric(12, 4), nullable=False)
    threshold_met = Column(Boolean, nullable=False, default=False)
    alert_triggered = Column(Boolean, nullable=False, default=False)
    observed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_rate_snapshots_booking_id", "booking_id"),
        Index("ix_rate_snapshots_observed_at", "observed_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<RateSnapshot(booking_id={self.booking_id!r}, "
            f"observed=${self.observed_rate}, savings=${self.savings}, "
            f"alert={self.alert_triggered})>"
        )
