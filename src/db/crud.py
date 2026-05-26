import hashlib
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ScrapedRecord
from src.logger import setup_logger

logger = setup_logger(__name__)

def _compute_hash(url: str, payload: dict) -> str:
    """Generate a deterministic SHA-256 hash from the URL and payload."""
    raw = json.dumps({"url": url, "payload": payload}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _push_price_drop_alert(
    url: str,
    old_price: float,
    new_price: float,
    webhook_url: str | None = None,
) -> None:
    """Push a price-drop alert message to the SQS alert queue.

    Called synchronously (boto3 is blocking) from within the async upsert
    because the SQS call itself is very fast and does not need awaiting.
    """
    alert_queue_url = os.environ.get("SQS_ALERT_QUEUE_URL", "")
    if not alert_queue_url:
        logger.warning("SQS_ALERT_QUEUE_URL not set — price-drop alert not sent for %s", url)
        return

    import boto3
    from botocore.exceptions import ClientError

    alert_payload = {
        "event": "price_drop",
        "url": url,
        "old_price": old_price,
        "new_price": new_price,
        "drop_amount": round(old_price - new_price, 4),
        "drop_pct": round((old_price - new_price) / old_price * 100, 2),
        "webhook_url": webhook_url,
    }

    try:
        client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        client.send_message(
            QueueUrl=alert_queue_url,
            MessageBody=json.dumps(alert_payload),
        )
        logger.info(
            "[PRICE DROP] %.4f → %.4f (%.2f%%) for %s — alert queued.",
            old_price, new_price, alert_payload["drop_pct"], url,
        )
    except ClientError as exc:
        logger.error("Failed to send price-drop alert for %s: %s", url, exc)


async def upsert_record(
    session: AsyncSession,
    url: str,
    payload: dict,
    status: str = "success",
    price: float | None = None,
    ai_fallback_used: bool = False,
    alert_webhook_url: str | None = None,
) -> ScrapedRecord:
    """Insert or update a scraped record using PostgreSQL ON CONFLICT.

    Delta Trigger: if a previous price exists and the new price is lower,
    a price-drop alert is pushed to the SQS alert queue before committing.

    Idempotency: re-scraping the same URL only updates the row in place —
    no duplicates are ever created.
    """
    now = datetime.now(timezone.utc)
    data_hash = _compute_hash(url, payload)

    # --- Delta check: read existing price before overwriting ---
    existing_result = await session.execute(
        select(ScrapedRecord.price).where(ScrapedRecord.source_url == url)
    )
    existing_price: float | None = existing_result.scalar_one_or_none()

    if (
        price is not None
        and existing_price is not None
        and price < existing_price
    ):
        _push_price_drop_alert(url, existing_price, price, alert_webhook_url)

    # --- Upsert ---
    stmt = pg_insert(ScrapedRecord).values(
        source_url=url,
        data_hash=data_hash,
        payload=payload,
        price=price,
        scraped_at=now,
        status=status,
        ai_fallback_used=ai_fallback_used,
    )

    stmt = stmt.on_conflict_do_update(
        index_elements=["source_url"],
        set_={
            "payload": stmt.excluded.payload,
            "data_hash": stmt.excluded.data_hash,
            "price": stmt.excluded.price,
            "scraped_at": stmt.excluded.scraped_at,
            "status": stmt.excluded.status,
            "ai_fallback_used": stmt.excluded.ai_fallback_used,
        },
    )

    await session.execute(stmt)
    await session.commit()

    result = await session.execute(
        select(ScrapedRecord).where(ScrapedRecord.source_url == url)
    )
    record = result.scalar_one()
    logger.info(
        "Upserted record: %s (hash=%s, price=%s, ai_fallback=%s)",
        url, data_hash[:12], price, ai_fallback_used,
    )
    return record
