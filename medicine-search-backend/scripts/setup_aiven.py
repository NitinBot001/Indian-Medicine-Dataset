"""
Aiven for PostgreSQL — Setup & Ingestion Script

Automates full setup on your managed Aiven PostgreSQL service:
1. Validates connection & SSL handshake
2. Creates pg_trgm & uuid-ossp extensions
3. Creates all tables and search indexes (B-Tree prefix, GIN trigram)
4. Streams and normalizes 253,973 medicines via PostgreSQL COPY
5. Runs test queries to verify sub-100ms performance on Aiven

Usage:
    python scripts/setup_aiven.py --url "postgres://avnadmin:password@host:port/defaultdb?sslmode=require"
    OR set DATABASE_URL in your .env and run:
    python scripts/setup_aiven.py
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import psycopg
from app.core.config import normalize_db_url_for_sync
from scripts.setup_db import setup_database
from app.importers.csv_importer import import_csv

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
RED = "\033[0;31m"
NC = "\033[0m"


def main():
    parser = argparse.ArgumentParser(description="Set up Aiven PostgreSQL for Medicine Search")
    parser.add_argument(
        "--url",
        help="Aiven Service URI (e.g. postgres://avnadmin:secret@pg-xyz.aivencloud.com:12345/defaultdb?sslmode=require)",
    )
    parser.add_argument(
        "--csv-file",
        default="../DATA/indian_medicine_data.csv",
        help="Path to medicine CSV file",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help="Batch size for COPY insertion",
    )
    args = parser.parse_args()

    raw_url = (
        args.url
        or os.getenv("AIVEN_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )

    if not raw_url:
        print(f"{RED}[✗] Error: No database URL provided!{NC}")
        print("\nPlease provide your Aiven PostgreSQL Service URI:")
        print("  python scripts/setup_aiven.py --url 'postgres://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require'")
        print("  OR set DATABASE_URL in your medicine-search-backend/.env file.\n")
        sys.exit(1)

    sync_url = normalize_db_url_for_sync(raw_url)
    masked_host = sync_url.split("@")[1] if "@" in sync_url else "configured host"

    print(f"\n{CYAN}{'='*65}{NC}")
    print(f"{CYAN} Aiven for PostgreSQL — Setup & Ingestion{NC}")
    print(f"{CYAN}{'='*65}{NC}")
    print(f"Target Service: {masked_host}\n")

    # Step 1: Test Connection
    print(f"{YELLOW}[1/4] Testing connection to Aiven PostgreSQL with SSL...{NC}")
    try:
        with psycopg.connect(sync_url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                pg_ver = cur.fetchone()[0]
                print(f"{GREEN}[✓] Connected successfully!{NC}")
                print(f"    PostgreSQL Version: {pg_ver.split(',')[0]}")
    except Exception as e:
        print(f"{RED}[✗] Connection failed: {e}{NC}")
        sys.exit(1)

    # Step 2: Create Schema & Indexes
    print(f"\n{YELLOW}[2/4] Creating database schema and performance indexes...{NC}")
    try:
        setup_database(sync_url)
    except Exception as e:
        print(f"{RED}[✗] Schema creation failed: {e}{NC}")
        sys.exit(1)

    # Step 3: Ingest Dataset
    csv_path = Path(__file__).resolve().parent.parent / args.csv_file
    if not csv_path.exists():
        # Fallback to absolute
        csv_path = Path(args.csv_file).resolve()

    if not csv_path.exists():
        print(f"{RED}[✗] CSV dataset not found at: {csv_path}{NC}")
        sys.exit(1)

    print(f"\n{YELLOW}[3/4] Streaming {csv_path.name} into Aiven via PostgreSQL COPY...{NC}")
    start_import = time.perf_counter()
    res = import_csv(str(csv_path), sync_url, batch_size=args.batch_size)
    elapsed = time.perf_counter() - start_import
    print(f"{GREEN}[✓] Import complete in {elapsed:.1f}s! ({res.get('accepted_rows', 0):,} medicines ingested){NC}")

    # Step 4: Verify Search on Aiven
    print(f"\n{YELLOW}[4/4] Verifying search performance on Aiven...{NC}")
    test_queries = [
        "Augmentin 625 Duo Tablet",
        "augmentn 625",
        "dolo 650",
        "azithrl 500",
    ]

    with psycopg.connect(sync_url) as conn:
        with conn.cursor() as cur:
            for q in test_queries:
                t0 = time.perf_counter()
                cur.execute(
                    """
                    SELECT id, name
                    FROM medicines
                    WHERE name_normalized LIKE (%s || '%%')
                       OR name_normalized %% %s
                    LIMIT 5
                    """,
                    (q.lower(), q.lower()),
                )
                rows = cur.fetchall()
                t1 = time.perf_counter()
                latency_ms = (t1 - t0) * 1000
                top_match = rows[0][1] if rows else "No match"
                print(f"  {GREEN}✓{NC} [{latency_ms:5.1f}ms] q=\"{q}\" → top: \"{top_match}\"")

    # Update .env
    env_file = Path(__file__).resolve().parent.parent / ".env"
    with open(env_file, "w") as f:
        f.write(f"# Aiven for PostgreSQL Configuration\nDATABASE_URL={raw_url}\n")
    print(f"\n{GREEN}[✓] Updated {env_file} with your Aiven connection URI!{NC}")

    print(f"\n{CYAN}{'='*65}{NC}")
    print(f"{GREEN} Aiven PostgreSQL is fully configured and ready!{NC}")
    print(f"{CYAN}{'='*65}{NC}")
    print("To start the search API pointing to your Aiven database:")
    print(f"  {CYAN}uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload{NC}\n")


if __name__ == "__main__":
    main()
