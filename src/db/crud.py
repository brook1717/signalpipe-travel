import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ActiveBooking
from src.logger import setup_logger

logger = setup_logger(__name__)


def _push_savings_alert(
    booking_id: str,
    client_name: str,
    provider_url: str,
    booked_rate: Decimal,
    current_rate: Decimal,
    threshold: Decimal,
    webhook_url: str | None = None,
) -> None:
    """Push a savings-opportunity alert to the SQS alert queue.

    Called synchronously (boto3 is blocking) because SQS calls are fast
    and do not benefit from awaiting.
    """
    alert_queue_url = os.environ.get("SQS_ALERT_QUEUE_URL", "")
    if not alert_queue_url:
        logger.warning(
            "SQS_ALERT_QUEUE_URL not set — savings alert not sent for booking %s",
            booking_id,
        )
        return

    import boto3
    from botocore.exceptions import ClientError

    savings = booked_rate - current_rate
    alert_payload = {
        "event": "price_protection_savings",
        "booking_id": booking_id,
        "client_name": client_name,
        "provider_url": provider_url,
        "booked_rate": float(booked_rate),
        "current_rate": float(current_rate),
        "savings_amount": float(round(savings, 2)),
        "savings_pct": float(round(savings / booked_rate * 100, 2)),
        "threshold_triggered": float(threshold),
        "webhook_url": webhook_url,
    }

    try:
        client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        client.send_message(
            QueueUrl=alert_queue_url,
            MessageBody=json.dumps(alert_payload),
        )
        logger.info(
            "[SAVINGS ALERT] booking=%s client=%s booked=$%.2f current=$%.2f "
            "savings=$%.2f (%.2f%%)",
            booking_id, client_name,
            float(booked_rate), float(current_rate),
            float(savings), alert_payload["savings_pct"],
        )
    except ClientError as exc:
        logger.error("Failed to send savings alert for booking %s: %s", booking_id, exc)


async def upsert_booking(
    session: AsyncSession,
    booking_id: str,
    client_name: str,
    provider_url: str,
    booked_rate: Decimal,
    current_rate: Decimal | None,
    cancellation_deadline: datetime,
    room_or_ticket_class: str,
    status: str = "monitoring",
    target_savings_threshold: Decimal = Decimal("50.00"),
    alert_webhook_url: str | None = None,
) -> ActiveBooking:
    """Insert or update a travel booking record using PostgreSQL ON CONFLICT.

    Savings Trigger: if current_rate is provided and
    booked_rate - current_rate >= target_savings_threshold, a savings alert
    is dispatched to SQS before the row is committed.

    Idempotency: re-upserting the same booking_id updates the row in place —
    no duplicates are ever created.
    """
    if current_rate is not None and current_rate < booked_rate:
        savings = booked_rate - current_rate
        if savings >= target_savings_threshold:
            _push_savings_alert(
                booking_id=booking_id,
                client_name=client_name,
                provider_url=provider_url,
                booked_rate=booked_rate,
                current_rate=current_rate,
                threshold=target_savings_threshold,
                webhook_url=alert_webhook_url,
            )

    stmt = pg_insert(ActiveBooking).values(
        booking_id=booking_id,
        client_name=client_name,
        provider_url=provider_url,
        booked_rate=booked_rate,
        current_rate=current_rate,
        cancellation_deadline=cancellation_deadline,
        target_savings_threshold=target_savings_threshold,
        room_or_ticket_class=room_or_ticket_class,
        status=status,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["booking_id"],
        set_={
            "client_name": stmt.excluded.client_name,
            "provider_url": stmt.excluded.provider_url,
            "booked_rate": stmt.excluded.booked_rate,
            "current_rate": stmt.excluded.current_rate,
            "cancellation_deadline": stmt.excluded.cancellation_deadline,
            "target_savings_threshold": stmt.excluded.target_savings_threshold,
            "room_or_ticket_class": stmt.excluded.room_or_ticket_class,
            "status": stmt.excluded.status,
        },
    )

    await session.execute(stmt)
    await session.commit()

    result = await session.execute(
        select(ActiveBooking).where(ActiveBooking.booking_id == booking_id)
    )
    booking = result.scalar_one()
    logger.info(
        "Upserted booking %s (client=%s, status=%s, booked=$%.2f, current=%s)",
        booking_id, client_name, status, float(booked_rate),
        f"${float(current_rate):.2f}" if current_rate is not None else "N/A",
    )
    return booking


async def get_booking(
    session: AsyncSession,
    booking_id: str,
) -> ActiveBooking | None:
    """Fetch a single booking by its primary key. Returns None if not found."""
    result = await session.execute(
        select(ActiveBooking).where(ActiveBooking.booking_id == booking_id)
    )
    return result.scalar_one_or_none()


async def list_bookings(
    session: AsyncSession,
    status: str | None = None,
) -> Sequence[ActiveBooking]:
    """Return all bookings, optionally filtered by status.

    Valid status values: 'monitoring', 'ceiling_truncated',
    'expired_cancellation_passed', 'rebooked'.
    """
    stmt = select(ActiveBooking)
    if status is not None:
        stmt = stmt.where(ActiveBooking.status == status)
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_booking_rate(
    session: AsyncSession,
    booking_id: str,
    current_rate: Decimal,
    alert_webhook_url: str | None = None,
) -> ActiveBooking | None:
    """Update the current_rate for a single booking by booking_id.

    Savings Trigger: fires an alert if
    booked_rate - current_rate >= target_savings_threshold.

    Returns the updated booking, or None if booking_id does not exist.
    """
    booking = await get_booking(session, booking_id)
    if booking is None:
        logger.warning("update_booking_rate: booking %s not found.", booking_id)
        return None

    booked = Decimal(str(booking.booked_rate))
    threshold = Decimal(str(booking.target_savings_threshold))
    if current_rate < booked:
        savings = booked - current_rate
        if savings >= threshold:
            _push_savings_alert(
                booking_id=booking_id,
                client_name=booking.client_name,
                provider_url=booking.provider_url,
                booked_rate=booked,
                current_rate=current_rate,
                threshold=threshold,
                webhook_url=alert_webhook_url,
            )

    await session.execute(
        update(ActiveBooking)
        .where(ActiveBooking.booking_id == booking_id)
        .values(current_rate=current_rate)
    )
    await session.commit()
    await session.refresh(booking)
    logger.info(
        "Rate updated for booking %s: booked=$%.2f → current=$%.2f",
        booking_id, float(booked), float(current_rate),
    )
    return booking


async def update_rate_by_provider_url(
    session: AsyncSession,
    provider_url: str,
    current_rate: Decimal,
    status: str = "monitoring",
    alert_webhook_url: str | None = None,
) -> Sequence[ActiveBooking]:
    """Update current_rate for all active bookings matching a provider URL.

    Used by the scraper pipeline to push freshly-scraped rates back into all
    bookings that share the same provider page.  Only rows whose status is
    'monitoring' or 'ceiling_truncated' are eligible for rate updates.

    Returns the list of updated bookings.
    """
    result = await session.execute(
        select(ActiveBooking)
        .where(ActiveBooking.provider_url == provider_url)
        .where(ActiveBooking.status.in_(["monitoring", "ceiling_truncated"]))
    )
    bookings: Sequence[ActiveBooking] = result.scalars().all()

    if not bookings:
        logger.warning(
            "update_rate_by_provider_url: no active bookings found for %s", provider_url
        )
        return []

    for booking in bookings:
        booked = Decimal(str(booking.booked_rate))
        threshold = Decimal(str(booking.target_savings_threshold))
        if current_rate < booked:
            savings = booked - current_rate
            if savings >= threshold:
                _push_savings_alert(
                    booking_id=booking.booking_id,
                    client_name=booking.client_name,
                    provider_url=booking.provider_url,
                    booked_rate=booked,
                    current_rate=current_rate,
                    threshold=threshold,
                    webhook_url=alert_webhook_url,
                )

        await session.execute(
            update(ActiveBooking)
            .where(ActiveBooking.booking_id == booking.booking_id)
            .values(current_rate=current_rate, status=status)
        )

    await session.commit()
    logger.info(
        "Rate updated to $%.2f for %d booking(s) at %s (status=%s)",
        float(current_rate), len(bookings), provider_url, status,
    )
    return bookings


async def update_booking_status(
    session: AsyncSession,
    booking_id: str,
    status: str,
) -> ActiveBooking | None:
    """Update the status of a booking.

    Returns the updated booking, or None if booking_id does not exist.
    """
    booking = await get_booking(session, booking_id)
    if booking is None:
        logger.warning("update_booking_status: booking %s not found.", booking_id)
        return None

    await session.execute(
        update(ActiveBooking)
        .where(ActiveBooking.booking_id == booking_id)
        .values(status=status)
    )
    await session.commit()
    await session.refresh(booking)
    logger.info("Status updated for booking %s: → %s", booking_id, status)
    return booking
