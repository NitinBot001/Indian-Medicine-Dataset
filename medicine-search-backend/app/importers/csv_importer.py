"""
CSV Importer for the Indian Medicine Dataset.

Implements plan.md Section 8 — Dataset ingestion workflow:
- Stream CSV rows in chunks (not full file in memory)
- Validate required columns
- Normalize fields for search indexing
- Track import provenance (source, hash, version)
- Idempotent: re-import of same hash is a no-op

Usage:
    python -m app.importers.csv_importer --file /path/to/csv [--batch-size 5000]
"""

import csv
import hashlib
import io
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# Add parent to path for direct script execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.normalization import (
    normalize_medicine_name,
    normalize_manufacturer,
    normalize_composition,
)

REQUIRED_COLUMNS = {"id", "name"}
EXPECTED_COLUMNS = {
    "id", "name", "price(₹)", "price", "Is_discontinued",
    "manufacturer_name", "type", "pack_size_label",
    "short_composition1", "short_composition2",
}

IMPORTER_VERSION = "1.0.0"


def compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_row_hash(row: dict) -> str:
    """Compute a hash of a row's content for change detection."""
    content = "|".join(str(row.get(k, "")) for k in sorted(row.keys()))
    return hashlib.md5(content.encode()).hexdigest()


def parse_price(value: str | None) -> float | None:
    """Safely parse price value."""
    if not value or value.strip() == "":
        return None
    try:
        return round(float(value.strip()), 2)
    except (ValueError, TypeError):
        return None


def parse_discontinued(value: str | None) -> bool:
    """Parse Is_discontinued field."""
    if not value:
        return False
    return value.strip().upper() in ("TRUE", "1", "YES")


def import_csv(
    file_path: str,
    database_url: str,
    batch_size: int = 5000,
    source_name: str = "indian_medicine_dataset",
) -> dict:
    """
    Import a CSV file into the medicines table.

    Uses synchronous psycopg for bulk import (COPY-style performance).
    Returns import statistics.
    """
    file_path = str(Path(file_path).resolve())
    file_hash = compute_file_hash(file_path)

    # Strip SQLAlchemy driver prefix if present
    if database_url.startswith("postgresql+"):
        database_url = "postgresql" + database_url[database_url.index("://"):]

    # Connect synchronously for bulk import
    conn = psycopg.connect(database_url, row_factory=dict_row)

    try:
        with conn.cursor() as cur:
            # Check if this exact file was already imported
            cur.execute(
                "SELECT id, status FROM catalog_import_runs WHERE file_hash_sha256 = %s AND status = 'completed'",
                (file_hash,),
            )
            existing = cur.fetchone()
            if existing:
                print(f"File already imported (run {existing['id']}). Skipping.")
                return {
                    "status": "skipped",
                    "reason": "already_imported",
                    "import_run_id": str(existing["id"]),
                }

            # Ensure catalog source exists
            cur.execute(
                """INSERT INTO catalog_sources (id, name, repository_url, license_attribution)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, source_name)),
                    source_name,
                    "https://github.com/junioralive/Indian-Medicine-Dataset",
                    "MIT License — junioralive/Indian-Medicine-Dataset",
                ),
            )
            source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source_name))

            # Create import run record
            import_run_id = str(uuid.uuid4())
            cur.execute(
                """INSERT INTO catalog_import_runs
                   (id, source_id, file_path, file_hash_sha256, importer_version, status, started_at)
                   VALUES (%s, %s, %s, %s, %s, 'running', %s)""",
                (import_run_id, source_id, file_path, file_hash, IMPORTER_VERSION, datetime.now(timezone.utc)),
            )
            conn.commit()

        # Read and validate CSV
        print(f"Reading CSV: {file_path}")
        print(f"File hash: {file_hash}")

        total_rows = 0
        accepted_rows = 0
        rejected_rows = 0
        errors = []

        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            # Validate columns
            if reader.fieldnames is None:
                raise ValueError("CSV has no headers")

            actual_columns = set(reader.fieldnames)
            missing_required = REQUIRED_COLUMNS - actual_columns
            if missing_required:
                raise ValueError(f"Missing required columns: {missing_required}")

            # Detect price column name
            price_col = "price(₹)" if "price(₹)" in actual_columns else "price"

            # Check for extra columns in updated CSV
            has_salt = "salt_composition" in actual_columns

            print(f"Columns: {reader.fieldnames}")
            print(f"Price column: {price_col}")
            print(f"Has salt_composition: {has_salt}")

            # Create catalog version (inactive until import completes)
            version_id = str(uuid.uuid4())
            version_label = f"v1.0-{file_hash[:8]}"

            with conn.cursor() as cur2:
                cur2.execute(
                    """INSERT INTO catalog_versions (id, version_label, import_run_id, published_by, is_active)
                       VALUES (%s, %s, %s, 'system_importer', false)""",
                    (version_id, version_label, import_run_id),
                )
            conn.commit()

            batch = []

            for row in reader:
                total_rows += 1

                # Validate row
                source_id = str(row.get("id", "")).strip()
                name = str(row.get("name", "")).strip()

                if not source_id or not name:
                    rejected_rows += 1
                    if len(errors) < 100:
                        errors.append({"row": total_rows, "error": "missing id or name"})
                    continue

                # Parse fields
                price = parse_price(row.get(price_col))
                is_discontinued = parse_discontinued(row.get("Is_discontinued"))
                manufacturer = row.get("manufacturer_name", "").strip() or None
                med_type = row.get("type", "").strip() or None
                pack_size = row.get("pack_size_label", "").strip() or None
                comp1 = row.get("short_composition1", "").strip() or None
                comp2 = row.get("short_composition2", "").strip() or None
                salt = row.get("salt_composition", "").strip() or None if has_salt else None

                # Normalize for search
                name_normalized = normalize_medicine_name(name)
                manufacturer_normalized = normalize_manufacturer(manufacturer) if manufacturer else None
                composition_normalized = normalize_composition(comp1, comp2)
                source_hash = compute_row_hash(row)

                medicine_id = str(uuid.uuid4())

                batch.append((
                    medicine_id,
                    source_id,
                    name,
                    name_normalized,
                    price,
                    is_discontinued,
                    manufacturer,
                    manufacturer_normalized,
                    med_type,
                    pack_size,
                    comp1,
                    comp2,
                    composition_normalized,
                    salt,
                    version_id,
                    source_hash,
                ))
                accepted_rows += 1

                # Flush batch
                if len(batch) >= batch_size:
                    _insert_batch(conn, batch)
                    batch = []
                    print(f"  Imported {accepted_rows} rows...", end="\r")

            # Final batch
            if batch:
                _insert_batch(conn, batch)

        print(f"\nImport complete: {accepted_rows} accepted, {rejected_rows} rejected out of {total_rows} total")

        # Activate catalog version
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE catalog_versions
                   SET is_active = true, published_at = %s
                   WHERE id = %s""",
                (datetime.now(timezone.utc), version_id),
            )

            # Deactivate previous versions
            cur.execute(
                "UPDATE catalog_versions SET is_active = false WHERE id != %s",
                (version_id,),
            )

            # Update import run status
            cur.execute(
                """UPDATE catalog_import_runs
                   SET status = 'completed', total_rows = %s, accepted_rows = %s,
                       rejected_rows = %s, error_report = %s::jsonb, completed_at = %s
                   WHERE id = %s""",
                (
                    total_rows, accepted_rows, rejected_rows,
                    psycopg.types.json.Json({"errors": errors[:100]}),
                    datetime.now(timezone.utc),
                    import_run_id,
                ),
            )
            conn.commit()
            print("Running ANALYZE on medicines table for query planner...")
            cur.execute("ANALYZE medicines;")
            conn.commit()

        return {
            "status": "completed",
            "import_run_id": import_run_id,
            "catalog_version": version_label,
            "total_rows": total_rows,
            "accepted_rows": accepted_rows,
            "rejected_rows": rejected_rows,
            "file_hash": file_hash,
        }

    except Exception as e:
        conn.rollback()
        # Mark import as failed
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE catalog_import_runs SET status = 'failed', error_report = %s::jsonb, completed_at = %s WHERE id = %s",
                    (
                        psycopg.types.json.Json({"error": str(e)}),
                        datetime.now(timezone.utc),
                        import_run_id if "import_run_id" in dir() else "unknown",
                    ),
                )
                conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _insert_batch(conn: psycopg.Connection, batch: list[tuple]) -> None:
    """Insert a batch of medicines using COPY for high performance."""
    with conn.cursor() as cur:
        with cur.copy(
            """COPY medicines
               (id, source_id, name, name_normalized, price, is_discontinued,
                manufacturer_name, manufacturer_normalized, type, pack_size_label,
                short_composition1, short_composition2, composition_normalized,
                salt_composition, catalog_version_id, source_hash)
               FROM STDIN"""
        ) as copy:
            for row in batch:
                copy.write_row(row)
    conn.commit()


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    import os

    load_dotenv()

    parser = argparse.ArgumentParser(description="Import medicine CSV into database")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--batch-size", type=int, default=5000, help="Batch size for inserts")
    parser.add_argument("--database-url", default=None, help="Database URL (or set DATABASE_URL_SYNC)")

    args = parser.parse_args()

    db_url = args.database_url or os.getenv("DATABASE_URL_SYNC", "postgresql://medicine:medicine_dev@localhost:5432/medicine_search")

    result = import_csv(args.file, db_url, args.batch_size)
    print(f"\nResult: {result}")
