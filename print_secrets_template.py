"""Generate a .env.example file with every environment variable required by the system.

Run: python print_secrets_template.py
"""

ENV_TEMPLATE = """\
# ============================================================
# Multi-Source Data Scraper — Environment Variables
# ============================================================
# Copy this file to .env and fill in your values.
# NEVER commit the actual .env file to version control.
# ============================================================

# ----------------------------------------------------------
# AWS Credentials & Region
# ----------------------------------------------------------
AWS_ACCESS_KEY_ID=your-aws-access-key-id
AWS_SECRET_ACCESS_KEY=your-aws-secret-access-key
AWS_REGION=us-east-1

# ----------------------------------------------------------
# AWS SQS (Message Queue)
# ----------------------------------------------------------
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123456789012/scraper-tasks.fifo

# ----------------------------------------------------------
# AWS ECS / Fargate (Browser Worker)
# ----------------------------------------------------------
ECS_CLUSTER=scraper-cluster
ECS_TASK_DEFINITION=scraper-worker
ECS_SUBNETS=subnet-0abc123,subnet-0def456
ECS_SECURITY_GROUPS=sg-0abc123456

# ----------------------------------------------------------
# PostgreSQL Database
# ----------------------------------------------------------
DATABASE_URL=postgresql+asyncpg://scraper:your-db-password@your-rds-endpoint:5432/scraper

# ----------------------------------------------------------
# Gemini AI (LLM Fallback — Self-Healing Parser)
# ----------------------------------------------------------
GEMINI_API_KEY=your-gemini-api-key

# ----------------------------------------------------------
# Webhook Delivery (Optional — Premium Client Feature)
# ----------------------------------------------------------
WEBHOOK_URL=https://hooks.zapier.com/hooks/catch/123456/abcdef/

# ----------------------------------------------------------
# Streamlit Dashboard Auth (leave blank to disable)
# ----------------------------------------------------------
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change-me-before-demo

# ----------------------------------------------------------
# FastAPI Server
# ----------------------------------------------------------
API_HOST=0.0.0.0
API_PORT=8000

# ----------------------------------------------------------
# Apify Actor (Optional — Marketplace Deployment)
# ----------------------------------------------------------
APIFY_TOKEN=your-apify-api-token

# ----------------------------------------------------------
# Proxy Configuration (Optional)
# ----------------------------------------------------------
PROXY_URL=http://user:pass@proxy-host:8080
"""


def main():
    output_path = ".env.example"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ENV_TEMPLATE)
    print(f"✓ Generated {output_path} with all required environment variables.")
    print(f"  Copy to .env and fill in your secrets before deployment.")


if __name__ == "__main__":
    main()
