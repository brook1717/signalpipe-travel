import os

from celery import Celery

from src.logger import setup_logger

logger = setup_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery("scraper")

app.conf.update(
    broker_url=REDIS_URL,
    result_backend=REDIS_URL,

    # High-throughput tuning
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    worker_max_tasks_per_child=100,

    # Strict time limits — kill hung Playwright browsers
    task_soft_time_limit=120,   # seconds — raises SoftTimeLimitExceeded
    task_time_limit=180,        # seconds — hard kill

    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Misc
    timezone="UTC",
    enable_utc=True,
)


@app.task(bind=True, max_retries=3, default_retry_delay=30)
def scrape_url(self, url: str, use_browser: bool = False, proxy: str | None = None):
    """Celery task: fetch a single URL and persist the result to the database.

    This task is designed to be distributed across many workers.
    """
    from src.fetcher import DataFetcher, BrowserFetcher

    logger.info("Worker processing URL: %s (browser=%s, proxy=%s)", url, use_browser, proxy)

    try:
        if use_browser:
            fetcher = BrowserFetcher(proxy=proxy)
            html = fetcher.fetch_html(url)
            return {"url": url, "type": "html", "length": len(html), "content": html[:500]}
        else:
            fetcher = DataFetcher()
            if proxy:
                fetcher.session.proxies.update({"http": proxy, "https": proxy})
            response = fetcher.fetch_data(url)
            data = response.json()
            return {"url": url, "type": "json", "data": data}

    except Exception as exc:
        logger.error("Task failed for %s: %s", url, exc)
        raise self.retry(exc=exc)
