"""Automated 6-step validation rig — B2B Travel Price Protection Engine.

  0 · DB connectivity & active_bookings / rate_snapshots schema validation
  1 · Booking insertion: single manual input + multi-row batch array
  2 · Cancellation safety cutoff: expired deadline bypass via expire_lapsed_bookings
  3 · Delta engine: alert-trigger vs alert-suppression (threshold + rise guards)
  4 · Gemini parser resilience: HTML→Markdown, mock LLM, validation layer
  5 · Full round-trip: SQS queue pickup → DB state update → webhook dispatch

SQS is mocked in-process with moto — no LocalStack or real AWS needed.
PostgreSQL: requires the local postgres container (docker compose up postgres -d).

Usage:
    python -m src.seed_test_data

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
"""

import asyncio
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

# ── Windows: asyncpg is incompatible with ProactorEventLoop (the default) ────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Fake AWS credentials so boto3 / moto don't complain ──────────────────────
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# ─────────────────────────────────────────────────────────────────────────────
# Console helpers
# ─────────────────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

PASS_TAG = f"{GREEN}{BOLD}  ✓ PASS{RESET}"
FAIL_TAG = f"{RED}{BOLD}  ✗ FAIL{RESET}"
INFO_TAG = f"{CYAN}  · INFO{RESET}"
WARN_TAG = f"{YELLOW}  ⚠ WARN{RESET}"

_results: list[tuple[str, bool]] = []


def section(title: str) -> None:
    bar = "─" * 64
    print(f"\n{CYAN}{BOLD}{bar}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{BOLD}{bar}{RESET}")


def check(label: str, passed: bool, detail: str = "") -> None:
    tag = PASS_TAG if passed else FAIL_TAG
    print(f"{tag}  {label}")
    if detail:
        for line in textwrap.wrap(detail, width=72):
            print(f"           {line}")
    _results.append((label, passed))


def info(msg: str) -> None:
    print(f"{INFO_TAG}  {msg}")


def warn(msg: str) -> None:
    print(f"{WARN_TAG}  {msg}")


def summary() -> None:
    section("TEST SUMMARY")
    total  = len(_results)
    passed = sum(1 for _, ok in _results if ok)
    failed = total - passed
    for label, ok in _results:
        tag = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"  {tag}  {label}")
    print()
    if failed == 0:
        print(f"{GREEN}{BOLD}  All {total} checks passed.{RESET}")
    else:
        print(f"{RED}{BOLD}  {failed}/{total} checks FAILED.{RESET}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────

_FUTURE = datetime(2027, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
_PAST   = datetime(2023, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

ALERT_WEBHOOK = "https://hooks.zapier.com/hooks/catch/signalpipe/travel-alerts/"

# Step 1 — single + batch
SINGLE_BOOKING = {
    "booking_id":               "SINGLE-001",
    "client_name":              "Alice Wanderer",
    "provider_url":             "https://www.marriott.com/hotels/travel/single-001",
    "booked_rate":              Decimal("499.00"),
    "cancellation_deadline":    _FUTURE,
    "room_or_ticket_class":     "Premier Suite, Harbour View",
    "target_savings_threshold": Decimal("50.00"),
}

BATCH_BOOKINGS = [
    {
        "booking_id":               "BATCH-001",
        "client_name":              "Dr. Sarah Chen",
        "provider_url":             "https://www.marriott.com/hotels/travel/batch-001",
        "booked_rate":              Decimal("450.00"),
        "cancellation_deadline":    _FUTURE,
        "room_or_ticket_class":     "Deluxe King, Free Breakfast",
        "target_savings_threshold": Decimal("50.00"),
    },
    {
        "booking_id":               "BATCH-002",
        "client_name":              "James Whitfield",
        "provider_url":             "https://www.expedia.com/hotels/booking/batch-002",
        "booked_rate":              Decimal("280.75"),   # non-round to test Decimal precision
        "cancellation_deadline":    _FUTURE,
        "room_or_ticket_class":     "Standard Double, Breakfast Included",
        "target_savings_threshold": Decimal("30.00"),
    },
    {
        "booking_id":               "BATCH-003",
        "client_name":              "Priya Nair",
        "provider_url":             "https://www.booking.com/hotel/ae/burj-al-arab/batch-003",
        "booked_rate":              Decimal("1250.00"),
        "cancellation_deadline":    _FUTURE,
        "room_or_ticket_class":     "Junior Suite, Sea View",
        "target_savings_threshold": Decimal("100.00"),
    },
]

# Step 2 — expired deadline
EXPIRED_BOOKING = {
    "booking_id":               "EXPIRED-001",
    "client_name":              "Test Expired Client",
    "provider_url":             "https://www.expedia.com/hotels/booking/expired-001",
    "booked_rate":              Decimal("500.00"),
    "cancellation_deadline":    _PAST,              # deliberately in the past
    "room_or_ticket_class":     "Standard Room",
    "target_savings_threshold": Decimal("50.00"),
}

# Step 3 — delta engine cases
ALERT_BOOKING = {
    "booking_id":               "DELTA-ALERT-001",
    "client_name":              "Carlos Rivera",
    "provider_url":             "https://www.hotels.com/delta/alert-001",
    "booked_rate":              Decimal("450.00"),
    "monitored_rate":           Decimal("295.00"),   # saves $155 — clears $50 threshold
    "cancellation_deadline":    _FUTURE,
    "room_or_ticket_class":     "Ocean View Suite",
    "target_savings_threshold": Decimal("50.00"),
}

SILENT_DROP_BOOKING = {
    "booking_id":               "DELTA-SILENT-001",
    "client_name":              "Nina Okonkwo",
    "provider_url":             "https://www.hotels.com/delta/silent-001",
    "booked_rate":              Decimal("620.00"),
    "monitored_rate":           Decimal("595.00"),   # saves $25 — BELOW $50 threshold
    "cancellation_deadline":    _FUTURE,
    "room_or_ticket_class":     "Executive Room",
    "target_savings_threshold": Decimal("50.00"),
}

RISE_BOOKING = {
    "booking_id":               "DELTA-RISE-001",
    "client_name":              "Tom Hartley",
    "provider_url":             "https://www.hotels.com/delta/rise-001",
    "booked_rate":              Decimal("280.00"),
    "monitored_rate":           Decimal("310.00"),   # price RISE — savings are negative
    "cancellation_deadline":    _FUTURE,
    "room_or_ticket_class":     "Standard King",
    "target_savings_threshold": Decimal("50.00"),
}

# Step 4 — Gemini parser
MOCK_TRAVEL_HTML = """
<html>
<head><title>Marriott Deluxe King - Rate Details</title></head>
<body>
<nav>Home | Hotels | My Trips | Sign In</nav>
<header><div id="brand">Marriott Bonvoy</div></header>
<main>
  <h1>Deluxe King Room with Free Breakfast</h1>
  <div class="rate-block">
    <span class="total-price">$685.00</span>
    <span class="tax-note">Includes all taxes and resort fees</span>
  </div>
  <div class="inventory-warning">Only 2 rooms left at this rate!</div>
  <p>Currency: USD | 3 nights | Check-in Jul 15, 2026</p>
</main>
<footer>© 2026 Marriott International. All rights reserved.</footer>
<script>gtag('event', 'view_item', {price: 685.00});</script>
<style>.price { color: #c00; font-weight: bold; }</style>
</body>
</html>
"""

# Step 5 — round-trip
ROUNDTRIP_BOOKING = {
    "booking_id":               "RT-001",
    "client_name":              "Elena Marchetti",
    "provider_url":             "https://www.hilton.com/roundtrip/rt-001",
    "booked_rate":              Decimal("800.00"),
    "monitored_rate":           Decimal("650.00"),   # saves $150 — clears $50 threshold
    "cancellation_deadline":    _FUTURE,
    "room_or_ticket_class":     "Deluxe Double, City View",
    "target_savings_threshold": Decimal("50.00"),
}


# All booking IDs written by this rig — used for cleanup at start of each run
_TEST_BOOKING_IDS = [
    "SINGLE-001",
    "BATCH-001", "BATCH-002", "BATCH-003",
    "EXPIRED-001",
    "DELTA-ALERT-001", "DELTA-SILENT-001", "DELTA-RISE-001",
    "RT-001",
]


async def _cleanup_test_data() -> None:
    """Delete all rows written by previous runs of this rig.

    Ensures every run starts with a clean slate so RateSnapshot's
    append-only log doesn't accumulate stale rows that break
    scalar_one_or_none() assertions.
    """
    from src.db.database import async_session
    from src.db.models import ActiveBooking, RateSnapshot
    from sqlalchemy import delete

    async with async_session() as session:
        await session.execute(
            delete(RateSnapshot).where(RateSnapshot.booking_id.in_(_TEST_BOOKING_IDS))
        )
        await session.execute(
            delete(ActiveBooking).where(ActiveBooking.booking_id.in_(_TEST_BOOKING_IDS))
        )
        await session.commit()
    info("Test fixtures purged — starting clean run.")


# ─────────────────────────────────────────────────────────────────────────────
# Engine lifecycle helper
# ─────────────────────────────────────────────────────────────────────────────

def _dispose_engine() -> None:
    """Dispose the asyncpg connection pool between asyncio.run() calls.

    Required on Windows to prevent 'Future attached to a different loop' errors
    when a second asyncio.run() creates a new event loop.
    """
    try:
        from src.db.database import engine as _engine
        _engine.sync_engine.dispose()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# STEP 0 — DB Connectivity & Schema Validation
# ─────────────────────────────────────────────────────────────────────────────

async def step_0_db_connectivity() -> bool:
    section("STEP 0 · DB Connectivity & Schema Validation")
    try:
        from src.db.database import init_db, engine
        from sqlalchemy import inspect

        await init_db()
        info("init_db() completed — DDL applied via SQLAlchemy metadata.create_all.")
        check("PostgreSQL connection established", True)

        async with engine.connect() as conn:
            table_names: list[str] = await conn.run_sync(
                lambda sc: inspect(sc).get_table_names()
            )

        has_bookings  = "active_bookings" in table_names
        has_snapshots = "rate_snapshots"  in table_names
        check("Table 'active_bookings' exists",  has_bookings)
        check("Table 'rate_snapshots' exists",   has_snapshots,
              f"tables present: {table_names}")

        if has_bookings:
            REQUIRED_COLS = {
                "booking_id", "client_name", "provider_url", "booked_rate",
                "current_rate", "cancellation_deadline", "room_or_ticket_class",
                "status", "target_savings_threshold",
            }
            async with engine.connect() as conn:
                col_names: set[str] = set(
                    c["name"] for c in await conn.run_sync(
                        lambda sc: inspect(sc).get_columns("active_bookings")
                    )
                )
            missing = REQUIRED_COLS - col_names
            check(
                "active_bookings has all required columns",
                len(missing) == 0,
                f"missing: {missing}" if missing else "all columns present",
            )

        return has_bookings

    except Exception as exc:
        check("PostgreSQL connection established", False, str(exc))
        warn("Is the postgres container running?  docker compose up postgres -d")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Booking Insertion: Single Manual Input + Batch Array
# ─────────────────────────────────────────────────────────────────────────────

async def step_1_insertion() -> None:
    section("STEP 1 · Booking Insertion — Single Manual Input + Batch Array")
    from src.db.database import async_session
    from src.db.crud import upsert_booking
    from src.db.models import ActiveBooking
    from sqlalchemy import select, func

    # ── A. Single manual insert ───────────────────────────────────────────────
    info("Inserting single booking SINGLE-001…")
    bk = SINGLE_BOOKING
    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=bk["booking_id"],
            client_name=bk["client_name"],
            provider_url=bk["provider_url"],
            booked_rate=bk["booked_rate"],
            current_rate=None,
            cancellation_deadline=bk["cancellation_deadline"],
            room_or_ticket_class=bk["room_or_ticket_class"],
            target_savings_threshold=bk["target_savings_threshold"],
        )

    async with async_session() as session:
        result = await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id == "SINGLE-001")
        )
        row = result.scalar_one_or_none()

    check("Single booking SINGLE-001 inserted and retrieved", row is not None)
    if row:
        check(
            "Single booking booked_rate stored correctly ($499.00)",
            row.booked_rate == bk["booked_rate"],
            f"stored={row.booked_rate}",
        )
        check(
            "Single booking status defaults to 'monitoring'",
            row.status == "monitoring",
            f"stored status={row.status}",
        )

    # ── B. Batch insert ───────────────────────────────────────────────────────
    info(f"Inserting batch of {len(BATCH_BOOKINGS)} bookings…")
    async with async_session() as session:
        for bk in BATCH_BOOKINGS:
            await upsert_booking(
                session=session,
                booking_id=bk["booking_id"],
                client_name=bk["client_name"],
                provider_url=bk["provider_url"],
                booked_rate=bk["booked_rate"],
                current_rate=None,
                cancellation_deadline=bk["cancellation_deadline"],
                room_or_ticket_class=bk["room_or_ticket_class"],
                target_savings_threshold=bk["target_savings_threshold"],
            )

    batch_ids = [bk["booking_id"] for bk in BATCH_BOOKINGS]
    async with async_session() as session:
        result = await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id.in_(batch_ids))
        )
        found = {r.booking_id: r for r in result.scalars().all()}

    check(
        f"All {len(BATCH_BOOKINGS)} batch bookings inserted",
        len(found) == len(BATCH_BOOKINGS),
        f"found={list(found.keys())}",
    )

    b002 = found.get("BATCH-002")
    check(
        "Decimal precision preserved for BATCH-002 ($280.75)",
        b002 is not None and b002.booked_rate == Decimal("280.75"),
        f"stored={b002.booked_rate if b002 else 'NOT FOUND'}",
    )

    # ── C. Idempotency: re-upsert BATCH-001, row count must not increase ─────
    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=BATCH_BOOKINGS[0]["booking_id"],
            client_name=BATCH_BOOKINGS[0]["client_name"],
            provider_url=BATCH_BOOKINGS[0]["provider_url"],
            booked_rate=BATCH_BOOKINGS[0]["booked_rate"],
            current_rate=None,
            cancellation_deadline=BATCH_BOOKINGS[0]["cancellation_deadline"],
            room_or_ticket_class=BATCH_BOOKINGS[0]["room_or_ticket_class"],
        )

    scope_ids = batch_ids + ["SINGLE-001"]
    async with async_session() as session:
        total = await session.scalar(
            select(func.count()).select_from(ActiveBooking)
            .where(ActiveBooking.booking_id.in_(scope_ids))
        )

    expected_total = len(BATCH_BOOKINGS) + 1   # 3 batch + 1 single
    check(
        "Re-upsert of BATCH-001 created no duplicate row",
        total == expected_total,
        f"expected={expected_total}, actual={total}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Cancellation Safety Cutoff (Expired Deadline Bypass)
# ─────────────────────────────────────────────────────────────────────────────

async def step_2_expired_deadline() -> None:
    section("STEP 2 · Cancellation Safety Cutoff — Expired Deadline Bypass")
    from src.db.database import async_session
    from src.db.crud import upsert_booking, expire_lapsed_bookings, update_rate_by_provider_url
    from src.db.models import ActiveBooking
    from sqlalchemy import select

    bk = EXPIRED_BOOKING
    info(f"Inserting EXPIRED-001 with past deadline: {_PAST.isoformat()}")

    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=bk["booking_id"],
            client_name=bk["client_name"],
            provider_url=bk["provider_url"],
            booked_rate=bk["booked_rate"],
            current_rate=None,
            cancellation_deadline=bk["cancellation_deadline"],
            room_or_ticket_class=bk["room_or_ticket_class"],
            target_savings_threshold=bk["target_savings_threshold"],
        )

    async with async_session() as session:
        result = await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id == "EXPIRED-001")
        )
        row_before = result.scalar_one_or_none()

    check(
        "EXPIRED-001 initially registered with status 'monitoring'",
        row_before is not None and row_before.status == "monitoring",
        f"status={row_before.status if row_before else 'NOT FOUND'}",
    )

    # Invoke the deadline guard — should flip status and return 0 active bookings
    async with async_session() as session:
        active_remaining = await expire_lapsed_bookings(
            session, provider_url=bk["provider_url"]
        )

    check(
        "expire_lapsed_bookings returns 0 active bookings for expired URL",
        active_remaining == 0,
        f"active_remaining={active_remaining}",
    )

    async with async_session() as session:
        result = await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id == "EXPIRED-001")
        )
        row_after = result.scalar_one()

    check(
        "EXPIRED-001 status transitioned to 'expired_cancellation_passed'",
        row_after.status == "expired_cancellation_passed",
        f"stored status={row_after.status}",
    )

    # Confirm the scraper pipeline also skips the expired URL (returns empty list)
    async with async_session() as session:
        updated = await update_rate_by_provider_url(
            session=session,
            provider_url=bk["provider_url"],
            current_rate=Decimal("450.00"),   # big drop, but booking is expired
        )

    check(
        "Rate update skips expired booking — update_rate_by_provider_url returns []",
        len(updated) == 0,
        f"bookings updated: {len(updated)} (expected 0)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Delta Engine: Alert Trigger & Suppression  [moto SQS]
# ─────────────────────────────────────────────────────────────────────────────

async def step_3_delta_engine(alert_queue_url: str, sqs_client) -> None:
    section("STEP 3 · Delta Engine — Alert Trigger & Suppression")
    from src.db.database import async_session
    from src.db.crud import upsert_booking
    from src.db.models import RateSnapshot
    from sqlalchemy import select

    os.environ["SQS_ALERT_QUEUE_URL"] = alert_queue_url

    # ── A. Alert fires: $450 → $295 = $155 savings ≥ $50 threshold ──────────
    info("Sub-step A: $155 price drop — threshold CLEARED (DELTA-ALERT-001)")
    sqs_client.purge_queue(QueueUrl=alert_queue_url)
    bk = ALERT_BOOKING

    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=bk["booking_id"],
            client_name=bk["client_name"],
            provider_url=bk["provider_url"],
            booked_rate=bk["booked_rate"],
            current_rate=bk["monitored_rate"],
            cancellation_deadline=bk["cancellation_deadline"],
            room_or_ticket_class=bk["room_or_ticket_class"],
            target_savings_threshold=bk["target_savings_threshold"],
            alert_webhook_url=ALERT_WEBHOOK,
        )

    resp = sqs_client.receive_message(
        QueueUrl=alert_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    )
    alerts = resp.get("Messages", [])

    check(
        "Exactly 1 SQS alert enqueued after threshold-clearing drop",
        len(alerts) == 1,
        f"messages in queue: {len(alerts)}",
    )

    if alerts:
        body = json.loads(alerts[0]["Body"])
        check(
            "Alert event is 'price_protection_savings'",
            body.get("event") == "price_protection_savings",
            f"event={body.get('event')}",
        )
        check(
            "savings_amount ($155.00) ≥ threshold ($50.00)",
            body.get("savings_amount", 0) >= 50.0,
            f"savings_amount={body.get('savings_amount')}",
        )
        msg = body.get("message", "")
        check("Alert payload contains 'message' field", isinstance(msg, str) and len(msg) > 0)
        if msg:
            check(
                "Markdown message opens with 🚨 header",
                msg.startswith("🚨"),
                f"first 60 chars: {msg[:60]!r}",
            )
            check(
                "Markdown message contains booking ref DELTA-ALERT-001",
                "DELTA-ALERT-001" in msg,
            )
            check(
                "Markdown message contains 'Save $155.00'",
                "Save $155.00" in msg,
                f"savings line: {msg[150:300]!r}",
            )
        info(f"  Markdown alert preview:\n{msg[:420]}")

    async with async_session() as session:
        snap = (await session.execute(
            select(RateSnapshot)
            .where(RateSnapshot.booking_id == bk["booking_id"])
            .order_by(RateSnapshot.id.desc())
            .limit(1)
        )).scalar_one_or_none()

    check("rate_snapshots row created for DELTA-ALERT-001", snap is not None)
    if snap:
        check(
            "RateSnapshot.threshold_met=True",
            snap.threshold_met is True,
            f"threshold_met={snap.threshold_met}",
        )
        check(
            "RateSnapshot.alert_triggered=True",
            snap.alert_triggered is True,
            f"alert_triggered={snap.alert_triggered}",
        )

    # ── B. Sub-threshold drop suppressed: $620 → $595 = $25 < $50 ──────────
    info("Sub-step B: $25 price drop — threshold NOT cleared (DELTA-SILENT-001)")
    sqs_client.purge_queue(QueueUrl=alert_queue_url)
    bk = SILENT_DROP_BOOKING

    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=bk["booking_id"],
            client_name=bk["client_name"],
            provider_url=bk["provider_url"],
            booked_rate=bk["booked_rate"],
            current_rate=bk["monitored_rate"],
            cancellation_deadline=bk["cancellation_deadline"],
            room_or_ticket_class=bk["room_or_ticket_class"],
            target_savings_threshold=bk["target_savings_threshold"],
            alert_webhook_url=ALERT_WEBHOOK,
        )

    alerts_b = sqs_client.receive_message(
        QueueUrl=alert_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    ).get("Messages", [])
    check(
        "Queue empty after sub-threshold drop ($25 < $50)",
        len(alerts_b) == 0,
        f"messages: {len(alerts_b)} (expected 0)",
    )

    async with async_session() as session:
        snap_b = (await session.execute(
            select(RateSnapshot)
            .where(RateSnapshot.booking_id == bk["booking_id"])
            .order_by(RateSnapshot.id.desc())
            .limit(1)
        )).scalar_one_or_none()
    if snap_b:
        check(
            "RateSnapshot.threshold_met=False for sub-threshold drop",
            snap_b.threshold_met is False,
            f"threshold_met={snap_b.threshold_met}",
        )
        check(
            "RateSnapshot.alert_triggered=False for sub-threshold drop",
            snap_b.alert_triggered is False,
            f"alert_triggered={snap_b.alert_triggered}",
        )

    # ── C. Price rise suppressed: $280 → $310 ───────────────────────────────
    info("Sub-step C: price RISE — alert must be suppressed (DELTA-RISE-001)")
    sqs_client.purge_queue(QueueUrl=alert_queue_url)
    bk = RISE_BOOKING

    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=bk["booking_id"],
            client_name=bk["client_name"],
            provider_url=bk["provider_url"],
            booked_rate=bk["booked_rate"],
            current_rate=bk["monitored_rate"],
            cancellation_deadline=bk["cancellation_deadline"],
            room_or_ticket_class=bk["room_or_ticket_class"],
            target_savings_threshold=bk["target_savings_threshold"],
            alert_webhook_url=ALERT_WEBHOOK,
        )

    alerts_c = sqs_client.receive_message(
        QueueUrl=alert_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    ).get("Messages", [])
    check(
        "Queue empty after price RISE ($280 → $310)",
        len(alerts_c) == 0,
        f"messages: {len(alerts_c)} (expected 0)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Gemini Parser Resilience
# ─────────────────────────────────────────────────────────────────────────────

def step_4_gemini_parser() -> None:
    section("STEP 4 · Gemini Parser Resilience")

    try:
        from src.ai_parser import (
            TravelBookingExtraction,
            _html_to_markdown,
            _validate_extractions,
            extract_with_llm,
        )
    except ImportError as exc:
        warn(f"Cannot import src.ai_parser ({exc}). "
             "Install: pip install instructor google-genai markdownify")
        check("ai_parser import succeeded", False, str(exc))
        return

    # ── A. HTML → Markdown conversion ────────────────────────────────────────
    info("Sub-step A: HTML → Markdown stripping")
    markdown = _html_to_markdown(MOCK_TRAVEL_HTML)

    check(
        "HTML→Markdown output is a non-empty string",
        isinstance(markdown, str) and len(markdown) > 0,
        f"output length: {len(markdown)} chars",
    )
    check(
        "nav / footer / script tags stripped from Markdown",
        "gtag" not in markdown and "All rights reserved" not in markdown,
        f"first 200 chars: {markdown[:200]!r}",
    )
    check(
        "Price text ($685) preserved in Markdown output",
        "685" in markdown,
        f"snippet: {markdown[:300]!r}",
    )

    # ── B. No API key guard ───────────────────────────────────────────────────
    info("Sub-step B: GEMINI_API_KEY empty → graceful empty-list return")
    with patch("src.ai_parser.GEMINI_API_KEY", ""):
        try:
            result_no_key = extract_with_llm(MOCK_TRAVEL_HTML, "Deluxe King")
            check(
                "extract_with_llm returns [] when GEMINI_API_KEY is empty",
                result_no_key == [],
                f"returned: {result_no_key}",
            )
        except Exception as exc:
            check(
                "extract_with_llm returns [] when GEMINI_API_KEY is empty",
                False,
                f"raised unexpected exception: {exc}",
            )

    # ── C. Mocked LLM extraction ──────────────────────────────────────────────
    info("Sub-step C: mocked Gemini extraction via instructor patch")
    mock_extraction = TravelBookingExtraction(
        total_price=685.00,
        currency="USD",
        is_exact_match=True,
        inventory_status="limited",
    )

    try:
        with (
            patch("src.ai_parser.GEMINI_API_KEY", "mock-api-key-for-test"),
            patch("src.ai_parser.genai.Client"),
            patch("src.ai_parser.instructor.from_genai") as mock_from_genai,
            patch("src.ai_parser.instructor.Mode"),
        ):
            mock_client = MagicMock()
            mock_from_genai.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_extraction

            results = extract_with_llm(MOCK_TRAVEL_HTML, "Deluxe King, Free Breakfast")

        check(
            "Mocked extract_with_llm returns exactly 1 result",
            len(results) == 1,
            f"results count: {len(results)}",
        )
        if results:
            r = results[0]
            check(
                "Extracted total_price is $685.00",
                r.total_price == 685.00,
                f"total_price={r.total_price}",
            )
            check(
                "Extracted currency is valid 3-letter code 'USD'",
                r.currency == "USD" and len(r.currency) == 3,
                f"currency={r.currency}",
            )
            check(
                "Extracted inventory_status is valid enum value 'limited'",
                r.inventory_status == "limited",
                f"inventory_status={r.inventory_status}",
            )
    except Exception as exc:
        check("Mocked extract_with_llm returns exactly 1 result", False, str(exc))

    # ── D. Validation layer ───────────────────────────────────────────────────
    info("Sub-step D: _validate_extractions — valid passes, invalid rejected")

    valid_obj = TravelBookingExtraction(
        total_price=320.50, currency="EUR",
        is_exact_match=False, inventory_status="available",
    )
    passed = _validate_extractions([valid_obj])
    check(
        "_validate_extractions accepts a structurally valid extraction",
        len(passed) == 1,
        f"passed count: {len(passed)}",
    )

    try:
        TravelBookingExtraction(
            total_price=-99.00,             # triggers _validate_price validator
            currency="GBP",
            is_exact_match=True,
            inventory_status="available",
        )
        check("TravelBookingExtraction rejects negative total_price", False,
              "ValidationError was NOT raised — field_validator missing")
    except Exception:
        check("TravelBookingExtraction rejects negative total_price", True)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Full Round-Trip: SQS Pickup → DB State Update → Webhook Dispatch
# ─────────────────────────────────────────────────────────────────────────────

async def step_5_round_trip(alert_queue_url: str, sqs_client) -> None:
    section("STEP 5 · Full Round-Trip — Queue Pickup → DB Update → Webhook Dispatch")
    from src.db.database import async_session
    from src.db.crud import upsert_booking, update_booking_status
    from src.db.models import ActiveBooking
    from src.delivery import WebhookDeliverer
    from sqlalchemy import select

    os.environ["SQS_ALERT_QUEUE_URL"] = alert_queue_url
    sqs_client.purge_queue(QueueUrl=alert_queue_url)

    bk = ROUNDTRIP_BOOKING
    info(f"Registering RT-001: booked=${bk['booked_rate']} → current=${bk['monitored_rate']}")

    # 1. Register booking and immediately push a qualifying price drop
    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=bk["booking_id"],
            client_name=bk["client_name"],
            provider_url=bk["provider_url"],
            booked_rate=bk["booked_rate"],
            current_rate=bk["monitored_rate"],
            cancellation_deadline=bk["cancellation_deadline"],
            room_or_ticket_class=bk["room_or_ticket_class"],
            target_savings_threshold=bk["target_savings_threshold"],
            alert_webhook_url=ALERT_WEBHOOK,
        )

    # 2. Poll the alert queue
    resp = sqs_client.receive_message(
        QueueUrl=alert_queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0
    )
    messages = resp.get("Messages", [])
    check(
        "RT-001 alert enqueued to SQS and polled (1 message)",
        len(messages) == 1,
        f"queue depth: {len(messages)}",
    )

    if not messages:
        check("Round-trip aborted — no SQS message available", False)
        return

    body            = json.loads(messages[0]["Body"])
    receipt_handle  = messages[0]["ReceiptHandle"]

    # 3. Verify the Markdown message block structure
    msg = body.get("message", "")
    check("Polled SQS payload contains 'message' field",
          isinstance(msg, str) and len(msg) > 0)
    check(
        "Message starts with 🚨 PRICE PROTECTION ALERT header",
        msg.startswith("🚨 *PRICE PROTECTION ALERT"),
        f"first 60 chars: {msg[:60]!r}",
    )
    check(
        "Message contains Booking Ref RT-001",
        "RT-001" in msg,
        f"first 200 chars: {msg[:200]!r}",
    )
    check(
        "Message contains 'Save $150.00'",
        "Save $150.00" in msg,
        f"savings section: {msg[150:350]!r}",
    )
    check(
        "Message contains provider URL",
        bk["provider_url"] in msg,
        f"url: {bk['provider_url']}",
    )
    info(f"  Markdown message preview:\n{msg}")

    # 4. Dispatch webhook with mocked requests.post
    info("Dispatching via WebhookDeliverer (requests.post mocked)…")
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        deliverer = WebhookDeliverer(webhook_url=ALERT_WEBHOOK)
        delivered = deliverer.deliver(body)

    check(
        "requests.post called exactly once for webhook dispatch",
        mock_post.call_count == 1,
        f"call_count={mock_post.call_count}",
    )
    check(
        "WebhookDeliverer.deliver() returned True (success)",
        delivered is True,
        f"returned: {delivered}",
    )

    # 5. Update booking status to 'rebooked' (agent acted on the alert)
    async with async_session() as session:
        await update_booking_status(
            session=session,
            booking_id=bk["booking_id"],
            status="rebooked",
        )

    # 6. Verify final DB state
    async with async_session() as session:
        final_row = (await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id == bk["booking_id"])
        )).scalar_one()

    check(
        "Final DB status for RT-001 is 'rebooked'",
        final_row.status == "rebooked",
        f"stored status={final_row.status}",
    )
    check(
        "Final DB current_rate for RT-001 is $650.00",
        final_row.current_rate == bk["monitored_rate"],
        f"current_rate={final_row.current_rate}",
    )

    # 7. Acknowledge the SQS message and confirm queue is drained
    sqs_client.delete_message(QueueUrl=alert_queue_url, ReceiptHandle=receipt_handle)
    empty = sqs_client.receive_message(
        QueueUrl=alert_queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0
    ).get("Messages", [])
    check(
        "SQS queue empty after message acknowledgement",
        len(empty) == 0,
        f"remaining messages: {len(empty)}",
    )
    info("Round-trip complete ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def _run_db_only_steps() -> bool:
    await _cleanup_test_data()
    ok = await step_0_db_connectivity()
    if not ok:
        warn("Skipping Steps 1 & 2 — fix the DB connection first.")
        return False
    await step_1_insertion()
    await step_2_expired_deadline()
    return True


def _run_sqs_steps(alert_queue_url: str, sqs_client) -> None:
    asyncio.run(step_3_delta_engine(alert_queue_url, sqs_client))
    _dispose_engine()
    asyncio.run(step_5_round_trip(alert_queue_url, sqs_client))
    _dispose_engine()


def main() -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 64}{RESET}")
    print(f"{BOLD}{CYAN}  SignalPipe · B2B Travel Price Protection — Validation Rig{RESET}")
    print(f"{CYAN}  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 64}{RESET}")

    # ── Steps 0, 1, 2 — DB only ───────────────────────────────────────────────
    db_ok = asyncio.run(_run_db_only_steps())
    _dispose_engine()

    # ── Step 4 — fully mocked Gemini; runs regardless of DB availability ──────
    step_4_gemini_parser()

    # ── Steps 3, 5 — DB + moto SQS ───────────────────────────────────────────
    if not db_ok:
        warn("Skipping Steps 3 & 5 — DB was unavailable.")
    else:
        try:
            from moto import mock_aws
        except ImportError:
            warn("moto not installed — run:  pip install 'moto[sqs]'")
            warn("Skipping Steps 3 & 5.")
        else:
            import boto3
            section("SQS STEPS  (moto in-process mock — no real AWS needed)")
            info("Initialising mock_aws context…")
            with mock_aws():
                client    = boto3.client("sqs", region_name="us-east-1")
                alert_q   = client.create_queue(QueueName="price-alerts")["QueueUrl"]
                info(f"[moto] Alert queue URL: {alert_q}")
                _run_sqs_steps(alert_q, client)

    summary()
    sys.exit(0 if sum(1 for _, ok in _results if not ok) == 0 else 1)


if __name__ == "__main__":
    main()
