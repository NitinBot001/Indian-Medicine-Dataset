# Architectural & Search Engineering Decisions

Record of design choices, tradeoffs, and rationale for the Medicine Search Upgrade.

---

## Decision Log

### 1. In-Process ASGITransport for Tests and Benchmarks
- **Context**: 7 of 26 tests in `tests/integration/test_search_api.py` failed with `httpx.ConnectError` unless an external Uvicorn server was manually running on `:8000`.
- **Decision**: Used Starlette `TestClient(app)` (backed by ASGI transport) for in-process testing when `MEDICINE_SEARCH_BACKEND_URL` is omitted. Extended `scripts/benchmark_search.py` with `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))` to enable automated, reproducible benchmarks without requiring a separate server daemon.
- **Rationale**: Eliminates developer environment flakiness and ensures tests can run seamlessly in CI and local containers.

### 2. Query Normalizer & Wildcard Sanitization (`normalize_query`)
- **Context**: `_` and `%` survived normalization and acted as raw SQL `LIKE` wildcards (`p_n` matched `Pany`, `%` matched random rows). Alphanumeric tokens with glued digits (`dolo650`) failed prefix matching, and bare unit tokens (`mg`, `ml`) polluted searches.
- **Decision**: Implemented `normalize_query` in `app/services/normalization.py` performing:
  1. Unicode NFKC normalization and lowercasing.
  2. Alpha-digit separation (`dolo650` -> `dolo 650`, `625duo` -> `625 duo`).
  3. Stripping of non-alphanumeric characters (removing `%` and `_` wildcards). Added `escape_like` helper for parameterized escaping.
  4. Dropping bare unit tokens (`mg`, `ml`, `mcg`, `gm`, `iu`, `mm`, `cm`, etc.).
  5. Expanding standard form synonyms (`tab` -> `tablet`, `cap` -> `capsule`, etc.).
  6. Enforcing a hard ceiling of max 8 tokens and 64 characters.
- **Rationale**: Clean token streams prevent SQL pattern injection, improve prefix cacheability, and fix glued dosage queries.

### 3. Removal of Mid-Word Substring Matching
- **Context**: `PG_TOKEN_NAME_QUERY` used `LIKE '%q%'`, leading to irrelevant mid-word matches (e.g. `pnt` matching `Spntro-T Cream` and `Spntop-D Tablet`; `pan 40` ranking `Bepan 40` and `Cppan 40` above brand matches; `pcm` matching `Dpcm`).
- **Decision**: Replaced all substring `LIKE ('%' || :q || '%')` patterns with word-start matching: `LIKE (:q || '%')` OR `LIKE ('% ' || :q || '%')`.
- **Rationale**: Pharmacy billing users expect matches to begin at the start of words in the product name. Substrings inside words produce false positives.

### 4. Active-Before-Discontinued Strict Ordering
- **Context**: Discontinued medicines (7,903 rows) were ranked before active products if their names were shorter, and only flagged with `selectable=false`.
- **Decision**: Enforced `ORDER BY m.is_discontinued ASC` as the primary sort key across all search tiers and in the `sort_bucket` utility.
- **Rationale**: Billing users should never see discontinued products outranking active inventory.

### 5. Dropping Dead Alias Stage
- **Context**: `medicine_aliases` and `medicine_ingredients` tables are empty post-import. `PG_ALIAS_QUERY` and `SQLITE_ALIAS_QUERY` were wasting a database round-trip on every search query.
- **Decision**: Removed alias query execution from both PostgreSQL and SQLite search execution paths.
- **Rationale**: Reduces latency waterfall and eliminates unnecessary round-trips. Alias support will be reintroduced in Phase 4 via learned usage aliases.

### 6. Cross-Field AND for Multi-Token Queries
- **Context**: Queries like `dolo micro` and `pnt pant` failed or returned random manufacturer matches because tokens were treated as a single phrase.
- **Decision**: Added cross-field queries:
  - Name word + Manufacturer word (`dolo` in name, `micro` in manufacturer)
  - Name word + Composition word (`pnt` in name, `pant` in composition)
- **Rationale**: Resolves `dolo micro` directly to Dolo products from Micro Labs, and `pnt pant` to PNT products containing Pantoprazole.

### 7. Quality Gate on Typo & Garbage Queries
- **Context**: Garbage inputs (`zzzz`, `qwerty`) returned 10 irrelevant manufacturer/composition rows due to low-threshold fuzzy padding. Short tokens (3 letters or fewer) generated junk trigram matches.
- **Decision**:
  - Disabled trigram fuzzy search for tokens shorter than 4 characters. For 3-letter tokens (e.g. `pnt`), applied word-start and abbreviation matching.
  - Enforced a minimum similarity threshold (>= 0.40 for brand typo fixing against `brand_lex`, >= 0.40 for composition, >= 0.45 for manufacturer).
  - Eliminated padding with irrelevant results; queries with no plausible match return empty lists (`total_results: 0`).
- **Rationale**: Quality gate prevents customer confusion and speeds up garbage query rejection.

### 8. Schema & Lexicon Engineering (Phase 2)
- **Context**: Medicine names, compositions, and manufacturers required fast prefix matching, word-level tsvectors, brand typo tolerance, and ingredient lookups without full sequential table scans.
- **Decision**:
  1. Added `name_clean`, `brand`, `form`, `name_ts`, `comp_ts`, `mfr_ts`, `all_ts` columns to `medicines`.
  2. Created `idx_medicines_name_clean` (`text_pattern_ops`), `idx_medicines_brand_discontinued`, and GIN indexes on `name_ts`, `comp_ts`, `all_ts`.
  3. Created `brand_lex` table (118,084 brands) with `text_pattern_ops` and `gin_trgm_ops` for sub-20ms Levenshtein and abbreviation lookups.
  4. Created `ingredient_lex` table (2,160 words) via `ts_stat` on `comp_ts` for salt typo correction.
  5. Shipped as Alembic revision `20261016_0001` and idempotent script `scripts/build_search_index.py`.
- **Rationale**: Provides the index foundation required for single round-trip ladder execution in Phase 3.

### 9. Index Bloat Elimination & Cross-Dialect TSVector Support
- **Context**: Table had 350 MB in 14 indexes, of which 209 MB were zero-scan bloat or redundant (`idx_medicine_search_covering` was 122 MB, `idx_medicine_name_trgm` was 33 MB, `idx_medicine_name_normalized_btree` was 19 MB, `idx_medicine_source_id` was 10 MB). Furthermore, SQLite fallback tests broke on raw PostgreSQL `TSVECTOR` compilation.
- **Decision**:
  1. Dropped `idx_medicine_search_covering`, `idx_medicine_name_trgm`, `idx_medicine_name_normalized_btree`, and `idx_medicine_source_id`.
  2. Implemented `Text().with_variant(TSVECTOR, 'postgresql')` in `catalog.py` so SQLite compiles tsvector columns as `TEXT` while PostgreSQL compiles them natively as `TSVECTOR`.
  3. Recompacted relation pages via `VACUUM FULL`.
- **Rationale**: Total storage dropped from 513 MB to 310 MB (well below the 319 MB target ceiling), saving over 209 MB of index bloat while maintaining 100% cross-dialect SQLite fallback test compatibility.

### 10. Single Round-Trip PL/pgSQL Function Engine (`search_medicines`) (Phase 3)
- **Context**: Up to 10 sequential SQL round trips executed per query in Python (`F6` latency waterfall), producing cold cache times up to 8s and high overhead across network boundaries.
- **Decision**:
  1. Implemented `search_medicines(p_tokens, p_lim, p_fields, p_active_only, p_min_price, p_max_price, p_mfr, p_form)` in PL/pgSQL (Alembic migration `20261016_0002`).
  2. Tiers T0 (exact), T1 (prefix), T2 (brand typo + abbreviation), T3 (name tsvector prefix AND), T4 (cross-field AND), T5 (ingredient_lex salt typo & tsvector), T6 (mfr), and T7 (last resort trigram) execute sequentially inside PostgreSQL, short-circuiting as soon as `p_lim` rows are collected into a temp table.
  3. Python backend calls `SELECT * FROM search_medicines(...)` in exactly **1 database round trip**.
- **Rationale**: Eliminates network hop overhead. Reduces warm steady-state latency to 1.94 ms mean and 3.52 ms p95 across 254k rows.

### 11. In-Memory TTLCache with Comprehensive Filter Keying
- **Context**: Previous `_CACHE` evicted via $O(n)$ `min()` scans on insertion time, lacked filter parameters in cache keys, and was unsafe for filter mutations.
- **Decision**: Replaced with `cachetools.TTLCache(maxsize=10000, ttl=60)` using compound tuple key: `(tuple(tokens), limit, tuple(sorted(fields)), active_only, min_price, max_price, manufacturer, form, shop_id, dialect)`.
- **Rationale**: Constant-time $O(1)$ LRU eviction and memory bounds prevent memory leaks while accurately caching filtered result sets.

### 12. Loud Fallback Degradation Detection (`/health` 503)
- **Context**: When PostgreSQL was unreachable, the service silently served 21 mock rows from SQLite while `/health` reported status "ok" (F10).
- **Decision**: In `GET /health`, if running on fallback SQLite and row count $\le 21$, return HTTP 503 Service Unavailable unless `ALLOW_TINY_FALLBACK=true` is set.
- **Rationale**: Prevents silent production outages and alerts orchestration/monitoring systems immediately if database connectivity fails.

### 13. Backward-Compatible Search API with Rich Filtering & Diagnostics
- **Context**: Billing clients needed filters (`active_only`, `min_price`, `max_price`, `manufacturer`, `form`, `fields`) and diagnostics (`debug`), while preserving existing `catalog_matches` structures.
- **Decision**:
  1. Extended `GET /api/v1/search` with new query parameters.
  2. Preserved all existing fields on `MedicineMatch` and `SearchResponse`.
  3. Added `match_tier`, `matched_on`, `corrected_query`, `low_confidence` to matches, and `corrected_query` and `debug_info` to responses.
- **Rationale**: Full backward compatibility for existing billing clients while enabling rich inventory and search filtering.

### 14. Usage-Based Popularity Feedback & Intra-Tier Boost (Addressing L1) (Phase 4)
- **Context**: Prototype lesson L1 proved that brand-family catalog size fails as a popularity proxy because private-label generics (e.g. StayHappi with 405 rows) outrank real brands. Real popularity must be derived from actual pharmacist billing selections.
- **Decision**:
  1. Added `selection_count` column and index on `medicines`.
  2. Created `search_selections` audit table recording selection events `(query_text, query_normalized, medicine_id, shop_id, rank_selected, selected_at)`.
  3. Implemented `POST /api/v1/search/feedback` and `/select` to record billing selections.
  4. Modified `search_medicines` ordering inside tiers: `ORDER BY t.sort_order, t.m_disc, t.m_popularity DESC, t.m_score DESC, length(t.m_name)`.
- **Rationale**: Frequently billed medicines cleanly bubble to the top of their tier without violating the strict tier boundary invariant (T0 > T1 > T2 ...).

### 15. Shop-Specific Inventory & Custom Pricing Lifecycle
- **Context**: Pharmacists require immediate visibility into local store stock (`inventory_matches`) alongside national catalog matches, with store-specific custom retail pricing.
- **Decision**:
  1. Created `shop_inventory` table `(shop_id, medicine_id, quantity, is_in_stock, custom_price, updated_at)`.
  2. Added `POST /api/v1/search/inventory` for batch stock ingestion.
  3. Extended `GET /api/v1/search` with `shop_id` query parameter. When provided, in-stock medicines populate `inventory_matches` with `in_stock=True` and `stock_quantity`, while other items populate `catalog_matches`.
- **Rationale**: Provides instant pharmacy stock awareness without slowing down catalog autocomplete.

### 16. Automated Query Alias Learning from User Selections
- **Context**: Pharmacists frequently use recurring shorthand or custom queries for specific products.
- **Decision**: When a specific `query_normalized` receives $\ge 2$ selections for a `medicine_id`, the system automatically inserts an active alias into `medicine_aliases` with `alias_type = 'usage_learned'`.
- **Rationale**: Turns real-world billing patterns into permanent zero-latency fast paths.

### 17. Single Vercel Multi-Service Gateway Architecture
- **Context**: Finding F14 noted conflicting configuration files (`vercel.json` in root and `medicine-search-backend/vercel.json`), plus dangerous wildcard CORS paired with `allow_credentials=True`, and verbatim copying of upstream `transfer-encoding` / `content-encoding` headers causing proxy decoding errors.
- **Decision**:
  1. Deleted legacy `medicine-search-backend/vercel.json`. Retained root `vercel.json` as the single deployment descriptor for the multi-service setup (`app` gateway + `medicine-search-backend`).
  2. In `api/index.py`, configured `allow_credentials=False` for wildcard CORS (`allow_origins=["*"]`).
  3. Added `HOP_BY_HOP_HEADERS` stripping in `api/index.py` (`transfer-encoding`, `content-encoding`, `content-length`, `connection`, etc.) before returning upstream responses to clients.
- **Rationale**: Resolves deployment ambiguity, prevents header corruption, and complies with modern web security standards.

### 18. Continuous Integration & Search Quality Benchmark Workflow
- **Context**: Verification across 254k rows and 51 unit/integration/quality tests required automated regression testing and reproducible latency benchmarking on every pull request.
- **Decision**:
  1. Implemented `.github/workflows/ci.yml` with a native PostgreSQL 16 service container (`pg_trgm` and `fuzzystrmatch` extensions enabled).
  2. Runs database migrations (`alembic upgrade head`), search index generation, full `pytest` suite, and automated `benchmark_search.py` execution.
  3. Uploads benchmark JSON results as CI artifacts for latency tracking across commits.
- **Rationale**: Guarantees that search relevance rules, latency targets (p95 < 20 ms), and database schemas are verified on every commit.
