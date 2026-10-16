"""
Medicine search service with single round-trip PL/pgSQL engine, SQLite fallback,
and Phase 4 feedback loop & shop inventory support.

Features:
- Single-statement PL/pgSQL function execution in PostgreSQL (Phase 3).
- Strict Intent Ladder (T0-T7):
    T0: Exact name match
    T1: Prefix name match (name starts with query)
    T2: Typo tolerance (Levenshtein <= 2) and abbreviation on brand lexicon
    T3: Word-start prefix AND on name tsvector
    T4: Cross-field AND (name + manufacturer or name + composition)
    T5: Composition (active salt) match with typo tolerance against ingredient lexicon
    T6: Manufacturer match
    T7: Last resort trigram match (low_confidence=True, similarity >= 0.45)
- Phase 4: Usage popularity boost (selection_count DESC inside tiers).
- Phase 4: Shop inventory lookup & stock decoration.
- Phase 4: Feedback recording & auto-learning query aliases.
- In-memory TTLCache with cachetools (maxsize=10000, ttl=60s).
- Resilient SQLite fallback engine for offline/test environments.
"""

import difflib
import logging
import uuid
from dataclasses import dataclass
from enum import Enum

import cachetools
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.normalization import normalize_query

logger = logging.getLogger("medicine_search")


class MatchReason(str, Enum):
    EXACT_NAME = "exact_name"
    PREFIX = "prefix"
    TOKEN_MATCH = "token_match"
    CROSS_FIELD = "cross_field"
    TYPO_MATCH = "typo_match"
    COMPOSITION_MATCH = "composition_match"
    MANUFACTURER_MATCH = "manufacturer_match"
    TRIGRAM_FALLBACK = "trigram_fallback"


@dataclass
class SearchResult:
    medicine_id: str
    source_id: str
    name: str
    manufacturer_name: str | None
    composition: str | None
    pack_size_label: str | None
    price: float | None
    is_discontinued: bool
    match_parameter: str = "name"
    match_reason: str = "prefix"
    score: float = 0.0
    match_tier: int = 1
    matched_on: str = "name"
    corrected_query: str | None = None
    low_confidence: bool = False
    in_stock: bool | None = None
    stock_quantity: int | None = None
    custom_price: float | None = None


# In-memory LRU cache per specification (TTL = 60s, maxsize = 10,000)
SEARCH_CACHE = cachetools.TTLCache(maxsize=10000, ttl=60)

# Common ingredient typos for fallback matching
KNOWN_INGREDIENT_TYPOS = {
    "pantaprazole": "pantoprazole",
    "paracetmol": "paracetamol",
    "amoxycllin": "amoxycillin",
    "cetrizine": "cetirizine",
    "azithromycin": "azithromycin",
    "ciprofloxacin": "ciprofloxacin",
}


# ============================================================
# POSTGRESQL SINGLE ROUND-TRIP SEARCH FUNCTION
# ============================================================

PG_SEARCH_FUNCTION_QUERY = text("""
SELECT
    medicine_id,
    source_id,
    name,
    manufacturer_name,
    composition,
    pack_size_label,
    price,
    is_discontinued,
    match_tier,
    matched_on,
    match_reason,
    score,
    corrected_query,
    low_confidence
FROM search_medicines(
    p_tokens => :tokens,
    p_lim => :lim,
    p_fields => :fields,
    p_active_only => :active_only,
    p_min_price => :min_price,
    p_max_price => :max_price,
    p_mfr => :mfr,
    p_form => :form
);
""")


async def search_medicines_postgres(
    session: AsyncSession,
    tokens: list[str],
    limit: int = 10,
    fields: list[str] | None = None,
    active_only: bool = False,
    min_price: float | None = None,
    max_price: float | None = None,
    manufacturer: str | None = None,
    form: str | None = None,
) -> list[SearchResult]:
    """Execute single round-trip PL/pgSQL search function in PostgreSQL."""
    if fields is None:
        fields = ["name", "composition", "manufacturer"]

    res = await session.execute(
        PG_SEARCH_FUNCTION_QUERY,
        {
            "tokens": tokens,
            "lim": limit,
            "fields": fields,
            "active_only": active_only,
            "min_price": min_price,
            "max_price": max_price,
            "mfr": manufacturer,
            "form": form,
        },
    )

    results: list[SearchResult] = []
    for row in res.fetchall():
        results.append(
            SearchResult(
                medicine_id=str(row.medicine_id),
                source_id=row.source_id,
                name=row.name,
                manufacturer_name=row.manufacturer_name,
                composition=row.composition if row.composition else None,
                pack_size_label=row.pack_size_label,
                price=float(row.price) if row.price is not None else None,
                is_discontinued=bool(row.is_discontinued),
                match_parameter=row.matched_on,
                match_reason=row.match_reason,
                score=round(float(row.score), 4),
                match_tier=int(row.match_tier),
                matched_on=row.matched_on,
                corrected_query=row.corrected_query if row.corrected_query else None,
                low_confidence=bool(row.low_confidence),
            )
        )
    return results


# ============================================================
# SQLITE FALLBACK LADDER
# ============================================================

SQLITE_EXACT_NAME_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    0 AS match_tier,
    'name' AS matched_on,
    'exact_name' AS match_reason,
    1.0 AS score
FROM medicines m
WHERE (m.name_normalized = :q OR m.name = :q_orig)
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized), m.name_normalized
LIMIT :limit
""")

SQLITE_PREFIX_NAME_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    1 AS match_tier,
    'name' AS matched_on,
    'prefix' AS match_reason,
    (0.91 + 0.06 * (CAST(length(:q) AS REAL) / CASE WHEN length(m.name_normalized) > 1 THEN length(m.name_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE m.name_normalized LIKE (:q || '%')
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

SQLITE_CROSS_NAME_MFR_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    4 AS match_tier,
    'name' AS matched_on,
    'cross_field' AS match_reason,
    0.72 AS score
FROM medicines m
WHERE (m.name_normalized LIKE (:name_token || '%') OR m.name_normalized LIKE ('% ' || :name_token || '%'))
  AND (m.manufacturer_normalized LIKE (:mfr_token || '%') OR m.manufacturer_normalized LIKE ('% ' || :mfr_token || '%'))
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized)
LIMIT :remaining
""")

SQLITE_CROSS_NAME_COMP_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    4 AS match_tier,
    'name' AS matched_on,
    'cross_field' AS match_reason,
    0.72 AS score
FROM medicines m
WHERE (m.name_normalized LIKE (:name_token || '%') OR m.name_normalized LIKE ('% ' || :name_token || '%'))
  AND (m.composition_normalized LIKE (:comp_token || '%') OR m.composition_normalized LIKE ('% ' || :comp_token || '%'))
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized)
LIMIT :remaining
""")

SQLITE_WORD_TOKEN_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    3 AS match_tier,
    'name' AS matched_on,
    'token_match' AS match_reason,
    (0.85 + 0.04 * (CAST(length(:q) AS REAL) / CASE WHEN length(m.name_normalized) > 1 THEN length(m.name_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE (m.name_normalized LIKE (:q || '%') OR m.name_normalized LIKE ('% ' || :q || '%'))
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

SQLITE_CANDIDATES_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.name_normalized,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    COALESCE(m.selection_count, 0) AS selection_count
FROM medicines m
WHERE ((:first_char != '' AND m.name_normalized LIKE (:first_char || '%'))
       OR (:word != '' AND (m.name_normalized LIKE (:word || '%') OR m.name_normalized LIKE ('% ' || :word || '%')))
       OR (:num != '' AND m.name_normalized LIKE ('%' || :num || '%')))
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized)
LIMIT 150
""")

SQLITE_PREFIX_COMPOSITION_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    5 AS match_tier,
    'composition' AS matched_on,
    'composition_match' AS match_reason,
    (0.65 + 0.05 * (CAST(length(:q) AS REAL) / CASE WHEN length(COALESCE(m.composition_normalized, '')) > 1 THEN length(m.composition_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE (m.composition_normalized LIKE (:q || '%') OR m.composition_normalized LIKE ('% ' || :q || '%'))
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC,
         COALESCE(m.selection_count, 0) DESC,
         CASE WHEN m.short_composition2 IS NULL OR m.short_composition2 = '' THEN 0 ELSE 1 END,
         length(m.name_normalized)
LIMIT :remaining
""")

SQLITE_PREFIX_MANUFACTURER_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    6 AS match_tier,
    'manufacturer' AS matched_on,
    'manufacturer_match' AS match_reason,
    (0.45 + 0.05 * (CAST(length(:q) AS REAL) / CASE WHEN length(COALESCE(m.manufacturer_normalized, '')) > 1 THEN length(m.manufacturer_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE (m.manufacturer_normalized LIKE (:q || '%') OR m.manufacturer_normalized LIKE ('% ' || :q || '%'))
  AND (NOT :active_only OR NOT m.is_discontinued)
  AND (:min_price IS NULL OR m.price >= :min_price)
  AND (:max_price IS NULL OR m.price <= :max_price)
  AND (:mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || :mfr || '%') OR m.manufacturer_name LIKE ('%' || :mfr || '%'))
  AND (:form IS NULL OR m.name_normalized LIKE ('%' || :form || '%') OR m.pack_size_label LIKE ('%' || :form || '%'))
ORDER BY m.is_discontinued ASC, COALESCE(m.selection_count, 0) DESC, length(m.name_normalized)
LIMIT :remaining
""")


def sort_bucket(bucket: list[SearchResult]) -> None:
    """Sort matches inside a tier per Section 3.3 and Phase 4 popularity."""
    bucket.sort(
        key=lambda r: (
            1 if r.is_discontinued else 0,
            -r.score,
            len(r.name),
            r.name,
        )
    )


async def search_medicines_sqlite(
    session: AsyncSession,
    q_norm: str,
    tokens: list[str],
    query_orig: str,
    limit: int = 10,
    fields: list[str] | None = None,
    active_only: bool = False,
    min_price: float | None = None,
    max_price: float | None = None,
    manufacturer: str | None = None,
    form: str | None = None,
) -> list[SearchResult]:
    """Execute SQLite search ladder matching ranking tiers."""
    if fields is None:
        fields = ["name", "composition", "manufacturer"]

    v_has_name = "name" in fields
    v_has_comp = "composition" in fields
    v_has_mfr = "manufacturer" in fields

    name_bucket: list[SearchResult] = []
    comp_bucket: list[SearchResult] = []
    mfr_bucket: list[SearchResult] = []
    seen_ids: set[str] = set()

    params_base = {
        "active_only": active_only,
        "min_price": min_price,
        "max_price": max_price,
        "mfr": manufacturer,
        "form": form,
    }

    def add_row(row, target_bucket: list[SearchResult], default_param: str, default_tier: int) -> None:
        med_id = str(row.medicine_id)
        if med_id not in seen_ids:
            seen_ids.add(med_id)
            target_bucket.append(
                SearchResult(
                    medicine_id=med_id,
                    source_id=row.source_id,
                    name=row.name,
                    manufacturer_name=row.manufacturer_name,
                    composition=row.composition if row.composition else None,
                    pack_size_label=row.pack_size_label,
                    price=float(row.price) if row.price is not None else None,
                    is_discontinued=bool(row.is_discontinued),
                    match_parameter=getattr(row, "matched_on", default_param),
                    match_reason=getattr(row, "match_reason", "prefix"),
                    score=round(float(row.score), 4),
                    match_tier=int(getattr(row, "match_tier", default_tier)),
                    matched_on=getattr(row, "matched_on", default_param),
                    corrected_query=None,
                    low_confidence=False,
                )
            )

    # 1. Exact Name (T0)
    if v_has_name:
        res = await session.execute(
            SQLITE_EXACT_NAME_QUERY,
            {"q": q_norm, "q_orig": query_orig, "limit": limit, **params_base},
        )
        for r in res.fetchall():
            add_row(r, name_bucket, "name", 0)

        if len(name_bucket) >= limit:
            sort_bucket(name_bucket)
            return name_bucket[:limit]

        # 2. Prefix Name (T1)
        remaining = limit - len(name_bucket)
        res = await session.execute(
            SQLITE_PREFIX_NAME_QUERY,
            {"q": q_norm, "remaining": remaining, **params_base},
        )
        for r in res.fetchall():
            add_row(r, name_bucket, "name", 1)

        if len(name_bucket) >= limit:
            sort_bucket(name_bucket)
            return name_bucket[:limit]

        # 3. Cross-Field AND (T4)
        if len(tokens) >= 2:
            t0, t1 = tokens[0], tokens[1]
            remaining = limit - len(name_bucket)
            res = await session.execute(
                SQLITE_CROSS_NAME_MFR_QUERY,
                {"name_token": t0, "mfr_token": t1, "remaining": remaining, **params_base},
            )
            for r in res.fetchall():
                add_row(r, name_bucket, "name", 4)

            remaining = limit - len(name_bucket)
            if remaining > 0:
                res = await session.execute(
                    SQLITE_CROSS_NAME_COMP_QUERY,
                    {"name_token": t0, "comp_token": t1, "remaining": remaining, **params_base},
                )
                for r in res.fetchall():
                    add_row(r, name_bucket, "name", 4)

        if len(name_bucket) >= limit:
            sort_bucket(name_bucket)
            return name_bucket[:limit]

        # 4. Word Token Match (T3)
        remaining = limit - len(name_bucket)
        if remaining > 0:
            res = await session.execute(
                SQLITE_WORD_TOKEN_QUERY,
                {"q": q_norm, "remaining": remaining, **params_base},
            )
            for r in res.fetchall():
                add_row(r, name_bucket, "name", 3)

        if len(name_bucket) >= limit:
            sort_bucket(name_bucket)
            return name_bucket[:limit]

        # 5. Fuzzy / Typo Candidates (T2)
        remaining = limit - len(name_bucket)
        if remaining > 0 and len(q_norm) >= 4:
            words = [t for t in tokens if t.isalpha()]
            numbers = [t for t in tokens if t.isdigit()]
            first_char = q_norm[0] if q_norm else ""
            first_word = words[0] if words else ""
            first_num = numbers[0] if numbers else ""

            cand_res = await session.execute(
                SQLITE_CANDIDATES_QUERY,
                {
                    "first_char": first_char,
                    "word": first_word[:4] if len(first_word) >= 4 else first_word,
                    "num": first_num,
                    **params_base,
                },
            )
            candidates = cand_res.fetchall()
            scored_candidates = []
            for cand in candidates:
                if str(cand.medicine_id) in seen_ids:
                    continue
                sim = difflib.SequenceMatcher(None, q_norm, cand.name_normalized).ratio()
                if sim >= 0.40:
                    scored_candidates.append((cand, sim))

            scored_candidates.sort(key=lambda x: (1 if x[0].is_discontinued else 0, -x[0].selection_count, -x[1], len(x[0].name)))
            for cand, sim in scored_candidates[:remaining]:
                seen_ids.add(str(cand.medicine_id))
                name_bucket.append(
                    SearchResult(
                        medicine_id=str(cand.medicine_id),
                        source_id=cand.source_id,
                        name=cand.name,
                        manufacturer_name=cand.manufacturer_name,
                        composition=cand.composition if cand.composition else None,
                        pack_size_label=cand.pack_size_label,
                        price=float(cand.price) if cand.price is not None else None,
                        is_discontinued=bool(cand.is_discontinued),
                        match_parameter="name",
                        match_reason="typo_match",
                        score=round(0.75 + 0.09 * sim, 4),
                        match_tier=2,
                        matched_on="name",
                        corrected_query=None,
                        low_confidence=False,
                    )
                )

        sort_bucket(name_bucket)
        if len(name_bucket) >= limit:
            return name_bucket[:limit]

    # 6. Composition (T5)
    remaining = limit - len(name_bucket)
    if v_has_comp and remaining > 0 and len(q_norm) >= 3:
        target_salt = KNOWN_INGREDIENT_TYPOS.get(q_norm, q_norm)
        comp_res = await session.execute(
            SQLITE_PREFIX_COMPOSITION_QUERY,
            {"q": target_salt, "remaining": remaining, **params_base},
        )
        for row in comp_res.fetchall():
            add_row(row, comp_bucket, "composition", 5)

    sort_bucket(comp_bucket)
    if len(name_bucket) + len(comp_bucket) >= limit:
        return (name_bucket + comp_bucket)[:limit]

    # 7. Manufacturer (T6)
    remaining = limit - len(name_bucket) - len(comp_bucket)
    if v_has_mfr and remaining > 0 and len(q_norm) >= 3:
        mfr_res = await session.execute(
            SQLITE_PREFIX_MANUFACTURER_QUERY,
            {"q": q_norm, "remaining": remaining, **params_base},
        )
        for row in mfr_res.fetchall():
            add_row(row, mfr_bucket, "manufacturer", 6)

    sort_bucket(mfr_bucket)
    return (name_bucket + comp_bucket + mfr_bucket)[:limit]


# ============================================================
# PHASE 4: SHOP INVENTORY & FEEDBACK HELPERS
# ============================================================

async def get_shop_inventory_for_medicines(
    session: AsyncSession,
    shop_id: str,
    medicine_ids: list[str],
) -> dict[str, dict]:
    """Look up shop inventory and pricing for candidate medicines."""
    if not shop_id or not medicine_ids:
        return {}

    bind = session.bind
    dialect = bind.dialect.name if bind else "postgresql"

    if dialect == "sqlite":
        res = await session.execute(
            text("""
            SELECT
                CAST(medicine_id AS TEXT) AS medicine_id,
                quantity,
                is_in_stock,
                CAST(custom_price AS REAL) AS custom_price
            FROM shop_inventory
            WHERE shop_id = :shop_id
            """),
            {"shop_id": shop_id},
        )
    else:
        res = await session.execute(
            text("""
            SELECT
                medicine_id::text AS medicine_id,
                quantity,
                is_in_stock,
                custom_price::float8 AS custom_price
            FROM shop_inventory
            WHERE shop_id = :shop_id
              AND medicine_id::text = ANY(:ids)
            """),
            {"shop_id": shop_id, "ids": medicine_ids},
        )

    inv_map: dict[str, dict] = {}
    for row in res.fetchall():
        inv_map[str(row.medicine_id)] = {
            "quantity": row.quantity,
            "is_in_stock": bool(row.is_in_stock),
            "custom_price": float(row.custom_price) if row.custom_price is not None else None,
        }
    return inv_map


async def record_search_selection(
    session: AsyncSession,
    query: str,
    medicine_id: str,
    shop_id: str | None = None,
    rank_selected: int = 1,
) -> tuple[int, bool]:
    """
    Record user selection feedback event for ranking popularity and alias auto-learning.
    Returns (updated_selection_count, learned_alias).
    """
    q_norm, _ = normalize_query(query)
    q_norm_clean = q_norm if q_norm else query.strip().lower()
    event_id = str(uuid.uuid4())
    med_uuid = uuid.UUID(medicine_id) if isinstance(medicine_id, str) and len(medicine_id) == 36 else medicine_id

    # 1. Insert selection event
    await session.execute(
        text("""
        INSERT INTO search_selections (id, query_text, query_normalized, medicine_id, shop_id, rank_selected, selected_at)
        VALUES (:id, :q_text, :q_norm, :med_id, :shop_id, :rank, now())
        """),
        {
            "id": event_id,
            "q_text": query,
            "q_norm": q_norm_clean,
            "med_id": med_uuid,
            "shop_id": shop_id,
            "rank": rank_selected,
        },
    )

    # 2. Increment selection count on medicine
    res = await session.execute(
        text("""
        UPDATE medicines
        SET selection_count = COALESCE(selection_count, 0) + 1
        WHERE id = :med_id
        RETURNING selection_count
        """),
        {"med_id": med_uuid},
    )
    row = res.fetchone()
    count = row[0] if row else 1

    # 3. Check for alias auto-learning (if query was selected >= 2 times for this medicine)
    count_res = await session.execute(
        text("""
        SELECT count(*) FROM search_selections
        WHERE query_normalized = :q_norm AND medicine_id = :med_id
        """),
        {"q_norm": q_norm_clean, "med_id": med_uuid},
    )
    select_count_for_query = count_res.scalar() or 0

    learned_alias = False
    if select_count_for_query >= 2:
        alias_id = str(uuid.uuid4())
        await session.execute(
            text("""
            INSERT INTO medicine_aliases (id, medicine_id, alias, alias_normalized, alias_type, review_status, created_at)
            VALUES (:id, :med_id, :alias, :alias_norm, 'usage_learned', 'active', now())
            ON CONFLICT DO NOTHING
            """),
            {
                "id": alias_id,
                "med_id": med_uuid,
                "alias": query,
                "alias_norm": q_norm_clean,
            },
        )
        learned_alias = True

    await session.commit()

    # Invalidate cache so updated popularity takes effect immediately
    SEARCH_CACHE.clear()

    return count, learned_alias


async def update_shop_inventory_items(
    session: AsyncSession,
    shop_id: str,
    items: list[dict],
) -> int:
    """Update or insert shop inventory items."""
    updated = 0
    for it in items:
        med_id = it["medicine_id"]
        med_uuid = uuid.UUID(med_id) if isinstance(med_id, str) and len(med_id) == 36 else med_id
        await session.execute(
            text("""
            INSERT INTO shop_inventory (id, shop_id, medicine_id, quantity, is_in_stock, custom_price, updated_at)
            VALUES (:id, :shop_id, :med_id, :qty, :in_stock, :price, now())
            ON CONFLICT (shop_id, medicine_id)
            DO UPDATE SET
                quantity = EXCLUDED.quantity,
                is_in_stock = EXCLUDED.is_in_stock,
                custom_price = EXCLUDED.custom_price,
                updated_at = now()
            """),
            {
                "id": str(uuid.uuid4()),
                "shop_id": shop_id,
                "med_id": med_uuid,
                "qty": it.get("quantity", 0),
                "in_stock": it.get("is_in_stock", True),
                "price": it.get("custom_price"),
            },
        )
        updated += 1
    await session.commit()
    SEARCH_CACHE.clear()
    return updated


# ============================================================
# MAIN SEARCH ENTRY POINT
# ============================================================

async def search_medicines(
    session: AsyncSession,
    query: str,
    limit: int = 10,
    *,
    fields: list[str] | None = None,
    active_only: bool = False,
    min_price: float | None = None,
    max_price: float | None = None,
    manufacturer: str | None = None,
    form: str | None = None,
    shop_id: str | None = None,
    similarity_threshold: float = 0.35,
) -> list[SearchResult]:
    """
    Search medicines across the 8-tier ranking ladder (T0-T7) with popularity & inventory boost.
    """
    q_norm, tokens = normalize_query(query)
    if not q_norm or not tokens:
        return []

    if fields is None:
        fields = ["name", "composition", "manufacturer"]

    bind = session.bind
    dialect = bind.dialect.name if bind else "postgresql"

    # Cache key per specification
    cache_key = (
        tuple(tokens),
        limit,
        tuple(sorted(fields)),
        active_only,
        min_price,
        max_price,
        manufacturer,
        form,
        shop_id,
        dialect,
    )

    if cache_key in SEARCH_CACHE:
        return SEARCH_CACHE[cache_key]

    if dialect == "sqlite":
        results = await search_medicines_sqlite(
            session=session,
            q_norm=q_norm,
            tokens=tokens,
            query_orig=query,
            limit=limit,
            fields=fields,
            active_only=active_only,
            min_price=min_price,
            max_price=max_price,
            manufacturer=manufacturer,
            form=form,
        )
    else:
        results = await search_medicines_postgres(
            session=session,
            tokens=tokens,
            limit=limit,
            fields=fields,
            active_only=active_only,
            min_price=min_price,
            max_price=max_price,
            manufacturer=manufacturer,
            form=form,
        )

    # Decorate with shop inventory context if shop_id provided
    if shop_id and results:
        inv_map = await get_shop_inventory_for_medicines(
            session, shop_id, [r.medicine_id for r in results]
        )
        for r in results:
            if r.medicine_id in inv_map:
                inv = inv_map[r.medicine_id]
                r.in_stock = inv["is_in_stock"]
                r.stock_quantity = inv["quantity"]
                if inv["custom_price"] is not None:
                    r.custom_price = inv["custom_price"]
            else:
                r.in_stock = False
                r.stock_quantity = 0

    SEARCH_CACHE[cache_key] = results
    return results
