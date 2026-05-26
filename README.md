<div align="center">

# SignalPipe Travel: B2B Price Protection Engine

### Monitor Bookings · Beat Cancellation Deadlines · Recover Client Savings Automatically

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![AWS Fargate](https://img.shields.io/badge/AWS-Fargate-FF9900.svg?style=flat&logo=amazon-ecs&logoColor=white)](https://aws.amazon.com/fargate/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Stealth-2EAD33.svg?style=flat&logo=playwright&logoColor=white)](https://playwright.dev/)
[![Streamlit](https://img.shields.io/badge/Streamlit-Dashboard-FF4B4B.svg?style=flat&logo=streamlit&logoColor=white)](https://streamlit.io/)
[![Gemini AI](https://img.shields.io/badge/Gemini-2.0%20Flash-4285F4.svg?style=flat&logo=google&logoColor=white)](https://ai.google.dev/)
[![Terraform](https://img.shields.io/badge/Terraform-IaC-7B42BC.svg?style=flat&logo=terraform&logoColor=white)](https://www.terraform.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**A production-grade, event-driven engine that watches active travel bookings 24/7, bypasses Cloudflare and Akamai bot-protection on Booking.com, Expedia, and Marriott, and automatically alerts agents to re-book when prices drop — before the cancellation deadline expires.**

[Overview](#overview) · [Architecture](#architecture) · [Core Features](#core-features) · [Local Deployment](#local-deployment) · [Environment Variables](#environment-variables) · [Cloud Deployment](#cloud-deployment)

</div>

---

## Overview

Travel agencies book hotels, flights, and tour packages weeks or months in advance at today's rates. By the time a client travels, the price has often dropped — sometimes by hundreds of dollars — but the agent never finds out because no one is watching.

**SignalPipe Travel solves this.** It registers every active booking in a PostgreSQL database, scrapes the live provider page on a recurring schedule, and fires a structured re-booking alert the moment it detects a qualifying price drop — as long as the cancellation window is still open.

### The business problem it solves

| Without SignalPipe | With SignalPipe |
|---|---|
| Agent books hotel at $850/night | Booking registered: provider URL, booked rate, cancellation deadline |
| Price later drops to $650/night | System detects $200 drop (exceeds $50 alert threshold) |
| Agent never finds out | Agent receives Slack / webhook alert within minutes |
| Client pays the original $850 | Agent cancels and re-books — client saves $200/night |
| Cancellation deadline silently passes | Deadline guard marks booking as expired before scraper wastes effort |

### Why standard scrapers fail on travel sites

Booking.com, Expedia, and Marriott run **Cloudflare and Akamai bot-protection** that blocks all AWS data-center IPs instantly. SignalPipe routes every Playwright browser request through a **residential proxy network** (Bright Data, Webshare), making traffic indistinguishable from a real desktop user. When a site layout changes and structured selectors break, **Gemini 2.0 Flash** parses the raw HTML as a fallback — so scraping never silently fails.

---

## Architecture

```
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                     AGENT-FACING LAYER                                   │
  │                                                                          │
  │   Streamlit Agent Dashboard  (src/dashboard.py)                         │
  │   ├── Live Monitor  — itinerary table, savings column, countdown timer  │
  │   ├── Add Booking   — single booking registration form                  │
  │   └── Batch Upload  — CSV manifest parser + bulk insert                 │
  └──────────────────────────────┬──────────────────────────────────────────┘
                                  │  REST (POST /bookings, PATCH /bookings/*)
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    FastAPI  (src/api.py)                                  │
  │   POST /bookings            — register a new active booking              │
  │   GET  /bookings            — list all monitored bookings                │
  │   PATCH /bookings/{id}/rate — manual rate update trigger                 │
  │   PATCH /bookings/{id}/status — mark as rebooked / expired               │
  └──────────────────────────────┬───────────────────────────────────────────┘
                                  │  Upsert → active_bookings (PostgreSQL)
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │              PostgreSQL  (src/db/)                                        │
  │   active_bookings  — booking_ref, booked_rate, cancellation_deadline,    │
  │                      savings_threshold, status, provider_url             │
  │   rate_snapshots   — per-scrape record, threshold_met, alert_triggered   │
  └──────────┬───────────────────────────────────────────────────────────────┘
             │  Celery / Fargate worker polls on schedule
             ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │              Scraping Worker  (src/tasks.py)                              │
  │                                                                          │
  │   1. expire_lapsed_bookings()  ← Cancellation Deadline Guard            │
  │      Skips any booking whose cancellation_deadline has passed            │
  │                                                                          │
  │   2. BrowserFetcher  (Playwright + stealth + residential proxy)          │
  │      Fetches live provider page; bypasses Cloudflare / Akamai            │
  │                                                                          │
  │   3. Two-stage extraction:                                               │
  │      Stage 1 — BeautifulSoup structured selectors   (free, ~5 ms)       │
  │      Stage 2 — Gemini 2.0 Flash AI fallback         (HTML→MD, Pydantic) │
  │                                                                          │
  │   4. Financial Delta Engine                                              │
  │      savings = booked_rate − current_rate                                │
  │      if savings ≥ savings_threshold  →  push to SQS price-alerts        │
  └──────────────────────────────┬───────────────────────────────────────────┘
                                  │
               ┌──────────────────┴───────────────────────┐
               ▼                                           ▼
  ┌────────────────────────────┐          ┌──────────────────────────────┐
  │  SQS  price-alerts  queue  │          │  rate_snapshots  (PostgreSQL) │
  │  Structured alert payload  │          │  threshold_met, alert_triggered│
  │  with Markdown message     │          │  current_rate written to      │
  └────────────────┬───────────┘          │  active_bookings              │
                   │                      └──────────────────────────────┘
                   ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │              WebhookDeliverer  (src/delivery.py)                          │
  │   Exponential-backoff retry  ·  Structured Markdown alert body           │
  │   🚨 PRICE PROTECTION ALERT                                              │
  │   Booking: SP-NYC-MAR-2025 | Current: $650 | You Booked: $800            │
  │   Save $150.00 | Deadline: 15 days remaining                             │
  └──────────────────────────┬───────────────────────────────────────────────┘
                              │
                              ▼
              Slack Webhook  /  Zapier  /  Make  /  Custom endpoint
```

### Source Tree

```
├── src/
│   ├── api.py               # FastAPI — booking CRUD endpoints
│   ├── dashboard.py         # Streamlit UI — Live Monitor, Add Booking, Batch Upload
│   ├── fetcher.py           # DataFetcher + BrowserFetcher (Playwright + residential proxy)
│   ├── ai_parser.py         # Gemini 2.0 Flash — HTML→MD, structured extraction, validator
│   ├── tasks.py             # Celery tasks — deadline guard + scrape + delta engine
│   ├── queue_manager.py     # SQS: send / poll / delete + price-alerts queue helpers
│   ├── delivery.py          # WebhookDeliverer — retry, backoff, Markdown payload
│   ├── proxy_manager.py     # Round-robin proxy file loader
│   ├── seed_test_data.py    # 6-step automated validation suite (moto + unittest.mock)
│   ├── logger.py            # Centralized structured logging
│   └── db/
│       ├── database.py      # Async SQLAlchemy engine + session factory
│       ├── models.py        # ActiveBooking + RateSnapshot ORM models
│       └── crud.py          # upsert_booking, delta engine, expire_lapsed_bookings
├── terraform/
│   ├── main.tf              # VPC, RDS, ECS Fargate, SQS price-alerts queue, ALB
│   ├── variables.tf
│   └── outputs.tf           # Queue URLs, API endpoint, RDS endpoint
├── Dockerfile               # Multi-stage: api target + worker target (MS Playwright base)
├── docker-compose.yml       # Local: Postgres + FastAPI + Celery worker
├── .env.example             # All required env vars — copy to .env to configure
├── print_secrets_template.py # Regenerates .env.example from source of truth
└── requirements.txt
```

---

## Core Features

### 1. Cancellation Deadline Guard

Before the scraping worker fetches a single page, it calls `expire_lapsed_bookings()` — a database function that checks every `active_booking`'s `cancellation_deadline` against the current UTC timestamp. Any booking whose window has already closed is immediately marked `expired_cancellation_passed` and **excluded from the scraping run**.

This prevents the engine from wasting residential proxy bandwidth on bookings that can no longer be re-booked, and ensures agents are never sent a useless alert for an expired itinerary.

```
Worker starts scrape cycle
         │
         ▼
expire_lapsed_bookings()
         │
    deadline < NOW?
         │
   YES ──┴── NO
    │          │
    │    Include in scrape
    │
Mark status = 'expired_cancellation_passed'
Skip URL — no proxy request made, no alert sent
```

```sql
-- View all bookings whose window has passed
SELECT booking_ref, traveller_name, cancellation_deadline
FROM   active_bookings
WHERE  status = 'expired_cancellation_passed'
ORDER  BY cancellation_deadline DESC;
```

---

### 2. Financial Delta Engine

Every time a provider page is scraped, the engine computes the savings against the original booked rate and compares it to the per-booking `savings_threshold`. Only drops that clear the threshold trigger an SQS alert — sub-threshold fluctuations are recorded in `rate_snapshots` but produce no noise for the agent.

```
Live price fetched: $650
Booked rate:        $800
─────────────────────────
Savings:            $150   ← booked_rate − current_rate
Threshold:          $50    ← savings_threshold on active_bookings

$150 ≥ $50  →  ALERT FIRES
```

The SQS message body includes a pre-formatted **Markdown alert** ready for Slack or any webhook consumer:

```
🚨 *PRICE PROTECTION ALERT*

*Booking:* SP-NYC-MAR-2025 | *Traveller:* Jane Smith
*Provider:* https://booking.com/hotel/new-york-grand
*Room/Class:* Deluxe King

*You booked at:*  $800.00
*Live price now:* $650.00
*You could save:* $150.00

⏰ *Cancellation deadline:* 2025-03-10 — 15 days remaining

Re-book before the deadline to lock in the saving.
```

The `rate_snapshots` table records `threshold_met` and `alert_triggered` flags for every scrape cycle, giving full auditability without re-sending duplicate alerts.

---

### 3. AI Fallback Parsing (Gemini 2.0 Flash)

Travel provider sites frequently A/B test their layouts. When BeautifulSoup selectors fail to extract a structured price, the engine falls back to **Gemini 2.0 Flash** — never speculatively, only on extraction failure.

| Stage | Method | Cost | When |
|---|---|---|---|
| **Stage 1** | BeautifulSoup structured selectors | Free (~5 ms) | Always first |
| **Stage 2** | Gemini 2.0 Flash + Instructor + Pydantic | ~$0.001/page | Only when Stage 1 returns no price |

Before sending to the LLM, raw HTML is converted to Markdown — stripping `<nav>`, `<footer>`, `<script>`, and `<style>` tags — reducing token count by approximately 80%.

The `TravelBookingExtraction` Pydantic schema enforces structured output:

```python
class TravelBookingExtraction(BaseModel):
    total_price: Decimal
    currency: str          # ISO-4217
    check_in: date | None
    check_out: date | None
    inventory_status: Literal["available", "limited", "sold_out"]
```

Extractions with negative `total_price` or invalid `currency` are rejected by `_validate_extractions()` before any database write — the AI can hallucinate, the validator catches it.

---

## Local Deployment

### Prerequisites

- Python 3.12+
- Docker Desktop (for the PostgreSQL container)

### 1. Clone & Install

```bash
git clone https://github.com/brook1717/signalpipe-travel.git
cd signalpipe-travel

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# .env.example is already committed — copy it and fill in your values
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

At minimum for local development, set `DATABASE_URL` and `GEMINI_API_KEY`. AWS credentials and `PROXY_URL` are only required for live scraping against real travel sites.

### 3. Start the PostgreSQL Container

```bash
# Start only the database — no Redis or workers needed for schema + test validation
docker compose up postgres -d

# Confirm the container is healthy
docker compose ps
```

The local database is available at `postgresql://scraper:scraper@localhost:5432/scraper`.  
Tables (`active_bookings`, `rate_snapshots`) are created automatically on first API startup.

### 4. Run the 6-Step Validation Suite

The `seed_test_data.py` script runs a complete automated test of every system layer — DB schema, booking insertion, deadline expiry, delta engine, Gemini parser, and full SQS round-trip — without needing real AWS credentials (SQS is mocked in-process via `moto`):

```bash
# PostgreSQL container must be running
python -m src.seed_test_data
```

Expected output on full pass:

```
[STEP 0]  DB Connectivity + Schema
  ✓ PASS  PostgreSQL connection established
  ✓ PASS  Table 'active_bookings' exists
  ✓ PASS  Table 'rate_snapshots' exists
  ✓ PASS  All required columns present on active_bookings

[STEP 1]  Booking Insertion + Idempotency
  ✓ PASS  SINGLE-001 inserted and retrieved
  ✓ PASS  booked_rate stored as $499.00
  ✓ PASS  Default status is 'monitoring'
  ✓ PASS  All 3 batch rows inserted
  ✓ PASS  Decimal precision preserved: $280.75
  ✓ PASS  Re-upsert did not create duplicate row

[STEP 2]  Cancellation Deadline Guard
  ✓ PASS  EXPIRED-001 starts as 'monitoring'
  ✓ PASS  expire_lapsed_bookings() returns 0 active bookings
  ✓ PASS  Status flipped to 'expired_cancellation_passed'
  ✓ PASS  Scraper correctly bypasses expired provider URL

[STEP 3]  Financial Delta Engine  [moto SQS]
  ✓ PASS  Alert enqueued for $155 drop (threshold $50)
  ✓ PASS  Alert message contains valid event type
  ✓ PASS  Savings amount correct: $155.00
  ✓ PASS  Markdown alert opens with 🚨 and contains booking ref
  ✓ PASS  RateSnapshot: threshold_met=True, alert_triggered=True
  ✓ PASS  No alert for $25 sub-threshold drop
  ✓ PASS  No alert for price rise

[STEP 4]  Gemini Parser Resilience  [unittest.mock]
  ✓ PASS  HTML→Markdown strips nav/footer/script
  ✓ PASS  extract_with_llm returns [] gracefully when GEMINI_API_KEY empty
  ✓ PASS  Mocked LLM returns 1 extraction with total_price=$685.00
  ✓ PASS  Validator passes valid extraction
  ✓ PASS  Validator rejects negative total_price

[STEP 5]  Full Round-Trip  [moto SQS + requests mock]
  ✓ PASS  RT-001 alert appears on SQS queue
  ✓ PASS  Polled payload contains Markdown alert body
  ✓ PASS  Message contains booking ref and Save $150.00
  ✓ PASS  WebhookDeliverer.deliver() called once, returned True
  ✓ PASS  DB status updated to 'rebooked', current_rate=$650.00
  ✓ PASS  Queue empty after delete_message

  35 / 35 checks passed. Exit 0.
```

Script exits `0` on full pass, `1` on any single failure.

### 5. Launch the FastAPI Backend

```bash
uvicorn src.api:app --reload --host 0.0.0.0 --port 8000
```

Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

```bash
# Register a new booking
curl -X POST http://localhost:8000/bookings \
  -H "Content-Type: application/json" \
  -d '{
    "booking_ref":          "SP-NYC-MAR-2025",
    "traveller_name":       "Jane Smith",
    "provider_url":         "https://booking.com/hotel/new-york-grand",
    "booked_rate":          800.00,
    "room_or_ticket_class": "Deluxe King",
    "cancellation_deadline":"2025-03-10T00:00:00",
    "savings_threshold":    50.00
  }'

# List all monitored bookings
curl http://localhost:8000/bookings
```

### 6. Launch the Streamlit Agent Dashboard

In a separate terminal (API must already be running):

```bash
# Open / unauthenticated  (local dev)
streamlit run src/dashboard.py

# Password-protected  (client demo)
DASHBOARD_USERNAME=admin DASHBOARD_PASSWORD=signalpipe \
  streamlit run src/dashboard.py
```

Opens at [http://localhost:8501](http://localhost:8501).

The **Live Monitor** tab shows every active booking with live savings calculation and a countdown to cancellation deadline. The **Add Booking** tab provides a single-booking form. The **Batch Upload** tab accepts a CSV manifest for bulk ingestion.

### 7. Full Stack via Docker Compose

```bash
# Spin up Postgres + FastAPI + Celery worker
docker compose up --build

# Tear down (database volume is preserved)
docker compose down
```

---

## Environment Variables

The `.env.example` file in the repository root documents every variable. Copy it to `.env` and fill in your values:

```bash
copy .env.example .env     # Windows
# cp .env.example .env     # macOS / Linux
```

To regenerate `.env.example` from the canonical source at any time:

```bash
python print_secrets_template.py
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | **Yes** | Async PostgreSQL DSN — `postgresql+asyncpg://user:pass@host:5432/db` |
| `AWS_ACCESS_KEY_ID` | **Yes** | AWS IAM access key |
| `AWS_SECRET_ACCESS_KEY` | **Yes** | AWS IAM secret key |
| `AWS_REGION` | **Yes** | AWS region (e.g. `us-east-1`) |
| `SQS_QUEUE_URL` | **Yes** | Main scraping task queue URL |
| `SQS_ALERT_QUEUE_URL` | **Yes** | Price-drop alert queue URL |
| `SQS_DLQ_URL` | **Yes** | Dead-Letter Queue URL |
| `GEMINI_API_KEY` | **Yes*** | Google Gemini API key — required for AI fallback parsing. Leave blank to disable the fallback (Stage 1 only). |
| `PROXY_URL` | **Yes*** | Residential proxy URL — **required for live scraping** against Booking.com, Expedia, and Marriott. Data-center IPs are blocked instantly by their bot-protection. Format: `http://username:password@proxy-host:8080`. Leave blank for local / mocked testing only. |
| `REDIS_URL` | **Yes** (worker) | Redis broker URL for Celery — `redis://localhost:6379/0` |
| `DASHBOARD_USERNAME` | No | Streamlit login username — leave blank to disable auth |
| `DASHBOARD_PASSWORD` | No | Streamlit login password — leave blank to disable auth |
| `API_BASE_URL` | No | URL the Streamlit dashboard uses to reach FastAPI (default: `http://localhost:8000`) |
| `WEBHOOK_URL` | No | Default client webhook for alert delivery (Slack / Zapier / Make) |
| `ECS_CLUSTER` | No | ECS cluster name for Fargate browser tasks |
| `ECS_TASK_DEFINITION` | No | Fargate task definition name |
| `ECS_SUBNETS` | No | Comma-separated subnet IDs for Fargate tasks |
| `ECS_SECURITY_GROUPS` | No | Comma-separated security group IDs for Fargate tasks |
| `APIFY_TOKEN` | No | Apify API token for marketplace actor deployment |

> **Proxy providers:** [Bright Data](https://brightdata.com) and [Webshare](https://webshare.io) both offer residential proxy pools with per-GB pricing. Bright Data's format: `http://brd-customer-CXXXXXXX-zone-residential:PASSWORD@brd.superproxy.io:22225`

> **Security:** Never commit `.env` to version control. The `.gitignore` already blocks it. Use AWS Secrets Manager or ECS task definition secrets for production.

---

## Cloud Deployment (Terraform)

```bash
cd terraform
terraform init

terraform apply \
  -var="db_password=YOUR_DB_PASSWORD" \
  -var="api_image=YOUR_ECR_URI:latest" \
  -var="worker_image=YOUR_ECR_URI:latest" \
  -var="gemini_api_key=YOUR_GEMINI_KEY" \
  -var="proxy_url=http://user:pass@proxy-host:8080"
```

Provisions in a single `apply`:

| Resource | Type | Purpose |
|---|---|---|
| VPC + subnets | Networking | Isolated private network |
| RDS PostgreSQL 16 | `db.t4g.micro` | `active_bookings` + `rate_snapshots` |
| SQS price-alerts | Standard queue | Price-drop event stream |
| ECS Cluster + Fargate | API + Worker services | Containerised workloads |
| ALB | HTTPS load balancer | Public FastAPI endpoint |
| CloudWatch Logs | `/ecs/signalpipe-travel` | Centralised log group |
| IAM Policy | SQS + ECS permissions | Least-privilege access |

The Dockerfile uses the **official Microsoft Playwright Python image** (`mcr.microsoft.com/playwright/python:v1.44.0-jammy`) as its base, so Chromium and all browser OS dependencies are pre-installed — no manual `apt-get` steps in the build.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Language** | Python 3.12+ |
| **Agent UI** | Streamlit |
| **API** | FastAPI + Uvicorn |
| **Task Queue** | AWS SQS + Celery + Redis |
| **Compute** | ECS Fargate (API + Worker) |
| **Browser** | Playwright + playwright-stealth + Residential Proxy |
| **Extraction AI** | Google Gemini 2.0 Flash + Instructor + Pydantic |
| **Database** | PostgreSQL 16 (RDS) + SQLAlchemy Async + asyncpg |
| **Alert Delivery** | Webhook (Slack / Zapier / Make) with exponential-backoff retry |
| **Infrastructure** | Terraform + Docker + docker-compose |
| **Testing** | moto (SQS mock) + unittest.mock (Gemini / requests) |

---

## License

MIT

---

<p align="center">
  <strong>Built by <a href="https://birukkasahun.com">Biruk Kasahun</a></strong><br/>
  <sub>Enterprise-grade travel price protection infrastructure for B2B agencies.</sub>
</p>
