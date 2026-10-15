"""
Search benchmark script.

Runs a suite of representative queries against the search API and measures latency.
Target: p95 <= 100ms.

Usage:
    python -m scripts.benchmark_search [--url http://localhost:8000] [--iterations 3]
"""

import argparse
import json
import statistics
import sys
import time

import httpx

# Representative test queries per plan.md Section 10 Phase 0
# Covers: exact names, typos, partial prefixes, manufacturer, composition, dosage
TEST_QUERIES = [
    # Exact names
    {"q": "Augmentin 625 Duo Tablet", "expect_top1": "Augmentin 625 Duo Tablet"},
    {"q": "Azithral 500 Tablet", "expect_top1": "Azithral 500 Tablet"},
    {"q": "Dolo 650 Tablet", "expect_top1": None},
    {"q": "Crocin Advance Tablet", "expect_top1": None},

    # Common typos
    {"q": "augmentn 625", "expect_top1_contains": "Augmentin"},
    {"q": "azithrl 500", "expect_top1_contains": "Azithral"},
    {"q": "paracetamol", "expect_top1_contains": None},
    {"q": "amoxycllin", "expect_top1_contains": None},
    {"q": "cetrizine", "expect_top1_contains": None},

    # Partial prefixes
    {"q": "augm", "expect_top1_contains": "Augmentin"},
    {"q": "azith", "expect_top1_contains": "Azith"},
    {"q": "cipro", "expect_top1_contains": None},
    {"q": "metro", "expect_top1_contains": None},
    {"q": "ome", "expect_top1_contains": None},

    # Omitted spaces
    {"q": "augmentin625", "expect_top1_contains": "Augmentin"},

    # Transpositions
    {"q": "augmenitn", "expect_top1_contains": "Augmentin"},

    # Manufacturer queries
    {"q": "glaxo", "expect_top1_contains": None},
    {"q": "cipla", "expect_top1_contains": None},
    {"q": "sun pharma", "expect_top1_contains": None},

    # Composition/ingredient queries
    {"q": "amoxycillin 500", "expect_top1_contains": None},
    {"q": "azithromycin", "expect_top1_contains": None},

    # Short queries
    {"q": "ab", "expect_top1_contains": None},
    {"q": "pan", "expect_top1_contains": None},

    # Dosage disambiguation
    {"q": "augmentin 375", "expect_top1_contains": "Augmentin"},
    {"q": "augmentin 1000", "expect_top1_contains": "Augmentin"},
]


def run_benchmark(base_url: str, iterations: int = 3) -> dict:
    """Run benchmark queries and collect latency statistics."""
    url = f"{base_url}/api/v1/search"

    all_latencies = []
    results = []

    with httpx.Client(timeout=30.0) as client:
        # Warm up
        print("Warming up...")
        for _ in range(3):
            try:
                client.get(url, params={"q": "test", "limit": 10})
            except Exception:
                pass

        print(f"\nRunning {len(TEST_QUERIES)} queries x {iterations} iterations...\n")

        for i, tq in enumerate(TEST_QUERIES):
            query = tq["q"]
            query_latencies = []

            for _ in range(iterations):
                start = time.perf_counter()
                try:
                    resp = client.get(url, params={"q": query, "limit": 10})
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    data = resp.json()

                    query_latencies.append(elapsed_ms)
                    all_latencies.append(elapsed_ms)
                except Exception as e:
                    print(f"  ERROR: {query} -> {e}")
                    query_latencies.append(float("inf"))

            avg_ms = statistics.mean(query_latencies) if query_latencies else 0
            p95 = query_latencies[int(len(query_latencies) * 0.95)] if query_latencies else 0

            # Check result quality
            top1_name = ""
            result_count = 0
            if "data" in dir():
                catalog = data.get("catalog_matches", [])
                result_count = len(catalog)
                if catalog:
                    top1_name = catalog[0].get("name", "")

            status = "✓" if avg_ms < 100 else "⚠" if avg_ms < 200 else "✗"
            print(f"  {status} [{avg_ms:6.1f}ms] q=\"{query}\" → {result_count} results, top1=\"{top1_name[:50]}\"")

            results.append({
                "query": query,
                "avg_ms": round(avg_ms, 2),
                "result_count": result_count,
                "top1": top1_name,
            })

    # Overall statistics
    valid_latencies = [l for l in all_latencies if l != float("inf")]
    if valid_latencies:
        valid_latencies.sort()
        stats = {
            "total_queries": len(TEST_QUERIES) * iterations,
            "min_ms": round(min(valid_latencies), 2),
            "max_ms": round(max(valid_latencies), 2),
            "mean_ms": round(statistics.mean(valid_latencies), 2),
            "median_ms": round(statistics.median(valid_latencies), 2),
            "p95_ms": round(valid_latencies[int(len(valid_latencies) * 0.95)], 2),
            "p99_ms": round(valid_latencies[int(len(valid_latencies) * 0.99)], 2),
            "under_100ms": sum(1 for l in valid_latencies if l < 100),
            "under_200ms": sum(1 for l in valid_latencies if l < 200),
            "total_valid": len(valid_latencies),
        }

        print(f"\n{'='*60}")
        print(f"BENCHMARK RESULTS")
        print(f"{'='*60}")
        print(f"  Total queries: {stats['total_queries']}")
        print(f"  Min:    {stats['min_ms']:>8.2f} ms")
        print(f"  Mean:   {stats['mean_ms']:>8.2f} ms")
        print(f"  Median: {stats['median_ms']:>8.2f} ms")
        print(f"  P95:    {stats['p95_ms']:>8.2f} ms")
        print(f"  P99:    {stats['p99_ms']:>8.2f} ms")
        print(f"  Max:    {stats['max_ms']:>8.2f} ms")
        print(f"  < 100ms: {stats['under_100ms']}/{stats['total_valid']} ({100*stats['under_100ms']/stats['total_valid']:.1f}%)")
        print(f"  < 200ms: {stats['under_200ms']}/{stats['total_valid']} ({100*stats['under_200ms']/stats['total_valid']:.1f}%)")

        target_met = stats["p95_ms"] <= 100
        print(f"\n  {'✓ TARGET MET' if target_met else '✗ TARGET NOT MET'}: p95 {'<=' if target_met else '>'} 100ms")

        return {"stats": stats, "results": results}

    print("No valid results collected!")
    return {"stats": {}, "results": []}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark medicine search")
    parser.add_argument("--url", default="http://localhost:8000", help="Base API URL")
    parser.add_argument("--iterations", type=int, default=3, help="Iterations per query")
    args = parser.parse_args()

    results = run_benchmark(args.url, args.iterations)
