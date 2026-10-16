# Indian Medicine Search Engine Backend

A high-performance, typo-tolerant medicine search backend engineered for pharmacy billing and point-of-sale autocomplete. Built on **FastAPI**, **async SQLAlchemy**, and **PostgreSQL 16**, searching **253,937 Indian medicine records** with **sub-10ms warm p95 latency** in **a single database round trip**.

---

## Key Features

- **Sub-10ms Autocomplete Latency**: Measured warm p95 latency of **5.58 ms** across 254k records running on single-container PostgreSQL (target was < 20 ms).
- **Single Round-Trip Engine**: The complete search ladder executes inside a compiled PostgreSQL PL/pgSQL function (`search_medicines`), eliminating application-database round trip waterfalls.
- **8-Tier Strict Ranking Ladder (T0–T7)**: Hierarchical relevance ordering guarantees exact and prefix brand matches always outrank fuzzy and salt matches.
- **Zero False-Positive Substrings**: Mid-word substring `LIKE '%q%'` queries are completely eliminated. Queries match anchored word boundaries only.
- **Intra-Tier Popularity Feedback**: Real billing selections record usage signals (`POST /api/v1/search/feedback`), bubbling frequently billed products to the top of their tier without violating tier boundaries.
- **Store Inventory Awareness**: Direct shop stock matching (`POST /api/v1/search/inventory`) segregates in-stock products into `inventory_matches` with custom shop pricing.
- **Compact & Clean Storage**: Index bloat eliminated; database and search indexes compacted from 513 MB to 310 MB (saving 209 MB).
- **Loud Fallback Safety**: Degraded SQLite fallbacks (21 starter records) fail loudly with HTTP 503 on `/health` rather than silently serving truncated data.

---

## 8-Tier Ranking Ladder

Search queries execute through an 8-tier priority ladder that short-circuits as soon as `limit` results are gathered:

| Tier | Priority | Strategy | Example Query & Match |
| :--- | :--- | :--- | :--- |
| **T0** | Exact Match | Normalized name exactly matches query | `pan 40 tablet` $\rightarrow$ PAN 40 Tablet |
| **T1** | Name Prefix | Medicine name starts with typed query phrase | `pnt` $\rightarrow$ PNT 40mg Tablet, Pnt Oral Suspension |
| **T2** | Spelling / Abbreviation | **T2a**: Typo correction against `brand_lex` (Levenshtein $\le 2$ on tokens $\ge 4$ chars)<br>**T2b**: Abbreviation pattern against `brand_lex` (`^p.*n.*t`) | `paracetmol` $\rightarrow$ Paracetamol Tablet<br>`pnt` $\rightarrow$ Penta 40 Tablet |
| **T3** | Name Word-Prefix | Full-text prefix AND on `name_ts` | `pan 40` $\rightarrow$ Pantop 40 Tablet |
| **T4** | Cross-Field AND | Multi-token match across name + manufacturer or name + composition | `dolo micro` $\rightarrow$ Dolo (Micro Labs)<br>`pnt pant` $\rightarrow$ PNT (Pantoprazole) |
| **T5** | Composition / Salt | Ingredient match against `ingredient_lex`, single-ingredient products boosted first | `pantaprazole` $\rightarrow$ Panprazole 40, Pantoprazole (500mg) |
| **T6** | Manufacturer | Brand or generic match on manufacturer name | `cipla` $\rightarrow$ Cipla Ltd medicines |
| **T7** | Last Resort Trigram | Trigram similarity ($\ge 0.45$) anywhere in name (flagged `low_confidence: true`) | Sparse typo fallback when limit not satisfied |

### Ranking Invariants
1. **Discontinued Demotion**: Discontinued medicines are always placed after active medicines within each tier (`ORDER BY is_discontinued ASC`).
2. **Popularity Ordering**: Verified billing usage (`selection_count`) sorts products **within** their tier (`ORDER BY t.sort_order, t.m_disc, t.m_popularity DESC, t.m_score DESC`). Popularity never elevates a lower tier above a higher tier.

---

## Benchmark Performance Across Phases

All benchmarks measured against the complete 253,937 de-duplicated Indian Medicine Dataset on PostgreSQL 16 using `scripts/benchmark_search.py` (37 realistic pharmacy queries, 3 iterations each):

| Phase | Description | Warm Mean | Warm p50 | Warm p95 | Warm p99 | Total Size |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **Baseline** | Legacy 10-statement waterfall | 1.71 ms | 1.25 ms | 2.69 ms | 4.15 ms | 513 MB |
| **Phase 1** | Safety & Quality Gate | 1.38 ms | 1.25 ms | 2.31 ms | 3.39 ms | 513 MB |
| **Phase 2** | Lexicons & Clean Indexes | 1.33 ms | 1.26 ms | 1.51 ms | 2.88 ms | **310 MB** |
| **Phase 3** | Single PL/pgSQL Function Engine | 1.94 ms | 1.48 ms | 3.52 ms | 4.52 ms | 310 MB |
| **Phase 4** | Popularity & Inventory Matching | 2.39 ms | 1.74 ms | **5.58 ms** | 6.72 ms | 310 MB |

- **Latency Target**: p95 $\le$ 20 ms (**Achieved: 5.58 ms**).
- **100% of benchmark queries** return in under 50 ms.
- **Storage Target**: $\le$ 319 MB (**Achieved: 310 MB**).

---

## API Reference

### 1. Medicine Autocomplete Search
```http
GET /api/v1/search?q={query}&limit={limit}&fields={fields}&active_only={bool}&shop_id={shop_id}
```

#### Query Parameters:
- `q` (required, string): Search query text (auto-sanitized, alpha-digit split, max 64 chars).
- `limit` (optional, integer, default: `10`, max: `50`): Maximum results to return.
- `fields` (optional, list of strings): Tiers to include (`name`, `composition`, `manufacturer`). Default: all.
- `active_only` (optional, boolean, default: `false`): When `true`, excludes discontinued medicines.
- `min_price` (optional, float): Minimum price filter.
- `max_price` (optional, float): Maximum price filter.
- `manufacturer` (optional, string): Case-insensitive manufacturer substring filter.
- `form` (optional, string): Dosage form filter (`tablet`, `capsule`, `syrup`, `injection`).
- `shop_id` (optional, string): Store identifier. When provided, in-stock store items populate `inventory_matches`.
- `debug` (optional, boolean, default: `false`): Returns normalization tokens, database dialect, and execution metadata in `debug_info`.

#### Response Example:
```json
{
  "query": "augmentn 625",
  "corrected_query": "augmentin 625",
  "total_results": 5,
  "latency_ms": 4.12,
  "inventory_matches": [],
  "catalog_matches": [
    {
      "medicine_id": "c1f103d3-7d7b-4028-a461-12c8b093375b",
      "name": "Augmentin 625 Duo Tablet",
      "name_normalized": "augmentin 625 duo tablet",
      "price": 223.42,
      "is_discontinued": false,
      "manufacturer_name": "Glaxo SmithKline Pharmaceuticals Ltd",
      "type": "allopathy",
      "pack_size_label": "strip of 10 tablets",
      "short_composition1": "Amoxycillin (500mg)",
      "short_composition2": "Clavulanic Acid (125mg)",
      "score": 0.95,
      "match_tier": 2,
      "matched_on": "name",
      "selectable": true,
      "in_stock": false
    }
  ]
}
```

---

### 2. Record Billing Selection (Popularity Feedback)
Records when a pharmacist clicks or bills a medicine, boosting its popularity score within its tier and auto-learning shorthand aliases:
```http
POST /api/v1/search/feedback
Content-Type: application/json

{
  "query": "dolo 650",
  "medicine_id": "c1f103d3-7d7b-4028-a461-12c8b093375b",
  "shop_id": "shop_01",
  "rank_selected": 1
}
```
*(Also available at alias route `POST /api/v1/search/select`)*.

---

### 3. Ingest Store Inventory
Bulk-updates local pharmacy stock quantities and custom store pricing:
```http
POST /api/v1/search/inventory
Content-Type: application/json

{
  "shop_id": "shop_blr_01",
  "items": [
    {
      "medicine_id": "c1f103d3-7d7b-4028-a461-12c8b093375b",
      "quantity": 150,
      "custom_price": 210.0,
      "is_in_stock": true
    }
  ]
}
```

---

### 4. Health & Degradation Check
```http
GET /health
```
- Returns `200 OK` when connected to primary database.
- Returns `503 Service Unavailable` with `{"status": "degraded", "degraded": true}` if operating on fallback SQLite with $\le 21$ rows unless explicitly overridden by `ALLOW_TINY_FALLBACK=true`.

---

## Directory Architecture

```
medicine-search-backend/
├── alembic/
│   ├── versions/
│   │   ├── 20261016_0001_search_schema_and_indexes.py
│   │   ├── 20261016_0002_plpgsql_search_function.py
│   │   └── 20261016_0003_phase4_feedback_and_inventory.py
│   └── env.py
├── app/
│   ├── api/v1/routes/
│   │   ├── search.py            # Search, feedback, inventory endpoints
│   │   └── catalog.py           # Catalog admin endpoints
│   ├── core/
│   │   └── config.py            # Pydantic environment configuration
│   ├── db/
│   │   ├── models/catalog.py    # SQLAlchemy ORM models & index definitions
│   │   └── session.py           # Async engine & session management
│   ├── importers/
│   │   └── csv_importer.py      # PostgreSQL COPY streaming importer
│   ├── schemas/models.py        # Pydantic schemas (SearchResponse, Match)
│   └── services/
│       ├── normalization.py     # NFKC, alpha-digit split, unit stripping
│       └── search.py            # Single round-trip search wrapper & cache
├── scripts/
│   ├── benchmark_search.py      # Search latency benchmarking utility
│   ├── build_search_index.py    # Idempotent index & lexicon builder
│   └── setup_db.py              # Database initialization
├── tests/
│   ├── integration/             # API transport tests
│   ├── quality/                 # Acceptance tests (T0-T7 ranking validation)
│   └── unit/                    # Normalizer & helper unit tests
├── docker-compose.yml           # Local PostgreSQL 16 container
├── requirements.txt             # Core backend dependencies
└── pyproject.toml               # Build & testing configuration
```

---

## Quickstart

### 1. Launch PostgreSQL 16
```bash
docker compose up -d --wait
```

### 2. Install Dependencies
```bash
pip install -e ".[dev]"
```

### 3. Run Migrations & Build Search Indexes
```bash
alembic upgrade head
python scripts/build_search_index.py
```

### 4. Run Test Suite
```bash
pytest -v
```

### 5. Run Search Latency Benchmark
```bash
python -m scripts.benchmark_search --limit 10 --iterations 3
```

### 6. Start Development Server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Deployment Architecture

The service is configured for Vercel multi-service deployment via root `vercel.json`:
- **`app` Gateway** (`api/index.py`): Serves as the public ingress proxy, cleanly stripping hop-by-hop headers and providing wildcard CORS.
- **`medicine-search-backend`**: Internal FastAPI service running the single round-trip search engine connected to PostgreSQL 16.
