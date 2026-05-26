import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.db.database import async_session, init_db, close_db
from src.db.models import ScrapeJob, ScrapedRecord
from src.tasks import scrape_url_task, process_and_store_task
from src.logger import setup_logger
from celery import chain

logger = setup_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: init DB on startup, close pool on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="Multi-Source Data Scraper API",
    description="Dispatch scraping jobs and retrieve structured results.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class JobRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, description="List of URLs to scrape")
    use_browser: bool = Field(False, description="Use Playwright stealth browser")
    proxy_config: dict | None = Field(None, description="Optional proxy config")
    schema_hint: str | None = Field(None, description="Target extraction schema hint")
    max_pages: int = Field(
        50,
        ge=1,
        le=500,
        description=(
            "Safety ceiling: max pages to paginate per URL (default 50). "
            "Prevents runaway pagination from generating unexpected compute costs."
        ),
    )


class JobResponse(BaseModel):
    job_id: str
    status: str
    total_urls: int


class RecordOut(BaseModel):
    source_url: str
    payload: dict
    scraped_at: str
    status: str

    class Config:
        from_attributes = True


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    total_urls: int
    completed_urls: int
    records: list[RecordOut]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/jobs", response_model=JobResponse, status_code=201)
async def create_job(request: JobRequest):
    """Dispatch a batch of URLs to the Celery scraping queue."""
    job_id = uuid.uuid4()

    async with async_session() as session:
        job = ScrapeJob(
            id=job_id,
            status="pending",
            total_urls=len(request.urls),
            schema_hint=request.schema_hint,
        )
        session.add(job)
        await session.commit()

    # Dispatch each URL as a chained Celery pipeline
    for url in request.urls:
        pipeline = chain(
            scrape_url_task.s(
                url,
                use_browser=request.use_browser,
                proxy_config=request.proxy_config,
                max_pages=request.max_pages,
            ),
            process_and_store_task.s(),
        )
        pipeline.apply_async()

    logger.info(
        "Job %s created with %d URLs (max_pages=%d).",
        job_id, len(request.urls), request.max_pages,
    )

    return JobResponse(
        job_id=str(job_id),
        status="pending",
        total_urls=len(request.urls),
    )


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str):
    """Retrieve the status and aggregated results for a scraping job."""
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format.")

    async with async_session() as session:
        result = await session.execute(
            select(ScrapeJob).where(ScrapeJob.id == uid)
        )
        job = result.scalar_one_or_none()

        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")

        records_result = await session.execute(
            select(ScrapedRecord).where(ScrapedRecord.job_id == uid)
        )
        records = records_result.scalars().all()

    return JobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        total_urls=job.total_urls,
        completed_urls=len(records),
        records=[
            RecordOut(
                source_url=r.source_url,
                payload=r.payload,
                scraped_at=r.scraped_at.isoformat(),
                status=r.status,
            )
            for r in records
        ],
    )
