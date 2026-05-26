import asyncio
import sys

import pandas as pd

from src.logger import setup_logger
from src.cli import parse_arguments
from src.fetcher import DataFetcher, BrowserFetcher
from src.processor import DataProcessor
from src.exporter import DataExporter
from src.proxy_manager import ProxyManager
from src.queue_manager import SQSManager
from src.delivery import WebhookDeliverer

logger = setup_logger(__name__)


async def _save_ceiling_records(source_url: str, raw_data: list[dict]) -> None:
    """Flag active bookings as ceiling_truncated when the pagination ceiling is hit.

    Extracts the first numeric 'price' value from raw_data and calls
    update_rate_by_provider_url with status='ceiling_truncated'.  Failures are
    logged but never raise — the local export must still complete.
    """
    try:
        from decimal import Decimal
        from src.db.database import async_session
        from src.db.crud import update_rate_by_provider_url

        current_rate: Decimal | None = None
        for record in raw_data:
            raw_price = record.get("price")
            if raw_price is not None:
                try:
                    current_rate = Decimal(str(raw_price))
                    break
                except Exception:
                    continue

        if current_rate is None:
            logger.warning(
                "[SAFETY CEILING] No 'price' key found in scraped records for %s "
                "— rate update skipped.",
                source_url,
            )
            return

        async with async_session() as session:
            updated = await update_rate_by_provider_url(
                session=session,
                provider_url=source_url,
                current_rate=current_rate,
                status="ceiling_truncated",
            )

        logger.info(
            "[SAFETY CEILING] Rate $%.2f flagged as ceiling_truncated "
            "for %d booking(s) at %s.",
            float(current_rate), len(updated), source_url,
        )
    except Exception as exc:
        logger.error(
            "[SAFETY CEILING] Rate update failed for %s: %s. "
            "Records will still be exported locally.",
            source_url, exc,
        )


def main():
    logger.info("Scraper Initialized")

    try:
        # 1. Parse CLI arguments
        args = parse_arguments()
        logger.info(
            "Args: source=%s, search=%s, format=%s, output=%s, "
            "use_browser=%s, proxies=%s, queue=%s",
            args.source, args.search, args.format, args.output,
            args.use_browser, args.proxies, args.queue,
        )

        # 2. Resolve proxy (if provided)
        proxy = None
        if args.proxies:
            proxy_manager = ProxyManager(args.proxies)
            proxy = proxy_manager.get_next_proxy()
            if proxy:
                logger.info("Proxy selected: %s", proxy)
            else:
                logger.warning("No usable proxy found. Proceeding without proxy.")

        # 3. Queue mode: dispatch to SQS and exit early
        if args.queue:
            logger.info("Queue mode enabled. Dispatching to SQS.")
            sqs = SQSManager()
            msg_id = sqs.send_message(
                url=args.source,
                use_browser=args.use_browser,
                proxy=proxy,
                metadata={"search": args.search, "format": args.format, "output": args.output},
            )
            if msg_id:
                logger.info("Task dispatched to SQS (MessageId=%s). Exiting.", msg_id)
            else:
                logger.error("Failed to dispatch task to SQS.")
                sys.exit(1)
            return

        # 4. Fetch data (local execution)
        if args.use_browser:
            logger.info("Using BrowserFetcher (Playwright stealth mode).")
            browser_fetcher = BrowserFetcher(proxy=proxy)
            raw_html = browser_fetcher.fetch_html(args.source)

            # Two-stage extraction
            processor = DataProcessor()
            raw_data = processor.extract(raw_html)
            if not raw_data:
                # Fallback to pd.read_html for table pages
                try:
                    tables = pd.read_html(raw_html)
                    if tables:
                        raw_data = tables[0].to_dict(orient="records")
                        logger.info("Extracted %d records from HTML table.", len(raw_data))
                except ValueError:
                    pass
        else:
            logger.info("Using DataFetcher (standard requests).")
            fetcher = DataFetcher()
            if proxy:
                fetcher.session.proxies.update(
                    {"http": proxy, "https": proxy}
                )
                logger.info("Proxy applied to DataFetcher session.")

            params = {}
            if args.search:
                params["search"] = args.search

            raw_data = fetcher.fetch_all_pages(
                args.source,
                params=params,
                max_pages=args.max_pages,
            )
            logger.info("Fetched %d raw records.", len(raw_data))

            if fetcher.last_fetch_hit_ceiling:
                logger.warning(
                    "[SAFETY CEILING] Pagination ceiling (%d pages) hit for %s. "
                    "Persisting %d partial records to PostgreSQL before export.",
                    args.max_pages, args.source, len(raw_data),
                )
                asyncio.run(_save_ceiling_records(args.source, raw_data))

        if not raw_data:
            logger.warning("No data fetched. Exiting.")
            sys.exit(0)

        # 5. Process data
        processor = DataProcessor()
        processor.load_data(raw_data)
        processor.clean_data()
        processor.deduplicate()

        # Apply optional filter
        if args.filter_key and args.filter_value:
            df = processor.apply_filter(args.filter_key, args.filter_value)
        else:
            df = processor.df

        # 6. Export data
        exporter = DataExporter(df)
        if args.format == "json":
            output_path = exporter.export_to_json(args.output)
        else:
            output_path = exporter.export_to_csv(args.output)

        logger.info("Pipeline complete. Output saved to: %s", output_path)

        # 7. Optional webhook delivery
        if args.webhook:
            logger.info("Webhook delivery enabled: %s", args.webhook)
            deliverer = WebhookDeliverer(webhook_url=args.webhook)
            records_to_send = df.to_dict(orient="records")
            result = deliverer.deliver_batch(records_to_send)
            logger.info("Webhook delivery result: %s", result)

    except KeyboardInterrupt:
        logger.info("Operation cancelled by user.")
        sys.exit(0)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
