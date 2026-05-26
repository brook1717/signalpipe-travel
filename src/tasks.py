from celery import chain

from src.worker import app
from src.logger import setup_logger

logger = setup_logger(__name__)


@app.task(bind=True, max_retries=3)
def scrape_url_task(self, url: str, use_browser: bool = True, proxy_config: dict | None = None):
    """Fetch a URL and chain the result into process_and_store_task.

    Retries with exponential backoff on failure (especially 429 Rate Limit).
    proxy_config example: {"proxy": "http://user:pass@ip:port"}
    """
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
    """Process fetched content and persist to the database.

    Receives output from scrape_url_task. Runs the two-stage processor
    (DOM extraction → LLM fallback) for HTML content, then upserts into PostgreSQL.
    """
    import asyncio
    from src.processor import DataProcessor
    from src.db.database import async_session
    from src.db.crud import upsert_record

    url = fetch_result["url"]
    content_type = fetch_result["type"]
    content = fetch_result["content"]

    logger.info("process_and_store_task: url=%s, type=%s", url, content_type)

    try:
        processor = DataProcessor()

        if content_type == "html":
            records = processor.extract(content)
        else:
            # JSON content — already structured
            records = content if isinstance(content, list) else [content]

        logger.info("process_and_store_task: extracted %d records from %s", len(records), url)

        # Persist each record to the database
        async def _persist():
            async with async_session() as session:
                for record in records:
                    payload = record if isinstance(record, dict) else record.model_dump()
                    await upsert_record(session, url, payload)

        asyncio.run(_persist())
        logger.info("process_and_store_task: stored %d records for %s", len(records), url)

        return {"url": url, "records_stored": len(records)}

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
