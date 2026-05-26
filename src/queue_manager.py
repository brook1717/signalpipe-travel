import json
import os
import uuid

import boto3
from botocore.exceptions import ClientError

from src.logger import setup_logger

logger = setup_logger(__name__)

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_ALERT_QUEUE_URL = os.environ.get("SQS_ALERT_QUEUE_URL", "")
SQS_DLQ_URL = os.environ.get("SQS_DLQ_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


class SQSManager:
    """Manages sending and receiving messages from an AWS SQS queue."""

    def __init__(self, queue_url: str = SQS_QUEUE_URL, region: str = AWS_REGION):
        self.queue_url = queue_url
        self.client = boto3.client("sqs", region_name=region)

        if not self.queue_url:
            logger.warning("SQS_QUEUE_URL is not set. Queue operations will fail.")

    def send_message(
        self,
        url: str,
        use_browser: bool = False,
        proxy: str | None = None,
        job_id: str | None = None,
        metadata: dict | None = None,
    ) -> str | None:
        """Send a scraping task message to the SQS queue.

        Returns the SQS MessageId on success, or None on failure.
        """
        message_body = {
            "task_id": str(uuid.uuid4()),
            "url": url,
            "use_browser": use_browser,
            "proxy": proxy,
            "job_id": job_id,
            "metadata": metadata or {},
        }

        try:
            response = self.client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(message_body),
                MessageGroupId=job_id or "default",
            )
            msg_id = response.get("MessageId")
            logger.info("SQS message sent: %s (url=%s)", msg_id, url)
            return msg_id
        except ClientError as exc:
            logger.error("Failed to send SQS message for %s: %s", url, exc)
            return None

    def send_batch(
        self,
        urls: list[str],
        use_browser: bool = False,
        proxy: str | None = None,
        job_id: str | None = None,
    ) -> int:
        """Send multiple URLs as individual messages. Returns count of successful sends."""
        success_count = 0
        # SQS SendMessageBatch supports max 10 per call
        batch_size = 10
        for i in range(0, len(urls), batch_size):
            chunk = urls[i:i + batch_size]
            entries = []
            for idx, url in enumerate(chunk):
                entries.append({
                    "Id": str(idx),
                    "MessageBody": json.dumps({
                        "task_id": str(uuid.uuid4()),
                        "url": url,
                        "use_browser": use_browser,
                        "proxy": proxy,
                        "job_id": job_id,
                        "metadata": {},
                    }),
                    "MessageGroupId": job_id or "default",
                })
            try:
                response = self.client.send_message_batch(
                    QueueUrl=self.queue_url,
                    Entries=entries,
                )
                success_count += len(response.get("Successful", []))
                failed = response.get("Failed", [])
                if failed:
                    logger.warning("SQS batch: %d messages failed.", len(failed))
            except ClientError as exc:
                logger.error("SQS batch send failed: %s", exc)

        logger.info("SQS batch complete: %d/%d messages sent.", success_count, len(urls))
        return success_count

    def poll_messages(
        self,
        max_messages: int = 10,
        wait_time: int = 20,
        visibility_timeout: int = 120,
    ) -> list[dict]:
        """Long-poll the SQS queue and return parsed message bodies.

        Each returned dict includes the parsed body and the ReceiptHandle
        for deletion after processing.
        """
        try:
            response = self.client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=min(max_messages, 10),
                WaitTimeSeconds=wait_time,
                VisibilityTimeout=visibility_timeout,
            )
        except ClientError as exc:
            logger.error("SQS poll failed: %s", exc)
            return []

        messages = response.get("Messages", [])
        if not messages:
            logger.info("SQS poll: no messages available.")
            return []

        results = []
        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                body["_receipt_handle"] = msg["ReceiptHandle"]
                body["_message_id"] = msg["MessageId"]
                results.append(body)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Skipping malformed SQS message: %s", exc)

        logger.info("SQS poll: received %d messages.", len(results))
        return results

    def delete_message(self, receipt_handle: str) -> bool:
        """Delete a processed message from the queue."""
        try:
            self.client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle,
            )
            return True
        except ClientError as exc:
            logger.error("Failed to delete SQS message: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Price-drop alert queue
    # ------------------------------------------------------------------

    def send_price_alert(
        self,
        url: str,
        old_price: float,
        new_price: float,
        webhook_url: str | None = None,
        alert_queue_url: str = SQS_ALERT_QUEUE_URL,
    ) -> str | None:
        """Send a price-drop alert to the dedicated SQS alert queue.

        Returns the SQS MessageId on success, or None on failure.
        """
        if not alert_queue_url:
            logger.warning("SQS_ALERT_QUEUE_URL not set. Alert not sent for %s.", url)
            return None

        alert_body = {
            "event": "price_drop",
            "url": url,
            "old_price": old_price,
            "new_price": new_price,
            "drop_amount": round(old_price - new_price, 4),
            "drop_pct": round((old_price - new_price) / old_price * 100, 2),
            "webhook_url": webhook_url,
        }

        try:
            response = self.client.send_message(
                QueueUrl=alert_queue_url,
                MessageBody=json.dumps(alert_body),
            )
            msg_id = response.get("MessageId")
            logger.info(
                "[ALERT] Price drop queued: %s (%.2f → %.2f, -%.2f%%), MessageId=%s",
                url, old_price, new_price, alert_body["drop_pct"], msg_id,
            )
            return msg_id
        except ClientError as exc:
            logger.error("Failed to send price-drop alert for %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Dead-Letter Queue (DLQ) inspection helpers
    # ------------------------------------------------------------------

    def get_dlq_count(self, dlq_url: str = SQS_DLQ_URL) -> int:
        """Return the approximate number of messages currently in the DLQ.

        Uses CloudWatch-backed ApproximateNumberOfMessages attribute.
        Returns -1 if the DLQ URL is not configured or the call fails.
        """
        if not dlq_url:
            logger.warning("SQS_DLQ_URL not set. Cannot inspect DLQ.")
            return -1

        try:
            response = self.client.get_queue_attributes(
                QueueUrl=dlq_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            count = int(response["Attributes"].get("ApproximateNumberOfMessages", 0))
            logger.info("DLQ depth: %d messages.", count)
            return count
        except ClientError as exc:
            logger.error("Failed to get DLQ depth: %s", exc)
            return -1

    def get_dlq_messages(
        self,
        max_messages: int = 10,
        dlq_url: str = SQS_DLQ_URL,
        delete_after_read: bool = False,
    ) -> list[dict]:
        """Peek at (or drain) messages in the DLQ for inspection.

        By default, messages remain in the DLQ (visibility timeout expires
        and they become visible again). Set delete_after_read=True to purge
        them after inspection.

        Returns a list of parsed message bodies including the failed URL.
        """
        if not dlq_url:
            logger.warning("SQS_DLQ_URL not set. Cannot read DLQ.")
            return []

        try:
            response = self.client.receive_message(
                QueueUrl=dlq_url,
                MaxNumberOfMessages=min(max_messages, 10),
                WaitTimeSeconds=1,
                VisibilityTimeout=30,
                AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
            )
        except ClientError as exc:
            logger.error("DLQ receive failed: %s", exc)
            return []

        raw_messages = response.get("Messages", [])
        if not raw_messages:
            logger.info("DLQ is empty.")
            return []

        results = []
        for msg in raw_messages:
            try:
                body = json.loads(msg["Body"])
                body["_receipt_handle"] = msg["ReceiptHandle"]
                body["_message_id"] = msg["MessageId"]
                body["_receive_count"] = msg.get("Attributes", {}).get("ApproximateReceiveCount")
                body["_sent_timestamp"] = msg.get("Attributes", {}).get("SentTimestamp")
                results.append(body)

                if delete_after_read:
                    self.client.delete_message(
                        QueueUrl=dlq_url,
                        ReceiptHandle=msg["ReceiptHandle"],
                    )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Skipping malformed DLQ message: %s", exc)

        logger.info(
            "DLQ inspection: %d message(s) read%s.",
            len(results), " and deleted" if delete_after_read else "",
        )
        return results
