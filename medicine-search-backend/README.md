# Medicine Fuzzy Search Backend

Fast, typo-tolerant medicine search for pharmacy billing. Searches 253,973 Indian medicines with sub-100ms latency using PostgreSQL `pg_trgm`.

## Quick Start

```bash
# 1. Start PostgreSQL
docker compose up -d --wait

# 2. Install dependencies
pip install fastapi[standard] pydantic pydantic-settings sqlalchemy asyncpg psycopg[binary] uvicorn python-dotenv httpx

# 3. Create database schema
python scripts/setup_db.py

# 4. Import medicine dataset (~254k records, ~60 seconds)
python -m app.importers.csv_importer --file ../DATA/indian_medicine_data.csv

# 5. Start the API server
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 6. Run benchmark
python scripts/benchmark_search.py
```

Or use the all-in-one script:
```bash
chmod +x setup_and_run.sh
./setup_and_run.sh
```

## API Endpoints

### Search (Billing Autocomplete)
```
GET /api/v1/search?q=augmentn+625&limit=10
```

Returns ranked medicine matches with fuzzy typo tolerance:
```json
{
  "query": "augmentn 625",
  "total_results": 5,
  "latency_ms": 12.34,
  "catalog_matches": [
    {
      "medicine_id": "...",
      "name": "Augmentin 625 Duo Tablet",
      "manufacturer": "Glaxo SmithKline Pharmaceuticals Ltd",
      "composition": "Amoxycillin (500mg) + Clavulanic Acid (125mg)",
      "pack_size": "strip of 10 tablets",
      "match_reason": "typo_match",
      "score": 0.91,
      "selectable": true
    }
  ]
}
```

### Other Endpoints
- `GET /health` — Health check
- `GET /api/v1/admin/catalog/status` — Catalog info and medicine count
- `GET /docs` — Interactive API documentation (Swagger)

## Search Strategy

Implements a 5-tier search pipeline, all within a single SQL query:

1. **Exact match** (score: 1.0) — Normalized name equals query
2. **Alias match** (score: 0.98) — Approved alternative spellings
3. **Prefix match** (score: 0.90–0.97) — Name starts with query
4. **Fuzzy trigram match** (score: variable) — pg_trgm similarity for typos
5. **Composition match** (score: variable) — Ingredient/salt name matching

Plus manufacturer name boost (up to +0.10).

### Performance

- **GIN trigram indexes** on `name_normalized`, `manufacturer_normalized`, `composition_normalized`
- **Covering index** to reduce heap lookups
- **Similarity threshold** set to 0.2 (lower than default 0.3 for better medicine typo handling)
- **Target: p95 ≤ 100ms** for 253k records, limit ≤ 20

## Architecture

```
medicine-search-backend/
  app/
    main.py                      # FastAPI app entry point
    core/config.py               # Settings (pydantic-settings)
    db/session.py                # Async SQLAlchemy engine
    db/models/catalog.py         # ORM models with GIN indexes
    schemas/models.py            # Pydantic request/response schemas
    services/search.py           # Core fuzzy search (tiered SQL query)
    services/normalization.py    # Text normalization (NFKC, units, etc.)
    importers/csv_importer.py    # CSV import with provenance tracking
    api/v1/routes/search.py      # Search endpoint
    api/v1/routes/catalog.py     # Catalog admin endpoints
  scripts/
    setup_db.py                  # Schema + index creation
    benchmark_search.py          # Performance benchmark
  docker-compose.yml             # PostgreSQL 16 with pg_trgm
```

## Data Source

Uses the [Indian Medicine Dataset](https://github.com/junioralive/Indian-Medicine-Dataset) (MIT License):
- 253,973 medicines
- Fields: name, price, discontinued status, manufacturer, type, pack size, composition

## License

MIT
