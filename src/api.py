from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.db.database import async_session, init_db, close_db
from src.db.crud import (
    get_booking,
    list_bookings,
    update_booking_rate,
    update_booking_status,
    upsert_booking,
)
from src.logger import setup_logger

logger = setup_logger(__name__)

BookingStatus = Literal[
    "monitoring",
    "ceiling_truncated",
    "expired_cancellation_passed",
    "rebooked",
]


# ---------------------------------------------------------------------------
# Lifespan: init DB on startup, close pool on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="SignalPipe Travel Price Protection API",
    description="Register and monitor travel bookings for price-drop savings alerts.",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class BookingIn(BaseModel):
    booking_id: str = Field(..., description="Agency's internal reference code")
    client_name: str = Field(..., description="Guest name for actionable alerts")
    provider_url: str = Field(..., description="Booking / hotel / flight itinerary page")
    booked_rate: Decimal = Field(..., description="Baseline cost the agency locked in")
    current_rate: Decimal | None = Field(None, description="Latest scraped price")
    cancellation_deadline: datetime = Field(..., description="Hard safety cutoff (tz-aware)")
    room_or_ticket_class: str = Field(..., description="e.g. 'Deluxe King, Free Breakfast'")
    status: BookingStatus = Field("monitoring", description="Monitoring lifecycle status")
    target_savings_threshold: Decimal = Field(
        Decimal("50.00"),
        description="Minimum dollar drop to trigger a savings alert",
    )


class BookingOut(BaseModel):
    booking_id: str
    client_name: str
    provider_url: str
    booked_rate: Decimal
    current_rate: Decimal | None
    cancellation_deadline: datetime
    room_or_ticket_class: str
    status: str
    target_savings_threshold: Decimal

    class Config:
        from_attributes = True


class RateUpdateIn(BaseModel):
    current_rate: Decimal = Field(..., description="Freshly scraped rate")
    alert_webhook_url: str | None = Field(None, description="Webhook for savings alert delivery")


class StatusUpdateIn(BaseModel):
    status: BookingStatus = Field(..., description="New monitoring lifecycle status")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/bookings", response_model=BookingOut, status_code=201)
async def register_booking(payload: BookingIn):
    """Register or update a booking for price protection monitoring."""
    async with async_session() as session:
        booking = await upsert_booking(
            session=session,
            booking_id=payload.booking_id,
            client_name=payload.client_name,
            provider_url=payload.provider_url,
            booked_rate=payload.booked_rate,
            current_rate=payload.current_rate,
            cancellation_deadline=payload.cancellation_deadline,
            room_or_ticket_class=payload.room_or_ticket_class,
            status=payload.status,
            target_savings_threshold=payload.target_savings_threshold,
        )
    logger.info("Registered booking %s for client %s.", payload.booking_id, payload.client_name)
    return booking


@app.get("/bookings", response_model=list[BookingOut])
async def get_bookings(status: str | None = None):
    """List all bookings, optionally filtered by status."""
    async with async_session() as session:
        bookings = await list_bookings(session=session, status=status)
    return list(bookings)


@app.get("/bookings/{booking_id}", response_model=BookingOut)
async def get_booking_by_id(booking_id: str):
    """Retrieve a single booking by its ID."""
    async with async_session() as session:
        booking = await get_booking(session=session, booking_id=booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return booking


@app.patch("/bookings/{booking_id}/rate", response_model=BookingOut)
async def patch_booking_rate(booking_id: str, payload: RateUpdateIn):
    """Push a freshly scraped rate and trigger a savings alert if the threshold is met."""
    async with async_session() as session:
        booking = await update_booking_rate(
            session=session,
            booking_id=booking_id,
            current_rate=payload.current_rate,
            alert_webhook_url=payload.alert_webhook_url,
        )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return booking


@app.patch("/bookings/{booking_id}/status", response_model=BookingOut)
async def patch_booking_status(booking_id: str, payload: StatusUpdateIn):
    """Update the monitoring lifecycle status of a booking."""
    async with async_session() as session:
        booking = await update_booking_status(
            session=session,
            booking_id=booking_id,
            status=payload.status,
        )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return booking
