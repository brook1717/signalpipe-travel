import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.logger import setup_logger

logger = setup_logger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_PAGES = 50
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}


def _is_retryable(exc: BaseException) -> bool:
    """Return True if the exception warrants a retry (429 or 5xx)."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return False


class DataFetcher:
    """HTTP client with automatic retries and error handling."""

    def __init__(self, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.session = requests.Session()
        self.session.headers.update(headers or DEFAULT_HEADERS)
        self.timeout = timeout
        self.last_fetch_hit_ceiling: bool = False

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def fetch_data(self, url: str, params: dict | None = None) -> requests.Response:
        """Fetch data from *url* and return the Response.

        Retries up to 3 times with exponential backoff on 429 and 5xx errors.
        Raises on non-retryable HTTP errors and timeouts.
        """
        logger.info("Fetching URL: %s | params: %s", url, params)
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            logger.info("Success: %s [%s]", url, response.status_code)
            return response
        except requests.HTTPError as exc:
            logger.warning(
                "HTTP error %s for %s",
                exc.response.status_code if exc.response is not None else "unknown",
                url,
            )
            raise
        except requests.ConnectionError:
            logger.error("Connection error for %s", url)
            raise
        except requests.Timeout:
            logger.error("Request timed out for %s", url)
            raise
        except requests.RequestException as exc:
            logger.error("Unexpected request error for %s: %s", url, exc)
            raise

    def fetch_all_pages(
        self,
        base_url: str,
        params: dict | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_param: str = "page",
        start_page: int = 1,
    ) -> list[dict]:
        """Fetch multiple pages of JSON data and return aggregated results.

        Loops through pages using *page_param* (default 'page') starting at
        *start_page* until the response returns an empty list or *max_pages*
        is reached (the safety ceiling).

        If the ceiling is hit while data is still being returned, a
        [SAFETY CEILING] warning is logged and self.last_fetch_hit_ceiling
        is set to True so the caller can persist partial data and alert.
        """
        self.last_fetch_hit_ceiling = False
        params = dict(params) if params else {}
        all_results: list[dict] = []
        _last_page_had_data = False

        for page in range(start_page, start_page + max_pages):
            params[page_param] = page
            logger.info("Fetching page %d / max %d of %s", page, max_pages, base_url)

            response = self.fetch_data(base_url, params=params)
            data = response.json()

            # Handle responses that are a list or a dict with a results key
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("results") or data.get("data") or data.get("items") or []
            else:
                items = []

            if not items:
                logger.info("No more data at page %d. Stopping naturally.", page)
                _last_page_had_data = False
                break

            all_results.extend(items)
            _last_page_had_data = True
            logger.info("Page %d: %d items (running total: %d)", page, len(items), len(all_results))

        else:
            # for-else fires ONLY when the loop ran to completion without a break,
            # meaning we exited because we hit max_pages — not because data ran out.
            if _last_page_had_data:
                self.last_fetch_hit_ceiling = True
                logger.warning(
                    "[SAFETY CEILING] max_pages=%d reached for %s. "
                    "%d items collected but the site likely has more pages. "
                    "Pagination truncated to prevent runaway proxy/compute costs. "
                    "Raise --max-pages if full extraction is required.",
                    max_pages, base_url, len(all_results),
                )

        return all_results


class BrowserFetcher:
    """Stealth browser fetcher using Playwright to bypass Cloudflare and JS-heavy pages."""

    def __init__(self, proxy: str | None = None, timeout: int = DEFAULT_TIMEOUT * 1000):
        self.proxy = proxy
        self.timeout = timeout

    def fetch_html(self, url: str) -> str:
        """Launch a headless Chromium browser, apply stealth, and return the page HTML.

        Uses networkidle to wait for JavaScript frameworks to fully load.
        """
        logger.info("BrowserFetcher navigating to: %s", url)

        launch_options: dict = {"headless": True}
        if self.proxy:
            launch_options["proxy"] = {"server": self.proxy}

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context()
            page = context.new_page()

            stealth_sync(page)

            page.goto(url, timeout=self.timeout)
            page.wait_for_load_state("networkidle", timeout=self.timeout)

            html = page.content()
            logger.info("BrowserFetcher success: %s (%d chars)", url, len(html))
            return html
        except Exception as exc:
            logger.error("BrowserFetcher error for %s: %s", url, exc)
            raise
        finally:
            if browser:
                browser.close()
            playwright.stop()
