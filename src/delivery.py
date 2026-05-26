"""Webhook delivery module for pushing cleaned data to client endpoints.

Supports retries with exponential backoff for reliability.
"""

import time
from typing import Any

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from src.logger import setup_logger

logger = setup_logger(__name__)


class WebhookDeliverer:
    """Delivers JSON payloads to client webhook endpoints with retry logic.

    Use cases:
    - Zapier webhooks
    - Make.com (Integromat) endpoints
    - Client custom APIs
    - n8n / Pipedream / custom ETL endpoints
    """

    def __init__(
        self,
        webhook_url: str,
        timeout: int = 30,
        max_retries: int = 5,
        headers: dict[str, str] | None = None,
    ):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = headers or {"Content-Type": "application/json"}
        self._delivery_count = 0
        self._failure_count = 0

    @property
    def stats(self) -> dict:
        """Return delivery statistics."""
        return {
            "delivered": self._delivery_count,
            "failed": self._failure_count,
            "webhook_url": self.webhook_url,
        }

    def deliver(self, payload: list[dict] | dict) -> bool:
        """Deliver a JSON payload to the configured webhook endpoint.

        Wraps payload in a standard envelope and retries on transient failures.
        Returns True on success, False on permanent failure.
        """
        envelope = {
            "source": "multi-source-scraper",
            "records_count": len(payload) if isinstance(payload, list) else 1,
            "data": payload,
        }

        logger.info(
            "Webhook delivery: sending %d record(s) to %s",
            envelope["records_count"], self.webhook_url,
        )

        try:
            self._send_with_retry(envelope)
            self._delivery_count += 1
            logger.info("Webhook delivery successful to %s.", self.webhook_url)
            return True
        except Exception as exc:
            self._failure_count += 1
            logger.error(
                "Webhook delivery FAILED after %d retries to %s: %s",
                self.max_retries, self.webhook_url, exc,
            )
            return False

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        retry=retry_if_exception_type((
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.HTTPError,
        )),
        reraise=True,
    )
    def _send_with_retry(self, envelope: dict) -> None:
        """POST the envelope to the webhook URL with exponential backoff.

        Retries on:
        - Connection errors (endpoint temporarily down)
        - Timeouts
        - 5xx server errors
        - 429 rate limits
        """
        response = requests.post(
            self.webhook_url,
            json=envelope,
            headers=self.headers,
            timeout=self.timeout,
        )

        # Retry on server errors and rate limits
        if response.status_code == 429 or response.status_code >= 500:
            logger.warning(
                "Webhook returned %d. Retrying...", response.status_code,
            )
            response.raise_for_status()

        # Fail permanently on 4xx client errors (except 429)
        if 400 <= response.status_code < 500:
            logger.error(
                "Webhook returned client error %d: %s. Not retrying.",
                response.status_code, response.text[:200],
            )
            return  # Don't retry client errors

        response.raise_for_status()

    def deliver_batch(self, records: list[dict], batch_size: int = 50) -> dict:
        """Deliver records in batches to avoid overwhelming the client endpoint.

        Returns a summary dict with success/failure counts.
        """
        total = len(records)
        success = 0
        failed = 0

        for i in range(0, total, batch_size):
            batch = records[i:i + batch_size]
            if self.deliver(batch):
                success += len(batch)
            else:
                failed += len(batch)

        summary = {
            "total_records": total,
            "delivered": success,
            "failed": failed,
            "webhook_url": self.webhook_url,
        }
        logger.info("Batch delivery complete: %s", summary)
        return summary
