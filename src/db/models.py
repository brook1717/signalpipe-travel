from decimal import Decimal

from sqlalchemy import Column, DateTime, Index, Numeric, String, Text
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
