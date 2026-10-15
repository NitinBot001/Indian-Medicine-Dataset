#!/usr/bin/env bash
# ==============================================================================
# Medicine Fuzzy Search — Full Setup & Launch Script
#
# This script:
# 1. Starts PostgreSQL via Docker Compose
# 2. Installs Python dependencies
# 3. Creates database schema (tables + GIN trigram indexes)
# 4. Imports the medicine CSV dataset (~254k records)
# 5. Starts the FastAPI server
# 6. Runs the search benchmark
#
# Usage:
#   chmod +x setup_and_run.sh
#   ./setup_and_run.sh
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }

# ──────────────────────────────────────────────────────────
# Step 1: Start PostgreSQL
# ──────────────────────────────────────────────────────────
log "Starting PostgreSQL via Docker Compose..."
docker compose up -d --wait 2>/dev/null || docker-compose up -d 2>/dev/null
sleep 3

# Wait for PostgreSQL to be ready
for i in {1..30}; do
    if docker compose exec -T db pg_isready -U medicine -d medicine_search > /dev/null 2>&1; then
        log "PostgreSQL is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        err "PostgreSQL failed to start after 30 seconds"
        exit 1
    fi
    sleep 1
done

# ──────────────────────────────────────────────────────────
# Step 2: Install Python dependencies
# ──────────────────────────────────────────────────────────
log "Installing Python dependencies..."
pip install -q fastapi[standard] pydantic pydantic-settings 'sqlalchemy[asyncio]' greenlet asyncpg psycopg[binary] uvicorn python-dotenv httpx 2>/dev/null

# ──────────────────────────────────────────────────────────
# Step 3: Create database schema
# ──────────────────────────────────────────────────────────
log "Creating database schema (tables + trigram indexes)..."
python scripts/setup_db.py

# Apply database performance tuning
docker compose exec -T db psql -U medicine -d medicine_search -c "
  ALTER DATABASE medicine_search SET pg_trgm.similarity_threshold = 0.25;
  ALTER DATABASE medicine_search SET random_page_cost = 1.1;
  ALTER DATABASE medicine_search SET work_mem = '32MB';
" 2>/dev/null || true

# ──────────────────────────────────────────────────────────
# Step 4: Import medicine dataset
# ──────────────────────────────────────────────────────────
# Use the original CSV (same row count, simpler columns)
CSV_FILE="../DATA/indian_medicine_data.csv"
if [ -f "$CSV_FILE" ]; then
    log "Importing medicine dataset from $CSV_FILE..."
    python -m app.importers.csv_importer --file "$CSV_FILE" --batch-size 10000
else
    err "CSV file not found at $CSV_FILE"
    exit 1
fi

# ──────────────────────────────────────────────────────────
# Step 5: Verify import
# ──────────────────────────────────────────────────────────
log "Verifying import..."
MEDICINE_COUNT=$(docker compose exec -T db psql -U medicine -d medicine_search -t -c "SELECT count(*) FROM medicines;" 2>/dev/null | tr -d ' ')
log "Imported ${MEDICINE_COUNT} medicines"

# Verify trigram indexes
log "Verifying trigram indexes..."
docker compose exec -T db psql -U medicine -d medicine_search -c "
  SELECT indexname, indexdef
  FROM pg_indexes
  WHERE tablename = 'medicines' AND indexdef LIKE '%trgm%';
" 2>/dev/null

# ──────────────────────────────────────────────────────────
# Step 6: Start FastAPI server
# ──────────────────────────────────────────────────────────
log "Starting FastAPI server on port 8000..."

# Kill any existing server on port 8000
kill $(lsof -t -i:8000 2>/dev/null) 2>/dev/null || true
sleep 1

uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 &
SERVER_PID=$!
sleep 3

# Check server is running
if ! kill -0 $SERVER_PID 2>/dev/null; then
    err "FastAPI server failed to start"
    exit 1
fi
log "FastAPI server running (PID: $SERVER_PID)"

# Quick smoke test
log "Smoke test..."
curl -s "http://localhost:8000/health" | python -m json.tool
echo ""
RESULT=$(curl -s "http://localhost:8000/api/v1/search?q=augmentin&limit=3")
echo "$RESULT" | python -m json.tool
echo ""

# ──────────────────────────────────────────────────────────
# Step 7: Run benchmark
# ──────────────────────────────────────────────────────────
log "Running search benchmark..."
python scripts/benchmark_search.py --url http://localhost:8000 --iterations 3

# ──────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────
log ""
log "================================================="
log " Medicine Fuzzy Search is READY!"
log "================================================="
log " API:       http://localhost:8000"
log " Docs:      http://localhost:8000/docs"
log " Search:    http://localhost:8000/api/v1/search?q=augmentin"
log " Health:    http://localhost:8000/health"
log " Benchmark: python scripts/benchmark_search.py"
log "================================================="
log ""
log "The server is running in the background (PID: $SERVER_PID)."
log "To stop: kill $SERVER_PID && docker compose down"
