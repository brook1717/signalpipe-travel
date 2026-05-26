"""AWS Lambda handler for processing SQS scraping tasks.

Routing logic:
- Standard HTTP fetches (use_browser=False) → execute directly in Lambda
- Browser fetches (use_browser=True) → trigger ECS Fargate task via boto3
"""

import asyncio
import json
import os

import boto3

from src.logger import setup_logger
from src.fetcher import DataFetcher
from src.processor import DataProcessor
from src.db.database import async_session
from src.db.crud import upsert_record

logger = setup_logger(__name__)

ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "scraper-cluster")
ECS_TASK_DEFINITION = os.environ.get("ECS_TASK_DEFINITION", "scraper-worker")
ECS_SUBNETS = os.environ.get("ECS_SUBNETS", "").split(",")
ECS_SECURITY_GROUPS = os.environ.get("ECS_SECURITY_GROUPS", "").split(",")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

ecs_client = boto3.client("ecs", region_name=AWS_REGION)


def handler(event, context):
    """Lambda entry point triggered by SQS.

    Processes each SQS record:
    - If use_browser=False: fetches via DataFetcher, processes, and upserts to DB.
    - If use_browser=True: launches an ECS Fargate task for browser rendering.
    """
    records = event.get("Records", [])
    logger.info("Lambda invoked with %d SQS record(s).", len(records))

    results = {"processed": 0, "delegated_to_fargate": 0, "errors": 0}

    for record in records:
        try:
            body = json.loads(record["body"])
            url = body["url"]
            use_browser = body.get("use_browser", False)
            proxy = body.get("proxy")
            job_id = body.get("job_id")
            metadata = body.get("metadata", {})

            logger.info(
                "Processing: url=%s, use_browser=%s, job_id=%s",
                url, use_browser, job_id,
            )

            if use_browser:
                # Delegate to ECS Fargate for Playwright execution
                _launch_fargate_task(body)
                results["delegated_to_fargate"] += 1
            else:
                # Execute directly in Lambda
                _process_standard_fetch(url, proxy, metadata)
                results["processed"] += 1

        except Exception as exc:
            logger.error("Failed to process SQS record: %s", exc, exc_info=True)
            results["errors"] += 1

    logger.info(
        "Lambda complete: processed=%d, fargate=%d, errors=%d",
        results["processed"], results["delegated_to_fargate"], results["errors"],
    )
    return results


def _process_standard_fetch(url: str, proxy: str | None, metadata: dict):
    """Fetch URL with DataFetcher, process, and persist to database."""
    logger.info("Standard fetch (Lambda): %s", url)

    fetcher = DataFetcher()
    if proxy:
        fetcher.session.proxies.update({"http": proxy, "https": proxy})
        logger.info("Proxy applied: %s", proxy)

    # Fetch
    params = {}
    search = metadata.get("search")
    if search:
        params["search"] = search

    response = fetcher.fetch_data(url, params=params)
    data = response.json()

    # Normalize to list
    if isinstance(data, dict):
        records = data.get("results") or data.get("data") or data.get("items") or [data]
    elif isinstance(data, list):
        records = data
    else:
        records = [data]

    logger.info("Fetched %d records from %s.", len(records), url)

    # Process
    processor = DataProcessor()
    processor.load_data(records)
    processor.clean_data()
    processor.deduplicate()

    # Persist to database
    async def _persist():
        async with async_session() as session:
            for record in processor.df.to_dict(orient="records"):
                await upsert_record(session, url, record)

    asyncio.run(_persist())
    logger.info("Stored %d records for %s.", len(processor.df), url)


def _launch_fargate_task(message_body: dict):
    """Launch an ECS Fargate task for browser-based scraping."""
    url = message_body["url"]
    logger.info("Delegating to ECS Fargate: %s", url)

    try:
        response = ecs_client.run_task(
            cluster=ECS_CLUSTER,
            taskDefinition=ECS_TASK_DEFINITION,
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [s.strip() for s in ECS_SUBNETS if s.strip()],
                    "securityGroups": [s.strip() for s in ECS_SECURITY_GROUPS if s.strip()],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": "worker",
                    "environment": [
                        {"name": "SCRAPE_URL", "value": url},
                        {"name": "SCRAPE_PROXY", "value": message_body.get("proxy") or ""},
                        {"name": "SCRAPE_JOB_ID", "value": message_body.get("job_id") or ""},
                        {"name": "SCRAPE_METADATA", "value": json.dumps(message_body.get("metadata", {}))},
                    ],
                }],
            },
        )

        tasks = response.get("tasks", [])
        if tasks:
            task_arn = tasks[0]["taskArn"]
            logger.info("Fargate task launched: %s (url=%s)", task_arn, url)
        else:
            failures = response.get("failures", [])
            logger.error("Fargate launch failed for %s: %s", url, failures)

    except Exception as exc:
        logger.error("ECS run_task failed for %s: %s", url, exc)
        raise
