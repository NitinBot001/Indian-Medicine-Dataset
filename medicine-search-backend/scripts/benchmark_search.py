"""
Search benchmark script.

Runs a suite of representative queries against the search API and measures latency.
Measures cold and warm query latency, p50, p95, p99, and verifies search quality.
Supports direct ASGI in-process execution or HTTP endpoint testing.

Usage:
    python -m scripts.benchmark_search [--url http://localhost:8000] [--iterations 3]
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from typing import Any

import httpx

# Representative test queries covering:
# 1. Section 7 Acceptance queries:
#    pnt, pant, pnt pant, pcm, paracetmol, pantaprazole, dolo650, augmentn 625,
#    pan 40, dolo micro, p_n, %, zzzz, qwerty
# 2. Section 2 Findings & representative queries:
#    exact names, typos, partial prefixes, manufacturer, composition, dosage
TEST_QUERIES = [
    # --- Section 7 Acceptance Queries ---
    {"q": "pnt", "category": "acceptance", "description": "Abbreviation/prefix for PNT"},
    {"q": "pant", "category": "acceptance", "description": "Prefix for Pantoprazole/Pant"},
    {"q": "pnt pant", "category": "acceptance", "description": "Name + composition cross-field"},
    {"q": "pcm", "category": "acceptance", "description": "PCM abbreviation"},
    {"q": "paracetmol", "category": "acceptance", "description": "Common paracetamol typo"},
    {"q": "pantaprazole", "category": "acceptance", "description": "Ingredient typo for pantoprazole"},
    {"q": "dolo650", "category": "acceptance", "description": "Glued alphanumeric token"},
    {"q": "augmentn 625", "category": "acceptance", "description": "Brand typo + dosage"},
    {"q": "pan 40", "category": "acceptance", "description": "Word start matching (no Bepan)"},
    {"q": "dolo micro", "category": "acceptance", "description": "Name + manufacturer cross-field"},
    {"q": "p_n", "category": "acceptance", "description": "Literal wildcard character handling"},
    {"q": "%", "category": "acceptance", "description": "Literal percent character handling"},
    {"q": "zzzz", "category": "acceptance", "description": "Garbage query (should return 0)"},
    {"q": "qwerty", "category": "acceptance", "description": "Garbage query (should return 0)"},

    # --- Exact Names ---
    {"q": "Augmentin 625 Duo Tablet", "category": "exact", "description": "Full exact name"},
    {"q": "Azithral 500 Tablet", "category": "exact", "description": "Full exact name"},
    {"q": "Dolo 650 Tablet", "category": "exact", "description": "Full exact name"},
    {"q": "Crocin Advance Tablet", "category": "exact", "description": "Full exact name"},

    # --- Common Typos ---
    {"q": "azithrl 500", "category": "typo", "description": "Azithral typo + dosage"},
    {"q": "amoxycllin", "category": "typo", "description": "Ingredient typo"},
    {"q": "cetrizine", "category": "typo", "description": "Cetirizine typo"},

    # --- Partial Prefixes ---
    {"q": "augm", "category": "prefix", "description": "Short 4-char prefix"},
    {"q": "azith", "category": "prefix", "description": "5-char prefix"},
    {"q": "cipro", "category": "prefix", "description": "5-char prefix"},
    {"q": "metro", "category": "prefix", "description": "5-char prefix"},
    {"q": "ome", "category": "prefix", "description": "3-char prefix"},

    # --- Transpositions & Decomposed ---
    {"q": "augmentin625", "category": "glued", "description": "Brand + number glued"},
    {"q": "augmenitn", "category": "transposition", "description": "Transposition typo"},

    # --- Manufacturer Queries ---
    {"q": "glaxo", "category": "manufacturer", "description": "Company name"},
    {"q": "cipla", "category": "manufacturer", "description": "Company name"},
    {"q": "sun pharma", "category": "manufacturer", "description": "Multi-word company name"},

    # --- Composition Queries ---
    {"q": "amoxycillin 500", "category": "composition", "description": "Composition + strength"},
    {"q": "azithromycin", "category": "composition", "description": "Salt name"},

    # --- Short Queries ---
    {"q": "ab", "category": "short", "description": "2-char prefix"},
    {"q": "pan", "category": "short", "description": "3-char prefix"},

    # --- Dosage Disambiguation ---
    {"q": "augmentin 375", "category": "dosage", "description": "Dosage variant 375"},
    {"q": "augmentin 1000", "category": "dosage", "description": "Dosage variant 1000"},
]


async def run_benchmark_async(base_url: str | None = None, iterations: int = 3) -> dict[str, Any]:
    """Run benchmark queries and collect comprehensive latency statistics."""
    # Setup client
    if base_url:
        client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        app_context = None
    else:
        # In-process ASGI transport
        from app.main import app
        from app.db.session import db_manager
        await db_manager.initialize()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30.0)
        app_context = db_manager

    url = "/api/v1/search"
    results = []
    all_warm_latencies = []
    cold_latencies = []

    try:
        # 1. Cold pass (1 request per query, no prior cache warm-up for these specific queries)
        print("Executing cold queries pass...")
        for tq in TEST_QUERIES:
            query = tq["q"]
            start = time.perf_counter()
            try:
                resp = await client.get(url, params={"q": query, "limit": 10})
                elapsed_ms = (time.perf_counter() - start) * 1000
                cold_latencies.append(elapsed_ms)
            except Exception as e:
                print(f"  Cold error: {query} -> {e}")
                cold_latencies.append(float("inf"))

        # 2. Warm up
        print("Warming up database & cache...")
        for _ in range(2):
            for tq in TEST_QUERIES[:5]:
                try:
                    await client.get(url, params={"q": tq["q"], "limit": 10})
                except Exception:
                    pass

        print(f"\nRunning {len(TEST_QUERIES)} queries x {iterations} iterations (Warm)...\n")

        # 3. Warm measurements
        for i, tq in enumerate(TEST_QUERIES):
            query = tq["q"]
            category = tq.get("category", "")
            query_latencies = []
            last_resp_data = {}

            for _ in range(iterations):
                start = time.perf_counter()
                try:
                    resp = await client.get(url, params={"q": query, "limit": 10})
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    query_latencies.append(elapsed_ms)
                    all_warm_latencies.append(elapsed_ms)
                    last_resp_data = resp.json()
                except Exception as e:
                    print(f"  ERROR: {query} -> {e}")
                    query_latencies.append(float("inf"))

            valid = [l for l in query_latencies if l != float("inf")]
            if valid:
                valid.sort()
                p50 = statistics.median(valid)
                p95 = valid[int(len(valid) * 0.95)] if len(valid) > 1 else valid[0]
                avg_ms = statistics.mean(valid)
            else:
                p50 = p95 = avg_ms = 0.0

            catalog = last_resp_data.get("catalog_matches", []) if isinstance(last_resp_data, dict) else []
            result_count = len(catalog)
            top1_name = catalog[0].get("name", "") if catalog else ""
            top1_reason = catalog[0].get("match_reason", "") if catalog else ""
            top1_param = catalog[0].get("match_parameter", "") if catalog else ""

            status = "✓" if avg_ms < 50 else "•" if avg_ms < 100 else "⚠" if avg_ms < 200 else "✗"
            print(
                f"  {status} [{avg_ms:6.1f}ms (p50: {p50:5.1f}ms, p95: {p95:5.1f}ms)] "
                f"q=\"{query:<16}\" → {result_count:2d} res, top1=\"{top1_name[:35]}\" ({top1_reason})"
            )

            results.append({
                "query": query,
                "category": category,
                "avg_ms": round(avg_ms, 2),
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "result_count": result_count,
                "top1": top1_name,
                "top1_reason": top1_reason,
                "top1_param": top1_param,
            })

    finally:
        await client.aclose()

    # Overall statistics
    valid_warm = [l for l in all_warm_latencies if l != float("inf")]
    valid_cold = [l for l in cold_latencies if l != float("inf")]

    stats: dict[str, Any] = {}
    if valid_warm:
        valid_warm.sort()
        stats = {
            "total_queries_run": len(valid_warm),
            "warm": {
                "min_ms": round(min(valid_warm), 2),
                "mean_ms": round(statistics.mean(valid_warm), 2),
                "median_p50_ms": round(statistics.median(valid_warm), 2),
                "p95_ms": round(valid_warm[int(len(valid_warm) * 0.95)], 2),
                "p99_ms": round(valid_warm[int(len(valid_warm) * 0.99)], 2),
                "max_ms": round(max(valid_warm), 2),
                "under_50ms": sum(1 for l in valid_warm if l < 50),
                "under_100ms": sum(1 for l in valid_warm if l < 100),
                "under_200ms": sum(1 for l in valid_warm if l < 200),
                "total": len(valid_warm),
            },
        }
        if valid_cold:
            valid_cold.sort()
            stats["cold"] = {
                "min_ms": round(min(valid_cold), 2),
                "mean_ms": round(statistics.mean(valid_cold), 2),
                "median_p50_ms": round(statistics.median(valid_cold), 2),
                "p95_ms": round(valid_cold[int(len(valid_cold) * 0.95)], 2),
                "max_ms": round(max(valid_cold), 2),
            }

        w = stats["warm"]
        print(f"\n{'='*70}")
        print("BENCHMARK SUMMARY")
        print(f"{'='*70}")
        if "cold" in stats:
            c = stats["cold"]
            print(f"  COLD  (first hit): mean={c['mean_ms']}ms, p50={c['median_p50_ms']}ms, p95={c['p95_ms']}ms, max={c['max_ms']}ms")
        print(f"  WARM  (steady state): mean={w['mean_ms']}ms, p50={w['median_p50_ms']}ms, p95={w['p95_ms']}ms, p99={w['p99_ms']}ms")
        print(f"  < 50ms:  {w['under_50ms']}/{w['total']} ({100*w['under_50ms']/w['total']:.1f}%)")
        print(f"  < 100ms: {w['under_100ms']}/{w['total']} ({100*w['under_100ms']/w['total']:.1f}%)")
        print(f"  < 200ms: {w['under_200ms']}/{w['total']} ({100*w['under_200ms']/w['total']:.1f}%)")

        target_met = w["p95_ms"] <= 100
        print(f"\n  {'✓ TARGET MET' if target_met else '✗ TARGET NOT MET'}: Warm p95 {'<=' if target_met else '>'} 100ms")

    return {"stats": stats, "results": results}


def main():
    default_base_url = os.getenv("MEDICINE_SEARCH_BACKEND_URL")
    parser = argparse.ArgumentParser(description="Benchmark medicine search")
    parser.add_argument("--url", default=default_base_url, help="Base API URL (omit for in-process ASGI)")
    parser.add_argument("--iterations", type=int, default=3, help="Iterations per query")
    parser.add_argument("--output", help="Optional JSON file to save results")
    args = parser.parse_args()

    results = asyncio.run(run_benchmark_async(args.url, args.iterations))

    if args.output:
        import json
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nSaved benchmark results to {args.output}")


if __name__ == "__main__":
    main()
