#!/usr/bin/env bash
# run_travel_demo.sh — SignalPipe Travel: one-click demo launcher
#
# Usage:
#   chmod +x run_travel_demo.sh
#   ./run_travel_demo.sh
#
# Flow:
#   1. Start PostgreSQL container
#   2. Wait 3 s, then run the 35-assertion test suite (seed_test_data.py)
#   3. If all tests pass → launch FastAPI on :8000 (background)
#   4. Launch Streamlit dashboard on :8501
#   5. Ctrl+C cleanly kills both background services

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────────────────────────────────────
BOLD="\033[1m"
CYAN="\033[96m"
GREEN="\033[92m"
YELLOW="\033[93m"
RED="\033[91m"
RESET="\033[0m"

_header() { echo -e "\n${CYAN}${BOLD}──────────────────────────────────────────────────${RESET}"; }
_step()   { echo -e "${GREEN}${BOLD}  ▶  $*${RESET}"; }
_info()   { echo -e "${CYAN}     $*${RESET}"; }
_warn()   { echo -e "${YELLOW}  ⚠  $*${RESET}"; }
_ok()     { echo -e "${GREEN}  ✓  $*${RESET}"; }
_err()    { echo -e "${RED}${BOLD}  ✗  $*${RESET}"; }

# ─────────────────────────────────────────────────────────────────────────────
# Banner
# ─────────────────────────────────────────────────────────────────────────────
clear
_header
echo -e "${CYAN}${BOLD}  ✈  SignalPipe Travel — B2B Price Protection Engine${RESET}"
echo -e "${CYAN}     Monitor Bookings · Beat Cancellation Deadlines · Recover Savings${RESET}"
_header
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Load .env if present
# ─────────────────────────────────────────────────────────────────────────────
if [[ -f ".env" ]]; then
    _info "Loading environment from .env"
    set -o allexport
    # shellcheck source=/dev/null
    source .env
    set +o allexport
else
    _warn ".env not found — using defaults (sufficient for local demo)."
    _warn "Copy .env.example to .env and fill in PROXY_URL + GEMINI_API_KEY for live scraping."
fi

export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://scraper:scraper@localhost:5432/scraper}"
export API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
export DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-}"
export DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-}"

# ─────────────────────────────────────────────────────────────────────────────
# Python resolver  (handles WSL where system python3 lacks project packages)
# ─────────────────────────────────────────────────────────────────────────────
if python.exe --version &>/dev/null 2>&1; then
    PY="python.exe"
elif python3 --version &>/dev/null 2>&1; then
    PY="python3"
elif python --version &>/dev/null 2>&1; then
    PY="python"
else
    _err "No Python interpreter found. Install Python 3.12+ and run: pip install -r requirements.txt"
    exit 1
fi
_info "Python: $($PY --version 2>&1)  →  using '$PY'"

# ─────────────────────────────────────────────────────────────────────────────
# PID tracking
# ─────────────────────────────────────────────────────────────────────────────
API_PID=""
STREAMLIT_PID=""

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup — kills background services on Ctrl+C or EXIT
# ─────────────────────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    _header
    echo -e "${YELLOW}${BOLD}  Shutting down SignalPipe Travel demo...${RESET}"
    _header

    if [[ -n "$STREAMLIT_PID" ]] && kill -0 "$STREAMLIT_PID" 2>/dev/null; then
        _step "Stopping Streamlit (PID $STREAMLIT_PID)"
        kill "$STREAMLIT_PID" 2>/dev/null || true
        _ok "Streamlit stopped."
    fi

    if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
        _step "Stopping FastAPI (PID $API_PID)"
        kill "$API_PID" 2>/dev/null || true
        _ok "FastAPI stopped."
    fi

    _step "Stopping PostgreSQL container"
    docker compose stop postgres 2>/dev/null || true
    _ok "Postgres stopped."

    echo ""
    _ok "Demo session ended cleanly."
    echo ""
}

trap cleanup SIGINT SIGTERM

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 1 / 4  ·  Starting PostgreSQL container"

docker compose up postgres -d

_info "Waiting 3 seconds for Postgres to initialise..."
sleep 3
_ok "PostgreSQL is ready."

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — 35-assertion test suite
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 2 / 4  ·  Running 35-assertion validation suite"
_info "Covers: DB schema · booking insertion · deadline guard · delta engine · Gemini parser · SQS round-trip"
_info "SQS is mocked in-process via moto — no real AWS credentials required."
echo ""

if DATABASE_URL="$DATABASE_URL" $PY -m src.seed_test_data; then
    echo ""
    _ok "35 / 35 assertions passed — pipeline is healthy. Proceeding to launch."
else
    TEST_EXIT=$?
    echo ""
    _err "Test suite failed (exit $TEST_EXIT). Fix the failures above before running the demo."
    _info "Start Postgres manually: docker compose up postgres -d"
    _info "Then re-run tests:       python -m src.seed_test_data"
    cleanup
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — FastAPI backend (background)
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 3 / 4  ·  Starting FastAPI backend on http://localhost:8000"

DATABASE_URL="$DATABASE_URL" $PY -m uvicorn src.api:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --log-level warning \
    --no-access-log \
    &
API_PID=$!

# Poll until the API responds (up to 15 s)
API_READY=false
for i in $(seq 1 15); do
    if curl -sf http://localhost:8000/openapi.json > /dev/null 2>&1; then
        API_READY=true
        break
    fi
    sleep 1
done

if $API_READY; then
    _ok "FastAPI is live (PID $API_PID)"
    _info "Interactive docs → http://localhost:8000/docs"
else
    _warn "FastAPI is taking longer than expected to start — check for import errors."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Streamlit dashboard (foreground-blocking)
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 4 / 4  ·  Starting Streamlit dashboard on http://localhost:8501"

DATABASE_URL="$DATABASE_URL" $PY -m streamlit run src/dashboard.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false \
    &
STREAMLIT_PID=$!

sleep 2

# ─────────────────────────────────────────────────────────────────────────────
# Ready summary
# ─────────────────────────────────────────────────────────────────────────────
_header
echo -e "${GREEN}${BOLD}  ✈  SignalPipe Travel demo is live!${RESET}"
_header
echo ""
echo -e "  ${BOLD}Agent Dashboard  ${RESET}  →  ${CYAN}http://localhost:8501${RESET}"
echo -e "  ${BOLD}FastAPI Backend  ${RESET}  →  ${CYAN}http://localhost:8000${RESET}"
echo -e "  ${BOLD}API Docs (Swagger)${RESET} →  ${CYAN}http://localhost:8000/docs${RESET}"
echo ""

if [[ -n "${DASHBOARD_USERNAME}" ]]; then
    echo -e "  ${BOLD}Login:${RESET}  username=${YELLOW}${DASHBOARD_USERNAME}${RESET}  password=${YELLOW}(from .env)${RESET}"
else
    echo -e "  ${YELLOW}  Auth disabled — set DASHBOARD_USERNAME + DASHBOARD_PASSWORD in .env to protect the demo.${RESET}"
fi

if [[ -z "${PROXY_URL:-}" ]]; then
    echo ""
    _warn "PROXY_URL is not set. Live scraping against Booking.com / Expedia will be blocked."
    _info "Set PROXY_URL in .env with your Bright Data or Webshare residential proxy URL."
fi

echo ""
echo -e "  ${BOLD}Press Ctrl+C to stop all services.${RESET}"
_header
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Block until Ctrl+C
# ─────────────────────────────────────────────────────────────────────────────
wait $API_PID $STREAMLIT_PID
