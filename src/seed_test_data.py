"""Local pipeline validation script — B2B Travel Price Protection Engine.

Tests every layer of the stack without needing real AWS credentials:
  0. DB connectivity & active_bookings table creation
  1. Booking registration (upsert_booking INSERT path)
  2. Idempotent re-upsert (same booking_id → no duplicate row)
  3. Savings alert trigger (rate drop >= threshold → SQS message dispatched)
  4. Below-threshold guard (small drop < threshold → no alert)
  5. No-alert guard (price INCREASE must NOT dispatch an alert)
  6. Status transition (mark a booking as 'rebooked')
  7. Direct SQS task message round-trip (send → poll → delete)
  8. DLQ inspection helpers (get_dlq_count, get_dlq_messages)

SQS is mocked in-process with moto — no LocalStack or real AWS needed.

Usage:
    python -m src.seed_test_data
"""

import asyncio
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from decimal import Decimal

# ── Windows: asyncpg is incompatible with ProactorEventLoop (the default) ────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Fake AWS creds so boto3 / moto don't complain ────────────────────────────
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
    bar = "─" * 60
    print(f"\n{CYAN}{BOLD}{bar}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{BOLD}{bar}{RESET}")


def check(label: str, passed: bool, detail: str = "") -> None:
    tag = PASS_TAG if passed else FAIL_TAG
    print(f"{tag}  {label}")
    if detail:
        for line in textwrap.wrap(detail, width=70):
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
# Travel booking fixtures
# ─────────────────────────────────────────────────────────────────────────────

MOCK_BOOKINGS = [
    {
        "booking_id": "OAT-2026-001",
        "client_name": "Dr. Sarah Chen",
        "provider_url": "https://www.marriott.com/reservation/ratelab/OAT2026001",
        "booked_rate": Decimal("450.00"),
        "monitored_rate": Decimal("295.00"),   # DROP $155 > $50 threshold → alert expected
        "cancellation_deadline": datetime(2026, 7, 15, 23, 59, tzinfo=timezone.utc),
        "room_or_ticket_class": "Deluxe King, Free Breakfast",
        "target_savings_threshold": Decimal("50.00"),
    },
    {
        "booking_id": "OAT-2026-002",
        "client_name": "James Whitfield",
        "provider_url": "https://www.expedia.com/hotels/booking/detail/OAT2026002",
        "booked_rate": Decimal("280.00"),
        "monitored_rate": Decimal("310.00"),   # RISE → no alert expected
        "cancellation_deadline": datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        "room_or_ticket_class": "Standard Double, Breakfast Included",
        "target_savings_threshold": Decimal("50.00"),
    },
    {
        "booking_id": "OAT-2026-003",
        "client_name": "Priya Nair",
        "provider_url": "https://www.booking.com/hotel/ae/burj-al-arab/OAT2026003",
        "booked_rate": Decimal("620.00"),
        "monitored_rate": Decimal("595.00"),   # DROP $25 < $50 threshold → no alert expected
        "cancellation_deadline": datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
        "room_or_ticket_class": "Junior Suite, Sea View",
        "target_savings_threshold": Decimal("50.00"),
    },
]

ALERT_WEBHOOK = "https://hooks.zapier.com/hooks/catch/signalpipe/travel-alerts/"


# ─────────────────────────────────────────────────────────────────────────────
# Step 0 — Database init
# ─────────────────────────────────────────────────────────────────────────────

async def step_0_init_db() -> bool:
    section("STEP 0 · Database Connectivity & Table Creation")
    try:
        from src.db.database import init_db
        await init_db()
        info("active_bookings table created / verified via SQLAlchemy metadata.create_all.")
        check("PostgreSQL connection", True)
        return True
    except Exception as exc:
        check("PostgreSQL connection", False, str(exc))
        warn("Is the postgres container running?  docker compose up postgres -d")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Register bookings (first upsert = INSERT)
# ─────────────────────────────────────────────────────────────────────────────

async def step_1_register_bookings() -> None:
    section("STEP 1 · Register Bookings (INSERT path)")
    from src.db.database import async_session
    from src.db.crud import upsert_booking
    from sqlalchemy import select
    from src.db.models import ActiveBooking

    async with async_session() as session:
        for bk in MOCK_BOOKINGS:
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
            info(
                f"Registered  {bk['booking_id']}  "
                f"client={bk['client_name']}  "
                f"booked=${bk['booked_rate']:.2f}"
            )

    async with async_session() as session:
        for bk in MOCK_BOOKINGS:
            result = await session.execute(
                select(ActiveBooking).where(ActiveBooking.booking_id == bk["booking_id"])
            )
            row = result.scalar_one_or_none()
            check(
                f"Booking registered: {bk['booking_id']}",
                row is not None and row.booked_rate == bk["booked_rate"],
                f"stored booked_rate={row.booked_rate if row else 'NOT FOUND'}",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Idempotency check (re-upsert same booking_id → no duplicate)
# ─────────────────────────────────────────────────────────────────────────────

async def step_2_idempotency() -> None:
    section("STEP 2 · Idempotent Upsert (re-register same booking_id)")
    from src.db.database import async_session
    from src.db.crud import upsert_booking
    from sqlalchemy import func, select
    from src.db.models import ActiveBooking

    target = MOCK_BOOKINGS[0]

    async with async_session() as session:
        await upsert_booking(
            session=session,
            booking_id=target["booking_id"],
            client_name=target["client_name"],
            provider_url=target["provider_url"],
            booked_rate=target["booked_rate"],
            current_rate=None,
            cancellation_deadline=target["cancellation_deadline"],
            room_or_ticket_class=target["room_or_ticket_class"],
            target_savings_threshold=target["target_savings_threshold"],
        )

    async with async_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(ActiveBooking)
        )
    info(f"Total rows in active_bookings: {count}")
    check(
        "No duplicate row created on re-upsert",
        count == len(MOCK_BOOKINGS),
        f"expected={len(MOCK_BOOKINGS)}, actual={count}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Savings alert trigger & guard (with mocked SQS alert queue)
# ─────────────────────────────────────────────────────────────────────────────

async def step_3_savings_alert_trigger(
    alert_queue_url: str,
    sqs_client,
) -> None:
    section("STEP 3 · Savings Alert Trigger & Guards")

    from src.db.database import async_session
    from src.db.crud import upsert_booking
    from sqlalchemy import select
    from src.db.models import ActiveBooking

    # OAT-2026-001: $450 → $295 = $155 savings (> $50) → alert expected
    # OAT-2026-002: $280 → $310 (RISE)                 → no alert expected
    # OAT-2026-003: $620 → $595 = $25 savings (< $50)  → no alert expected
    expected_alerts = [bk for bk in MOCK_BOOKINGS
                       if bk["monitored_rate"] < bk["booked_rate"]
                       and bk["booked_rate"] - bk["monitored_rate"] >= bk["target_savings_threshold"]]
    expected_silent = [bk for bk in MOCK_BOOKINGS if bk not in expected_alerts]

    info(f"Bookings triggering alert (savings >= threshold): {len(expected_alerts)}")
    info(f"Bookings staying silent  (rise or sub-threshold): {len(expected_silent)}")

    os.environ["SQS_ALERT_QUEUE_URL"] = alert_queue_url
    sqs_client.purge_queue(QueueUrl=alert_queue_url)

    async with async_session() as session:
        for bk in MOCK_BOOKINGS:
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

    # Verify current_rate stored correctly
    async with async_session() as session:
        for bk in MOCK_BOOKINGS:
            result = await session.execute(
                select(ActiveBooking).where(ActiveBooking.booking_id == bk["booking_id"])
            )
            row = result.scalar_one()
            check(
                f"current_rate stored: {bk['booking_id']}",
                row.current_rate == bk["monitored_rate"],
                f"expected={bk['monitored_rate']}, stored={row.current_rate}",
            )

    resp = sqs_client.receive_message(
        QueueUrl=alert_queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=0,
    )
    alerts = resp.get("Messages", [])
    alert_bodies = [json.loads(m["Body"]) for m in alerts]

    check(
        f"Alert queue received exactly {len(expected_alerts)} message(s)",
        len(alerts) == len(expected_alerts),
        f"expected={len(expected_alerts)}, received={len(alerts)}",
    )

    for body in alert_bodies:
        is_savings_event  = body.get("event") == "price_protection_savings"
        is_actually_lower = body.get("current_rate", 0) < body.get("booked_rate", 0)
        meets_threshold   = body.get("savings_amount", 0) >= body.get("threshold_triggered", 0)
        has_webhook       = body.get("webhook_url") == ALERT_WEBHOOK
        check(
            f"Alert payload valid for booking {body.get('booking_id', '')!r}",
            is_savings_event and is_actually_lower and meets_threshold and has_webhook,
            (
                f"event={body.get('event')}, "
                f"booked=${body.get('booked_rate')}, "
                f"current=${body.get('current_rate')}, "
                f"savings=${body.get('savings_amount')} ({body.get('savings_pct')}%), "
                f"webhook={'✓' if has_webhook else '✗'}"
            ),
        )
        info(
            f"  └─ {body.get('booking_id')}  "
            f"client={body.get('client_name')}  "
            f"${body.get('booked_rate')} → ${body.get('current_rate')}  "
            f"(saves ${body.get('savings_amount')})"
        )

    silent_ids  = {bk["booking_id"] for bk in expected_silent}
    alerted_ids = {b.get("booking_id") for b in alert_bodies}
    false_alerts = silent_ids & alerted_ids
    check(
        "No alert fired for RISE or sub-threshold drop",
        len(false_alerts) == 0,
        f"false alerts for: {false_alerts}" if false_alerts else "clean",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Status transition
# ─────────────────────────────────────────────────────────────────────────────

async def step_4_status_transition() -> None:
    section("STEP 4 · Status Transition (monitoring → rebooked)")
    from src.db.database import async_session
    from src.db.crud import update_booking_status
    from sqlalchemy import select
    from src.db.models import ActiveBooking

    target = MOCK_BOOKINGS[0]

    async with async_session() as session:
        await update_booking_status(
            session=session,
            booking_id=target["booking_id"],
            status="rebooked",
        )

    async with async_session() as session:
        result = await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id == target["booking_id"])
        )
        row = result.scalar_one()

    check(
        f"Status updated to 'rebooked' for {target['booking_id']}",
        row.status == "rebooked",
        f"stored status: {row.status}",
    )

    # Verify other bookings are unaffected
    async with async_session() as session:
        result = await session.execute(
            select(ActiveBooking).where(ActiveBooking.booking_id == MOCK_BOOKINGS[1]["booking_id"])
        )
        other = result.scalar_one()

    check(
        f"Other booking {MOCK_BOOKINGS[1]['booking_id']} status unchanged",
        other.status == "monitoring",
        f"stored status: {other.status}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — SQS task message round-trip (send → poll → delete)
# ─────────────────────────────────────────────────────────────────────────────

def step_5_sqs_round_trip(main_queue_url: str) -> None:
    section("STEP 5 · SQS Task Message Round-Trip (send → poll → delete)")

    os.environ["SQS_QUEUE_URL"] = main_queue_url
    from src.queue_manager import SQSManager

    sqs = SQSManager(queue_url=main_queue_url)

    test_url = MOCK_BOOKINGS[0]["provider_url"]
    msg_id = sqs.send_message(
        url=test_url,
        use_browser=False,
        job_id="seed-test-job-001",
        metadata={"booking_id": MOCK_BOOKINGS[0]["booking_id"]},
    )
    check("send_message returned a MessageId", bool(msg_id), f"MessageId={msg_id}")

    messages = sqs.poll_messages(max_messages=1, wait_time=0)
    check("poll_messages received 1 message", len(messages) == 1)

    if messages:
        body = messages[0]
        check(
            "Message URL matches sent URL",
            body.get("url") == test_url,
            f"got: {body.get('url')}",
        )
        check(
            "Message has task_id field",
            bool(body.get("task_id")),
            f"task_id={body.get('task_id')}",
        )
        check(
            "Message has _receipt_handle",
            bool(body.get("_receipt_handle")),
        )
        info(f"Payload preview: {json.dumps({k: v for k, v in body.items() if not k.startswith('_')})}")

        deleted = sqs.delete_message(body["_receipt_handle"])
        check("delete_message succeeded", deleted)

    empty = sqs.poll_messages(max_messages=1, wait_time=0)
    check("Queue is empty after deletion", len(empty) == 0)


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — DLQ inspection helpers
# ─────────────────────────────────────────────────────────────────────────────

def step_6_dlq_inspection(dlq_url: str, sqs_client) -> None:
    section("STEP 6 · DLQ Inspection Helpers")

    dead_payload = {
        "url": MOCK_BOOKINGS[2]["provider_url"],
        "use_browser": False,
        "booking_id": MOCK_BOOKINGS[2]["booking_id"],
        "error": "HTTP 503 Service Unavailable",
    }
    sqs_client.send_message(
        QueueUrl=dlq_url,
        MessageBody=json.dumps(dead_payload),
        MessageGroupId="dlq-test",
    )
    info("Manually injected 1 failed scrape message into DLQ.")

    os.environ["SQS_DLQ_URL"] = dlq_url
    from src.queue_manager import SQSManager
    sqs = SQSManager()

    count = sqs.get_dlq_count(dlq_url=dlq_url)
    check(
        "get_dlq_count returns >= 1",
        count >= 1,
        f"returned: {count}",
    )

    messages = sqs.get_dlq_messages(max_messages=10, dlq_url=dlq_url)
    check(
        "get_dlq_messages returns the injected message",
        len(messages) >= 1,
        f"messages returned: {len(messages)}",
    )

    if messages:
        m = messages[0]
        check(
            "DLQ message contains _receive_count metadata",
            "_receive_count" in m,
            f"keys: {list(m.keys())}",
        )
        info(
            f"Dead URL: {m.get('url')}  "
            f"booking_id={m.get('booking_id')}  "
            f"receive_count={m.get('_receive_count')}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def _run_db_steps() -> None:
    ok = await step_0_init_db()
    if not ok:
        warn("Skipping DB-dependent steps — fix the DB connection first.")
        return
    await step_1_register_bookings()
    await step_2_idempotency()
    await step_4_status_transition()


def _run_sqs_steps_with_mock() -> None:
    """Run SQS steps inside a moto mock_aws context."""
    try:
        from moto import mock_aws
    except ImportError:
        warn("moto not installed. Run:  pip install 'moto[sqs]'")
        warn("Skipping SQS steps.")
        return

    import boto3

    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")

        main_q = client.create_queue(
            QueueName="scraper-tasks.fifo",
            Attributes={
                "FifoQueue": "true",
                "ContentBasedDeduplication": "true",
            },
        )["QueueUrl"]

        alert_q = client.create_queue(
            QueueName="scraper-price-alerts",
        )["QueueUrl"]

        dlq = client.create_queue(
            QueueName="scraper-dlq.fifo",
            Attributes={
                "FifoQueue": "true",
                "ContentBasedDeduplication": "true",
            },
        )["QueueUrl"]

        info(f"[moto] Main queue:  {main_q}")
        info(f"[moto] Alert queue: {alert_q}")
        info(f"[moto] DLQ:         {dlq}")

        asyncio.run(_run_savings_alert_step(alert_q, client))

        step_5_sqs_round_trip(main_q)
        step_6_dlq_inspection(dlq, client)


async def _run_savings_alert_step(alert_queue_url: str, sqs_client) -> None:
    await step_3_savings_alert_trigger(alert_queue_url, sqs_client)


def main() -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 62}{RESET}")
    print(f"{BOLD}{CYAN}  SignalPipe — Travel Price Protection Validation{RESET}")
    print(f"{CYAN}  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 62}{RESET}")

    asyncio.run(_run_db_steps())

    # Dispose the engine pool so asyncpg connections bound to the first
    # event loop are not reused by the second asyncio.run() call below.
    from src.db.database import engine as _engine
    _engine.sync_engine.dispose()

    section("SQS TESTS  (moto in-process mock)")
    info("Using moto mock_aws — no real AWS credentials or LocalStack needed.")
    _run_sqs_steps_with_mock()

    summary()
    failed = sum(1 for _, ok in _results if not ok)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
