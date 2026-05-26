from celery import chain

from src.worker import app
from src.logger import setup_logger

logger = setup_logger(__name__)


@app.task(bind=True, max_retries=3)
def scrape_url_task(
    self,
    url: str,
    use_browser: bool = True,
    proxy_config: dict | None = None,
    max_pages: int = 50,
):
    """Fetch a provider URL and chain the result into process_and_store_task.

    Retries with exponential backoff on failure (especially 429 Rate Limit).
    proxy_config example: {"proxy": "http://user:pass@ip:port"}
    """
    import asyncio
    from src.db.database import async_session
    from src.db.crud import expire_lapsed_bookings

    async def _deadline_guard() -> int:
        async with async_session() as session:
            return await expire_lapsed_bookings(session, url)

    active_count: int = asyncio.run(_deadline_guard())
    if active_count == 0:
        logger.info(
            "[DEADLINE GUARD] scrape_url_task: 0 active bookings remain for %s "
            "(all cancelled deadlines passed) — scrape bypassed, "
            "no proxy/compute/token cost incurred.",
            url,
        )
        return {"url": url, "skipped": True, "reason": "all_bookings_expired"}

    from src.fetcher import DataFetcher, BrowserFetcher

    proxy = proxy_config.get("proxy") if proxy_config else None
    logger.info("scrape_url_task: url=%s, browser=%s, proxy=%s", url, use_browser, proxy)

    try:
        if use_browser:
            fetcher = BrowserFetcher(proxy=proxy)
            html = fetcher.fetch_html(url)
            logger.info("scrape_url_task: fetched %d chars of HTML from %s", len(html), url)
            return {"url": url, "type": "html", "content": html}
        else:
            fetcher = DataFetcher()
            if proxy:
                fetcher.session.proxies.update({"http": proxy, "https": proxy})
            response = fetcher.fetch_data(url)
            data = response.json()
            logger.info("scrape_url_task: fetched JSON from %s", url)
            return {"url": url, "type": "json", "content": data}

    except Exception as exc:
        backoff = 2 ** self.request.retries * 15  # 15s, 30s, 60s
        logger.warning(
            "scrape_url_task retry %d/%d for %s (backoff=%ds): %s",
            self.request.retries + 1, self.max_retries, url, backoff, exc,
        )
        raise self.retry(exc=exc, countdown=backoff)


@app.task(bind=True, max_retries=3)
def process_and_store_task(self, fetch_result: dict):
    """Process fetched content and update current_rate for matching bookings.

    Extracts the first numeric 'price' key from scraped records, then calls
    update_rate_by_provider_url to push the rate to all active_bookings rows
    whose provider_url matches the scraped URL.
    """
    import asyncio
    from decimal import Decimal
    from src.processor import DataProcessor
    from src.db.database import async_session
    from src.db.crud import update_rate_by_provider_url

    if fetch_result.get("skipped"):
        logger.info(
            "process_and_store_task: upstream task skipped url=%s reason=%s — no processing.",
            fetch_result.get("url"),
            fetch_result.get("reason"),
        )
        return fetch_result

    url: str = fetch_result["url"]
    content_type: str = fetch_result["type"]
    content = fetch_result["content"]

    logger.info("process_and_store_task: url=%s, type=%s", url, content_type)

    try:
        processor = DataProcessor()

        if content_type == "html":
            records = processor.extract(content)
        else:
            records = content if isinstance(content, list) else [content]

        logger.info("process_and_store_task: extracted %d records from %s", len(records), url)

        current_rate: Decimal | None = None
        for record in records:
            raw_price = record.get("price") if isinstance(record, dict) else None
            if raw_price is not None:
                try:
                    current_rate = Decimal(str(raw_price))
                    break
                except Exception:
                    continue

        if current_rate is None:
            logger.warning(
                "process_and_store_task: no 'price' key found in records for %s "
                "— rate update skipped.",
                url,
            )
            return {"url": url, "bookings_updated": 0}

        async def _persist() -> int:
            async with async_session() as session:
                updated = await update_rate_by_provider_url(session, url, current_rate)
                return len(updated)

        count = asyncio.run(_persist())
        logger.info("process_and_store_task: updated rate for %d booking(s) at %s", count, url)
        return {"url": url, "bookings_updated": count}

    except Exception as exc:
        backoff = 2 ** self.request.retries * 10
        logger.error("process_and_store_task failed for %s: %s", url, exc)
        raise self.retry(exc=exc, countdown=backoff)


def dispatch_scrape(url: str, use_browser: bool = True, proxy_config: dict | None = None):
    """Dispatch a chained scrape → process → store pipeline to the Celery queue."""
    pipeline = chain(
        scrape_url_task.s(url, use_browser=use_browser, proxy_config=proxy_config),
        process_and_store_task.s(),
    )
    result = pipeline.apply_async()
    logger.info("Dispatched pipeline for %s (task_id=%s)", url, result.id)
    return result
