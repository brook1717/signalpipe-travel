"""Local pipeline validation script.

Tests every layer of the stack without needing real AWS credentials:
  1. DB connectivity & table creation
  2. Idempotent upsert (insert + re-insert same URL)
  3. Price-drop delta trigger (alert message dispatched to mock SQS alert queue)
  4. No-alert guard (price increase must NOT dispatch an alert)
  5. Direct SQS task message round-trip (send → poll → delete)
  6. DLQ inspection helpers (get_dlq_count, get_dlq_messages)

SQS is mocked in-process with moto — no LocalStack or real AWS needed.
If SQS_ENDPOINT_URL is set (e.g. http://localhost:4566), LocalStack is used instead.

Usage:
    python -m src.seed_test_data
"""

import asyncio
import json
import os
import sys
import textwrap
from datetime import datetime, timezone

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
# Mock data fixtures
# ─────────────────────────────────────────────────────────────────────────────

MOCK_PRODUCTS = [
    {
        "url": "https://competitor-a.com/product/laptop-pro-x",
        "initial_price": 1_299.99,
        "updated_price": 999.99,   # DROP  → alert expected
        "payload": {"title": "Laptop Pro X", "brand": "Apex", "category": "Electronics"},
    },
    {
        "url": "https://competitor-b.com/shop/anc-headphones",
        "initial_price": 249.00,
        "updated_price": 299.00,   # RISE  → no alert expected
        "payload": {"title": "ANC Headphones", "brand": "SoundCore", "category": "Audio"},
    },
    {
        "url": "https://competitor-c.com/deals/monitor-4k",
        "initial_price": 599.99,
        "updated_price": 449.99,   # DROP  → alert expected
        "payload": {"title": "4K Monitor Ultra", "brand": "ViewMax", "category": "Displays"},
    },
]

ALERT_WEBHOOK = "https://hooks.zapier.com/hooks/catch/test/abc123/"

# ─────────────────────────────────────────────────────────────────────────────
# Step 0 — Database init
# ─────────────────────────────────────────────────────────────────────────────

async def step_0_init_db() -> bool:
    section("STEP 0 · Database Connectivity & Table Creation")
    try:
        from src.db.database import init_db, close_db
        await init_db()
        info("Tables created / verified via SQLAlchemy metadata.create_all.")
        check("PostgreSQL connection", True)
        return True
    except Exception as exc:
        check("PostgreSQL connection", False, str(exc))
        warn("Is the postgres container running?  docker compose up postgres -d")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Seed initial prices (first upsert = INSERT)
# ─────────────────────────────────────────────────────────────────────────────

async def step_1_seed_initial_prices() -> None:
    section("STEP 1 · Seed Initial Prices (INSERT path)")
    from src.db.database import async_session
    from src.db.crud import upsert_record
    from sqlalchemy import select
    from src.db.models import ScrapedRecord

    async with async_session() as session:
        for product in MOCK_PRODUCTS:
            record = await upsert_record(
                session=session,
                url=product["url"],
                payload=product["payload"],
                price=product["initial_price"],
                ai_fallback_used=False,
            )
            info(
                f"Inserted  {product['url'][-40:]}  "
                f"price=${product['initial_price']:.2f}"
            )

    # Verify all 3 rows exist
    async with async_session() as session:
        for product in MOCK_PRODUCTS:
            result = await session.execute(
                select(ScrapedRecord).where(
                    ScrapedRecord.source_url == product["url"]
                )
            )
            row = result.scalar_one_or_none()
            check(
                f"Row exists: {product['url'][-45:]}",
                row is not None and row.price == product["initial_price"],
                f"stored price={row.price if row else 'NOT FOUND'}",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Idempotency check (re-insert same URL → no duplicate)
# ─────────────────────────────────────────────────────────────────────────────

async def step_2_idempotency() -> None:
    section("STEP 2 · Idempotent Upsert (re-insert same URL)")
    from src.db.database import async_session
    from src.db.crud import upsert_record
    from sqlalchemy import func, select
    from src.db.models import ScrapedRecord

    target = MOCK_PRODUCTS[0]

    async with async_session() as session:
        # Re-insert with same price
        await upsert_record(
            session=session,
            url=target["url"],
            payload={**target["payload"], "extra_field": "re-scraped"},
            price=target["initial_price"],
        )

    # Row count must still be exactly the number we seeded
    async with async_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(ScrapedRecord)
        )
    info(f"Total rows in scraped_records: {count}")
    check(
        "No duplicate row created on re-upsert",
        count == len(MOCK_PRODUCTS),
        f"expected={len(MOCK_PRODUCTS)}, actual={count}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Price-drop delta trigger (with mocked SQS alert queue)
# ─────────────────────────────────────────────────────────────────────────────

async def step_3_price_drop_delta(
    alert_queue_url: str,
    sqs_client,
) -> None:
    section("STEP 3 · Price-Drop Delta Trigger")

    from src.db.database import async_session
    from src.db.crud import upsert_record
    from sqlalchemy import select
    from src.db.models import ScrapedRecord

    expected_drops  = [p for p in MOCK_PRODUCTS if p["updated_price"] < p["initial_price"]]
    expected_stable = [p for p in MOCK_PRODUCTS if p["updated_price"] >= p["initial_price"]]

    info(f"Products with price DROP  (alert expected): {len(expected_drops)}")
    info(f"Products with price RISE  (no alert):       {len(expected_stable)}")

    # Ensure crud reads the mock alert queue URL
    os.environ["SQS_ALERT_QUEUE_URL"] = alert_queue_url

    # --- Purge alert queue before the test ---
    sqs_client.purge_queue(QueueUrl=alert_queue_url)

    # --- Run upserts with updated prices ---
    async with async_session() as session:
        for product in MOCK_PRODUCTS:
            await upsert_record(
                session=session,
                url=product["url"],
                payload=product["payload"],
                price=product["updated_price"],
                ai_fallback_used=False,
                alert_webhook_url=ALERT_WEBHOOK,
            )

    # --- Verify DB prices were updated ---
    async with async_session() as session:
        for product in MOCK_PRODUCTS:
            result = await session.execute(
                select(ScrapedRecord).where(
                    ScrapedRecord.source_url == product["url"]
                )
            )
            row = result.scalar_one()
            check(
                f"Price updated in DB: {product['url'][-40:]}",
                row.price == product["updated_price"],
                f"expected={product['updated_price']}, stored={row.price}",
            )

    # --- Count alert messages dispatched ---
    resp = sqs_client.receive_message(
        QueueUrl=alert_queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=0,
    )
    alerts = resp.get("Messages", [])
    alert_bodies = [json.loads(m["Body"]) for m in alerts]

    check(
        f"Alert queue received {len(expected_drops)} message(s)",
        len(alerts) == len(expected_drops),
        f"expected={len(expected_drops)}, received={len(alerts)}",
    )

    for body in alert_bodies:
        is_drop_event = body.get("event") == "price_drop"
        is_actually_cheaper = body.get("new_price", 0) < body.get("old_price", 0)
        has_webhook = body.get("webhook_url") == ALERT_WEBHOOK
        check(
            f"Alert payload valid for {body.get('url', '')[-40:]}",
            is_drop_event and is_actually_cheaper and has_webhook,
            (
                f"event={body.get('event')}, "
                f"old={body.get('old_price')}, "
                f"new={body.get('new_price')}, "
                f"drop%={body.get('drop_pct')}%, "
                f"webhook={'✓' if has_webhook else '✗'}"
            ),
        )
        info(
            f"  └─ {body.get('url', '')[-45:]}  "
            f"${body.get('old_price')} → ${body.get('new_price')}  "
            f"(-{body.get('drop_pct')}%)"
        )

    # --- Verify the RISING price triggered NO alert ---
    rising_urls = {p["url"] for p in expected_stable}
    alert_urls  = {b.get("url") for b in alert_bodies}
    false_alerts = rising_urls & alert_urls
    check(
        "No alert fired for price INCREASE",
        len(false_alerts) == 0,
        f"false alerts for: {false_alerts}" if false_alerts else "clean",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — AI fallback flag persisted to DB
# ─────────────────────────────────────────────────────────────────────────────

async def step_4_ai_fallback_flag() -> None:
    section("STEP 4 · AI Fallback Flag Persisted")
    from src.db.database import async_session
    from src.db.crud import upsert_record
    from sqlalchemy import select
    from src.db.models import ScrapedRecord

    target = MOCK_PRODUCTS[2]

    async with async_session() as session:
        await upsert_record(
            session=session,
            url=target["url"],
            payload={**target["payload"], "healed": True},
            price=target["initial_price"],
            ai_fallback_used=True,
        )

    async with async_session() as session:
        result = await session.execute(
            select(ScrapedRecord).where(
                ScrapedRecord.source_url == target["url"]
            )
        )
        row = result.scalar_one()

    check(
        "ai_fallback_used=True stored in DB",
        row.ai_fallback_used is True,
        f"stored value: {row.ai_fallback_used}",
    )

    # Check that the other records still have ai_fallback_used=False
    async with async_session() as session:
        result = await session.execute(
            select(ScrapedRecord).where(
                ScrapedRecord.source_url == MOCK_PRODUCTS[0]["url"]
            )
        )
        other = result.scalar_one()

    check(
        "ai_fallback_used=False not contaminated on other records",
        other.ai_fallback_used is False,
        f"stored value: {other.ai_fallback_used}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — SQS task message round-trip (send → poll → delete)
# ─────────────────────────────────────────────────────────────────────────────

def step_5_sqs_round_trip(main_queue_url: str) -> None:
    section("STEP 5 · SQS Task Message Round-Trip (send → poll → delete)")

    os.environ["SQS_QUEUE_URL"] = main_queue_url
    from src.queue_manager import SQSManager

    sqs = SQSManager(queue_url=main_queue_url)

    # Send
    test_url = MOCK_PRODUCTS[0]["url"]
    msg_id = sqs.send_message(
        url=test_url,
        use_browser=False,
        job_id="seed-test-job-001",
        metadata={"frequency": "Hourly", "delivery": "Webhook"},
    )
    check("send_message returned a MessageId", bool(msg_id), f"MessageId={msg_id}")

    # Poll
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

        # Delete
        deleted = sqs.delete_message(body["_receipt_handle"])
        check("delete_message succeeded", deleted)

    # Verify queue is empty
    empty = sqs.poll_messages(max_messages=1, wait_time=0)
    check("Queue is empty after deletion", len(empty) == 0)


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — DLQ inspection helpers
# ─────────────────────────────────────────────────────────────────────────────

def step_6_dlq_inspection(dlq_url: str, sqs_client) -> None:
    section("STEP 6 · DLQ Inspection Helpers")

    # Manually push a simulated dead-letter message
    dead_payload = {
        "url": "https://dead-competitor.com/404-product",
        "use_browser": False,
        "job_id": "failed-job-999",
        "error": "HTTP 404 Not Found",
    }
    sqs_client.send_message(
        QueueUrl=dlq_url,
        MessageBody=json.dumps(dead_payload),
        MessageGroupId="dlq-test",
    )
    info("Manually injected 1 failed message into DLQ.")

    os.environ["SQS_DLQ_URL"] = dlq_url
    from src.queue_manager import SQSManager
    sqs = SQSManager()

    # get_dlq_count
    count = sqs.get_dlq_count(dlq_url=dlq_url)
    check(
        "get_dlq_count returns >= 1",
        count >= 1,
        f"returned: {count}",
    )

    # get_dlq_messages
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
    await step_1_seed_initial_prices()
    await step_2_idempotency()
    await step_4_ai_fallback_flag()


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

        # Create queues inside the mock context
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

        # price-drop delta (async, must be run inside this sync context)
        asyncio.run(_run_price_drop_step(alert_q, client))

        # synchronous SQS steps
        step_5_sqs_round_trip(main_q)
        step_6_dlq_inspection(dlq, client)


async def _run_price_drop_step(alert_queue_url: str, sqs_client) -> None:
    await step_3_price_drop_delta(alert_queue_url, sqs_client)


def main() -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 62}{RESET}")
    print(f"{BOLD}{CYAN}  SignalPipe — Local Pipeline Validation{RESET}")
    print(f"{CYAN}  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 62}{RESET}")

    # DB-dependent steps (always use real local postgres)
    asyncio.run(_run_db_steps())

    # Dispose the engine pool so asyncpg connections bound to the first
    # event loop are not reused by the second asyncio.run() call below.
    from src.db.database import engine as _engine
    _engine.sync_engine.dispose()

    # SQS steps — moto in-process mock (no real AWS needed)
    section("SQS TESTS  (moto in-process mock)")
    info("Using moto mock_aws — no real AWS credentials or LocalStack needed.")
    _run_sqs_steps_with_mock()

    summary()
    failed = sum(1 for _, ok in _results if not ok)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
