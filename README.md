<div align="center">

# SignalPipe: Event-Driven Competitor Intelligence Engine

### Resilient · AI Self-Healing · Zero-Maintenance · Serverless

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![AWS Lambda](https://img.shields.io/badge/AWS-Lambda-FF9900.svg?style=flat&logo=aws-lambda&logoColor=white)](https://aws.amazon.com/lambda/)
[![AWS Fargate](https://img.shields.io/badge/AWS-Fargate-FF9900.svg?style=flat&logo=amazon-ecs&logoColor=white)](https://aws.amazon.com/fargate/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-Dashboard-FF4B4B.svg?style=flat&logo=streamlit&logoColor=white)](https://streamlit.io/)
[![Terraform](https://img.shields.io/badge/Terraform-IaC-7B42BC.svg?style=flat&logo=terraform&logoColor=white)](https://www.terraform.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**A production-grade, serverless competitor intelligence platform. SignalPipe monitors rival pricing 24/7, self-heals broken scrapers with Gemini AI, fires instant price-drop alerts, and delivers structured data to any webhook — with zero idle infrastructure cost.**

[Overview](#overview) · [Architecture](#architecture) · [Key Features](#key-features) · [Local Development](#local-development) · [Environment Variables](#environment-variables) · [Deployment](#deployment)

</div>

---

## Overview

SignalPipe is built for one job: **reliably tracking competitor prices at scale, with zero ongoing maintenance.**

Traditional scrapers have three fatal flaws:
- **They break silently** when a website redesigns — you lose data without knowing it.
- **They waste money** running always-on servers that sit idle between scraping cycles.
- **They don't scale** — going from 50 monitored URLs to 5,000 requires a re-architecture.

SignalPipe eliminates all three problems with an event-driven, serverless design:

- **Resilient by default** — every record is idempotently upserted via `ON CONFLICT`. Re-scraping the same URL 1,000 times produces exactly one database row.
- **AI self-healing** — when a competitor site changes its layout and DOM selectors break, Gemini 2.0 Flash automatically recovers the data, logs an audit trail, and flags the record so you know exactly which selectors to update.
- **Zero-maintenance cost model** — AWS SQS + Lambda + Fargate means you pay only for the seconds the system is actively scraping. Idle cost is near zero.

---

## Architecture

The full data pipeline flows from the client-facing Streamlit dashboard through the event-driven AWS backbone and terminates at the client's chosen delivery endpoint.

```
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                          CLIENT LAYER                                    │
  │                                                                          │
  │   ⚡ Streamlit Dashboard  ──────────────────────────────────────────┐   │
  │      Deploy Monitor tab                                             │   │
  │      System Health tab (metrics + DLQ viewer)                      │   │
  └─────────────────────────────────────────────────────────────────────┘   │
                                     │ POST /jobs                           │
                                     ▼                                      │
  ┌──────────────────────────────────────────────────────────────────────┐  │
  │                         FastAPI  (src/api.py)                         │  │
  │   POST /jobs  ·  GET /jobs/{id}  ·  max_pages ceiling enforced        │  │
  └─────────────────────────────┬────────────────────────────────────────┘  │
                                │ Enqueues per-URL task messages             │
                                ▼                                            │
  ┌──────────────────────────────────────────────────────────────────────┐  │
  │                   AWS SQS — Main FIFO Queue                           │  │
  │   Guaranteed ordering  ·  At-least-once delivery  ·  $0 idle cost     │  │
  │                                                                        │  │
  │   Redrive Policy → Dead-Letter Queue (DLQ) after 3 failed attempts    │  │
  └───────────────────────┬──────────────────────────────────────────────┘  │
                          │                                                  │
             ┌────────────┴──────────────┐                                  │
             ▼                           ▼                                   │
  ┌─────────────────────┐   ┌──────────────────────────┐                   │
  │    AWS Lambda        │   │      ECS Fargate          │                   │
  │  Standard HTTP fetch │   │  Playwright stealth mode  │                   │
  │  DataFetcher         │   │  JS-rendered / anti-bot   │                   │
  │  ~$0.0000002 / req   │   │  BrowserFetcher           │                   │
  └──────────┬──────────┘   └───────────┬──────────────┘                   │
             └──────────────┬───────────┘                                   │
                            ▼                                                │
  ┌──────────────────────────────────────────────────────────────────────┐  │
  │               Cost-Aware Two-Stage Extraction Engine                  │  │
  │                        (src/processor.py)                             │  │
  │                                                                        │  │
  │   Stage 1 ── BeautifulSoup DOM selectors      (free, ~5 ms)           │  │
  │                  │                                                     │  │
  │         Required fields present?                                       │  │
  │                  │                                                     │  │
  │        YES ──────┴────── NO (layout changed)                          │  │
  │         │                       │                                      │  │
  │    Return data            Stage 2 ── Gemini 2.0 Flash                 │  │
  │                                   HTML → Markdown (−80% tokens)       │  │
  │                                   Pydantic structured output          │  │
  │                                   ai_fallback_used = True             │  │
  └──────────────────────────────────┬───────────────────────────────────┘  │
                                     │                                       │
              ┌──────────────────────┼──────────────────────┐               │
              ▼                      ▼                       ▼               │
  ┌──────────────────┐  ┌────────────────────────┐  ┌──────────────────┐   │
  │   PostgreSQL      │  │  SQS Alert Queue        │  │ Webhook Delivery │   │
  │  Idempotent       │  │  Price-drop events      │  │ Zapier / Make /  │───┘
  │  ON CONFLICT      │  │  (price < prev price)   │  │ Custom endpoint  │
  │  upsert           │  │  → client notified      │  │ Retry + backoff  │
  │  SHA-256 dedup    │  └────────────────────────┘  └──────────────────┘
  └──────────────────┘
         ▲
         │  Failed messages (3× receive) routed here
  ┌──────────────────┐
  │  SQS Dead-Letter │
  │  Queue (DLQ)     │
  │  Visible in      │
  │  Dashboard       │
  └──────────────────┘
```

### Source Tree

```
├── src/
│   ├── api.py               # FastAPI — POST /jobs, GET /jobs/{id}
│   ├── dashboard.py         # Streamlit UI — Deploy Monitor + System Health tabs
│   ├── main.py              # CLI orchestrator (local execution path)
│   ├── cli.py               # Argument parsing (--max-pages ceiling, etc.)
│   ├── fetcher.py           # DataFetcher + BrowserFetcher (Playwright)
│   ├── processor.py         # Two-stage extraction: DOM → LLM fallback
│   ├── ai_parser.py         # Gemini 2.0 Flash structured extraction
│   ├── queue_manager.py     # SQS: send/poll/delete + alert queue + DLQ helpers
│   ├── lambda_handler.py    # AWS Lambda entry point
│   ├── delivery.py          # WebhookDeliverer with exponential backoff
│   ├── exporter.py          # CSV / JSON local export
│   ├── proxy_manager.py     # Round-robin proxy rotation
│   ├── seed_test_data.py    # Local validation script (DB + SQS pipeline test)
│   ├── logger.py            # Centralized structured logging
│   └── db/
│       ├── database.py      # Async SQLAlchemy engine + session factory
│       ├── models.py        # ScrapeJob + ScrapedRecord (price, ai_fallback_used)
│       └── crud.py          # Upsert with price-delta trigger + alert dispatch
├── terraform/
│   ├── main.tf              # VPC, RDS, ECS, SQS (main + alert + DLQ), ALB
│   ├── variables.tf
│   └── outputs.tf           # Queue URLs, API URL, RDS endpoint
├── Dockerfile               # Multi-stage: api + worker targets
├── docker-compose.yml       # Local: Postgres + FastAPI + Worker
├── print_secrets_template.py # Generates .env.example
└── requirements.txt
```

---

## Key Features

### 1. Price-Drop Delta Trigger

Every time a URL is re-scraped, the upsert logic compares the incoming price against the stored price **before committing the update**. If the new price is lower, a structured alert is dispatched to the dedicated SQS alert queue — before the database write, so no alert is ever missed.

```
Re-scrape fires
       │
       ▼
SELECT price WHERE source_url = ?   ← read existing price
       │
  new_price < old_price?
       │
   YES │                            NO → upsert silently, no alert
       ▼
Send to SQS Alert Queue:
  {
    "event":       "price_drop",
    "url":         "https://competitor.com/product",
    "old_price":   129.99,
    "new_price":    89.99,
    "drop_amount":  40.00,
    "drop_pct":     30.77,
    "webhook_url":  "https://hooks.zapier.com/..."
  }
       │
       ▼
ON CONFLICT DO UPDATE (price, scraped_at, payload)
```

Price alerts are decoupled from the main scraping queue so a surge in alert volume never delays the extraction pipeline.

---

### 2. LLM Fallback Extraction (Gemini 2.0 Flash)

The extraction engine is **cost-aware by design**. The LLM is never called speculatively — it fires only when the DOM extraction stage returns missing required fields, which is the direct signal that a site has changed its layout.

| Stage | Method | Cost | Triggered when |
|-------|--------|------|----------------|
| **Stage 1** | BeautifulSoup DOM selectors | Free (~5 ms) | Always |
| **Stage 2** | Gemini 2.0 Flash + Pydantic | ~$0.001/page | Required fields `None` after Stage 1 |

Before calling the LLM, the HTML is converted to Markdown — stripping images, scripts, nav, and footer — reducing token count by ~80%.

Every record extracted by the AI fallback has `ai_fallback_used = True` persisted in PostgreSQL, creating an auditable log. The System Health tab in the dashboard surfaces the 30-day count so you can see which competitor sites need selector maintenance.

```sql
-- Find sites that are currently depending on AI fallback
SELECT source_url, scraped_at
FROM scraped_records
WHERE ai_fallback_used = TRUE
ORDER BY scraped_at DESC;
```

---

### 3. Dead-Letter Queue (DLQ) & Reliability

Messages that fail processing **three consecutive times** are automatically moved to the Dead-Letter Queue via SQS's built-in Redrive Policy (`maxReceiveCount = 3`). This prevents poison-pill URLs from stalling the main queue.

The DLQ is directly visible in the **System Health** tab of the Streamlit dashboard, showing which competitor URLs are permanently broken and need to be updated or removed from monitoring.

```
Main Queue
    │
    │  Receive attempt 1 → failure
    │  Receive attempt 2 → failure
    │  Receive attempt 3 → failure
    │
    ▼
Dead-Letter Queue (14-day retention)
    │
    ▼
Dashboard → System Health → "Broken URLs (DLQ)" metric + dataframe
```

---

### 4. Pagination Safety Ceiling

Every job is subject to a hard `max_pages` ceiling (default: **50 pages per domain**) enforced at the fetcher level. If a site attempts to paginate indefinitely, the worker logs a `[SAFETY CEILING]` warning, persists the collected records to PostgreSQL with `status="ceiling_truncated"`, and halts — preventing runaway proxy bandwidth usage and unpredictable cloud compute costs.

```bash
# Tighten the ceiling for a spot-check
python -m src.main --source https://shop.com/products --max-pages 5 --output out.csv

# Raise it for a full catalogue extraction
curl -X POST http://localhost:8000/jobs \
  -d '{"urls": ["https://shop.com/products"], "max_pages": 200}'
```

---

### 5. Streamlit Competitor Intelligence Dashboard

A password-protected client-facing UI (set `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` env vars) with two tabs:

**Deploy Monitor** — submit a list of competitor URLs, choose monitoring frequency (Hourly/Daily) and alert delivery method (Webhook/Telegram), and dispatch the pipeline in one click.

**System Health** — three live metrics (Active Monitored URLs, AI Fallback Rescues, Broken URLs in DLQ) plus a full DLQ dataframe showing exactly which competitor links need attention.

```bash
# Protected demo mode
DASHBOARD_USERNAME=admin DASHBOARD_PASSWORD=secret \
  streamlit run src/dashboard.py

# Open access (local dev)
streamlit run src/dashboard.py
```

---

## Local Development

### Prerequisites

- Python 3.12+
- Docker Desktop (for the PostgreSQL container)

### 1. Clone & Install

```bash
git clone https://github.com/brook1717/multi-source-scraper-and-api-export-engine.git
cd multi-source-scraper-and-api-export-engine

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
python setup_playwright.py     # Only required for --use-browser mode
```

### 2. Generate & Configure Environment

```bash
python print_secrets_template.py   # writes .env.example
copy .env.example .env             # Windows
# cp .env.example .env             # macOS / Linux
```

Open `.env` and fill in your secrets. At minimum, set `DATABASE_URL`, `GEMINI_API_KEY`, and your AWS credentials for SQS.

### 3. Start the PostgreSQL Container

```bash
# Start only the database (no Redis or workers needed for local dev)
docker compose up postgres -d

# Verify it is healthy
docker compose ps
```

The database is available at `postgresql://scraper:scraper@localhost:5432/scraper`.

Tables are created automatically on first API startup via SQLAlchemy `create_all`.

### 4. Run the FastAPI Backend

```bash
uvicorn src.api:app --reload --host 0.0.0.0 --port 8000
```

Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

```bash
# Dispatch a monitoring job
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://competitor.com/pricing"],
    "use_browser": false,
    "max_pages": 10
  }'

# Poll job status
curl http://localhost:8000/jobs/<job_id>
```

### 5. Run the Streamlit Dashboard

In a separate terminal (with the FastAPI server already running):

```bash
# Open / unauthenticated (local dev)
streamlit run src/dashboard.py

# Protected (demo to client)
DASHBOARD_USERNAME=admin DASHBOARD_PASSWORD=signalpipe \
  streamlit run src/dashboard.py
```

Opens at [http://localhost:8501](http://localhost:8501).

### 6. Validate the Full Pipeline Locally

The `seed_test_data.py` script exercises every layer — database, upsert logic, price-drop delta trigger, and SQS (mocked in-process via `moto`) — without needing real AWS credentials:

```bash
# Postgres container must be running
python -m src.seed_test_data
```

Expected output on full pass:

```
  ✓ PASS  PostgreSQL connection
  ✓ PASS  Row exists: competitor-a.com/product/laptop-pro-x
  ✓ PASS  No duplicate row created on re-upsert
  ✓ PASS  Alert queue received 2 message(s)
  ✓ PASS  No alert fired for price INCREASE
  ✓ PASS  ai_fallback_used=True stored in DB
  ✓ PASS  send_message returned a MessageId
  ✓ PASS  Queue is empty after deletion
  ✓ PASS  get_dlq_count returns >= 1
  All N checks passed.
```

### 7. Full Stack via Docker Compose

```bash
# Spin up everything: Postgres + FastAPI + Worker (×2 replicas)
docker compose up --build

# Tear down (preserves database volume)
docker compose down
```

---

## Environment Variables

Generate the full template at any time:

```bash
python print_secrets_template.py   # → writes .env.example
```

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | **Yes** | PostgreSQL async DSN — `postgresql+asyncpg://user:pass@host:5432/db` |
| `AWS_ACCESS_KEY_ID` | **Yes** | AWS IAM access key |
| `AWS_SECRET_ACCESS_KEY` | **Yes** | AWS IAM secret key |
| `AWS_REGION` | **Yes** | AWS region (e.g. `us-east-1`) |
| `SQS_QUEUE_URL` | **Yes** | Main FIFO scraping queue URL (output of `terraform apply`) |
| `SQS_ALERT_QUEUE_URL` | **Yes** | Price-drop alert queue URL (output of `terraform apply`) |
| `SQS_DLQ_URL` | **Yes** | Dead-Letter Queue URL (output of `terraform apply`) |
| `GEMINI_API_KEY` | **Yes** | Google Gemini API key for LLM fallback extraction |
| `DASHBOARD_USERNAME` | No | Streamlit login username — leave blank to disable auth |
| `DASHBOARD_PASSWORD` | No | Streamlit login password — leave blank to disable auth |
| `API_BASE_URL` | No | URL the dashboard uses to reach FastAPI (default: `http://localhost:8000`) |
| `WEBHOOK_URL` | No | Default client webhook for price-drop alert delivery |
| `ECS_CLUSTER` | No | ECS cluster name for Fargate browser tasks |
| `ECS_TASK_DEFINITION` | No | Fargate task definition name |
| `ECS_SUBNETS` | No | Comma-separated subnet IDs for Fargate tasks |
| `ECS_SECURITY_GROUPS` | No | Comma-separated security group IDs for Fargate tasks |
| `PROXY_URL` | No | HTTP proxy for `DataFetcher` (e.g. `http://user:pass@host:8080`) |
| `APIFY_TOKEN` | No | Apify API token for marketplace actor deployment |

> **Security:** Never commit `.env` to version control. The `.gitignore` blocks it. Use AWS Secrets Manager or ECS task secrets for production deployments.

---

## Cloud Deployment (Terraform)

```bash
cd terraform
terraform init

terraform apply \
  -var="db_password=YOUR_DB_PASSWORD" \
  -var="api_image=YOUR_ECR_URI:latest" \
  -var="worker_image=YOUR_ECR_URI:latest" \
  -var="gemini_api_key=YOUR_GEMINI_KEY"
```

Provisions in a single `apply`:

| Resource | Type | Purpose |
|----------|------|---------|
| VPC + subnets | Networking | Isolated private network |
| RDS PostgreSQL 16 | `db.t4g.micro` | Persistent record storage |
| SQS Main Queue (FIFO) | + Redrive Policy | Scraping task messages |
| SQS Alert Queue | Standard | Price-drop event stream |
| SQS Dead-Letter Queue (FIFO) | 14-day retention | Failed URL quarantine |
| ECS Cluster + Fargate | API + Worker services | Containerised workloads |
| ALB | HTTPS load balancer | Public FastAPI endpoint |
| CloudWatch Logs | `/ecs/signalpipe` | Centralized log group |
| IAM Policy | SQS + ECS permissions | Least-privilege access |

---

## Cost Model

| Component | Idle Cost | Per-1,000-URLs |
|-----------|-----------|----------------|
| SQS (all 3 queues) | $0.00 | $0.001 |
| Lambda (standard fetch) | $0.00 | ~$0.20 |
| Fargate (browser fetch) | $0.00 | ~$2.00 |
| RDS db.t4g.micro | ~$12/mo | — |
| Gemini 2.0 Flash | $0.00 | ~$0.05 (only when triggered) |
| **Typical mixed workload** | **~$12/mo** | **~$0.25 / 1,000 URLs** |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.12+ |
| **UI** | Streamlit |
| **API** | FastAPI + Uvicorn |
| **Queue** | AWS SQS FIFO + Standard + DLQ |
| **Compute** | AWS Lambda · ECS Fargate |
| **Browser** | Playwright + playwright-stealth |
| **Extraction AI** | Google Gemini 2.0 Flash · Instructor · Pydantic |
| **Database** | PostgreSQL 16 (RDS) · SQLAlchemy Async · asyncpg |
| **HTTP Client** | Requests · Tenacity (retry) |
| **Delivery** | Webhook (Zapier / Make / Custom) |
| **Infrastructure** | Terraform · Docker · docker-compose |
| **Testing** | Pytest · moto (SQS mock) |
| **Marketplace** | Apify SDK |

---

## Running Tests

```bash
# Unit + integration tests
pytest tests/ -v

# Full end-to-end pipeline validation (requires postgres container)
docker compose up postgres -d
python -m src.seed_test_data
```

---

## License

MIT

---

<p align="center">
  <strong>Built by <a href="https://birukkasahun.com">Biruk Kasahun</a></strong><br/>
  <sub>Enterprise-grade competitor intelligence infrastructure.</sub>
</p>
