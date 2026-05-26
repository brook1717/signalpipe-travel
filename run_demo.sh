#!/usr/bin/env bash
# run_demo.sh — SignalPipe local demo launcher
#
# Usage:
#   chmod +x run_demo.sh
#   ./run_demo.sh
#
# Starts PostgreSQL, validates the pipeline, then runs
# FastAPI (port 8000) and Streamlit (port 8501) side-by-side.
# Press Ctrl+C to cleanly shut down all processes.

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
echo -e "${CYAN}${BOLD}  ⚡ SignalPipe — Local Demo Launcher${RESET}"
echo -e "${CYAN}     Competitor Intelligence · Event-Driven · AI Self-Healing${RESET}"
_header
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Load .env if present (never required — all vars have defaults)
# ─────────────────────────────────────────────────────────────────────────────
if [[ -f ".env" ]]; then
    _info "Loading environment from .env"
    set -o allexport
    # shellcheck source=/dev/null
    source .env
    set +o allexport
else
    _warn ".env not found — using defaults. Run: python print_secrets_template.py"
fi

# Local DB URL must match docker-compose credentials (scraper:scraper)
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://scraper:scraper@localhost:5432/scraper}"

# ── Python resolver ──────────────────────────────────────────────────────────
# In WSL the system python3 has no project packages — use the Windows Python
# (accessible via WSL interop as python.exe) where pip just installed them.
if python.exe --version &>/dev/null; then
    PY="python.exe"
elif python3 --version &>/dev/null; then
    PY="python3"
else
    _err "No Python found. Install Python 3.12 or run: pip install -r requirements.txt"
    exit 1
fi
_info "Python interpreter: $($PY --version 2>&1) → $PY"

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: release ports 8000 and 8501 if held by a previous run
# ─────────────────────────────────────────────────────────────────────────────
_free_port() {
    local port=$1
    # Works from WSL: call netstat.exe + taskkill.exe via Windows interop
    local pids
    pids=$(netstat.exe -ano 2>/dev/null \
        | grep -E "0\.0\.0\.0:${port}|127\.0\.0\.1:${port}" \
        | grep "LISTENING" \
        | awk '{print $NF}' \
        | sort -u \
        || true)
    for pid in $pids; do
        [[ -z "$pid" || "$pid" == "0" ]] && continue
        taskkill.exe /PID "$pid" /F &>/dev/null && true \
            && _info "Released port ${port} (killed PID ${pid})."
    done
    return 0
}

_info "Checking for stale processes on ports 8000 and 8501..."
_free_port 8000
_free_port 8501
sleep 1

# ─────────────────────────────────────────────────────────────────────────────
# PID tracking — populated as processes start
# ─────────────────────────────────────────────────────────────────────────────
API_PID=""
STREAMLIT_PID=""

cleanup() {
    echo ""
    _header
    echo -e "${YELLOW}${BOLD}  Ctrl+C received — shutting down SignalPipe demo...${RESET}"
    _header

    if [[ -n "$STREAMLIT_PID" ]] && kill -0 "$STREAMLIT_PID" 2>/dev/null; then
        _step "Stopping Streamlit (PID $STREAMLIT_PID)"
        kill "$STREAMLIT_PID" 2>/dev/null && _ok "Streamlit stopped."
    fi

    if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
        _step "Stopping FastAPI (PID $API_PID)"
        kill "$API_PID" 2>/dev/null && _ok "FastAPI stopped."
    fi

    if [[ "${EXISTING_DB:-false}" == "false" ]]; then
        _step "Stopping PostgreSQL container"
        docker compose stop postgres 2>/dev/null && _ok "Postgres stopped."
    else
        _info "Postgres was pre-existing — leaving it running."
    fi

    echo ""
    _ok "Demo session ended cleanly."
    echo ""
    exit 0
}

trap cleanup SIGINT SIGTERM

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 1/4 · Starting PostgreSQL container"

# If something is already listening on 5432, skip docker compose up to avoid
# a "port already allocated" error (e.g. another project's postgres container).
if pg_isready -h localhost -p 5432 -q 2>/dev/null; then
    _warn "Port 5432 is already in use — skipping docker compose up."
    _info "Connecting to whichever Postgres is running on localhost:5432."
    _info "If the DB credentials differ, set DATABASE_URL in .env."

    # Try to ensure our schema exists on whatever is running
    EXISTING_DB=true
else
    EXISTING_DB=false
    docker compose up postgres -d

    _info "Waiting for Postgres to be ready..."
    MAX_WAIT=30
    ELAPSED=0
    CONTAINER=$(docker compose ps -q postgres 2>/dev/null | head -1)
    until [[ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null)" == "healthy" ]]; do
        if [[ $ELAPSED -ge $MAX_WAIT ]]; then
            _err "Postgres did not become healthy within ${MAX_WAIT}s."
            _err "Check: docker compose logs postgres"
            exit 1
        fi
        sleep 1
        ELAPSED=$((ELAPSED + 1))
        printf "."
    done
    [[ $ELAPSED -gt 0 ]] && echo ""
fi

_ok "PostgreSQL is ready."

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Pipeline validation
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 2/4 · Running pipeline validation suite (seed_test_data.py)"
_info "Tests: DB upsert · price-drop delta trigger · SQS round-trip (moto mock)"
echo ""

if DATABASE_URL="$DATABASE_URL" $PY -m src.seed_test_data; then
    echo ""
    _ok "All validation checks passed — pipeline is healthy."
else
    EXIT_CODE=$?
    echo ""
    _err "Validation suite reported failures (exit $EXIT_CODE)."
    _warn "Review the output above. Proceeding with demo anyway — fix failures before production."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — FastAPI backend
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 3/4 · Starting FastAPI backend on http://localhost:8000"

DATABASE_URL="$DATABASE_URL" $PY -m uvicorn src.api:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level warning \
    --no-access-log \
    &
API_PID=$!

# Wait up to 10s for the API to start accepting connections
API_READY=false
for i in $(seq 1 10); do
    if curl -sf http://localhost:8000/docs > /dev/null 2>&1; then
        API_READY=true
        break
    fi
    sleep 1
done

if $API_READY; then
    _ok "FastAPI is up (PID $API_PID) · http://localhost:8000/docs"
else
    _warn "FastAPI may still be starting. Check logs if the dashboard can't connect."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Streamlit dashboard
# ─────────────────────────────────────────────────────────────────────────────
_header
_step "Step 4/4 · Starting Streamlit dashboard on http://localhost:8501"

# Pass-through dashboard auth from .env (or exported env vars)
export DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-}"
export DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-}"
export API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"

DATABASE_URL="$DATABASE_URL" $PY -m streamlit run src/dashboard.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false \
    &
STREAMLIT_PID=$!

sleep 2

# ─────────────────────────────────────────────────────────────────────────────
# Ready — print access summary
# ─────────────────────────────────────────────────────────────────────────────
_header
echo -e "${GREEN}${BOLD}  ⚡ SignalPipe demo is live!${RESET}"
_header
echo ""
echo -e "  ${BOLD}Streamlit Dashboard${RESET}   →  ${CYAN}http://localhost:8501${RESET}"
echo -e "  ${BOLD}FastAPI Backend    ${RESET}   →  ${CYAN}http://localhost:8000${RESET}"
echo -e "  ${BOLD}API Interactive Docs${RESET}  →  ${CYAN}http://localhost:8000/docs${RESET}"
echo ""

if [[ -n "${DASHBOARD_USERNAME}" ]]; then
    echo -e "  ${BOLD}Login:${RESET}  username=${YELLOW}${DASHBOARD_USERNAME}${RESET}  password=${YELLOW}(from .env)${RESET}"
else
    echo -e "  ${YELLOW}  Auth disabled — set DASHBOARD_USERNAME + DASHBOARD_PASSWORD in .env to protect the demo.${RESET}"
fi

echo ""
echo -e "  ${BOLD}Press Ctrl+C to stop all services.${RESET}"
_header
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Block until Ctrl+C
# ─────────────────────────────────────────────────────────────────────────────
wait $API_PID $STREAMLIT_PID
