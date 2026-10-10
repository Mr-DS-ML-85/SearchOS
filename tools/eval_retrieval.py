"""Retrieval evaluation harness.

There is no way to tell whether a change to ranking, thresholds or the embedding
model helped or hurt without a set of queries whose correct answers are known.
Every threshold in SearchOS — `RERANK_MIN_SCORE`, `DEDUPE_SIMHASH_THRESHOLD`,
`SEMANTIC_CACHE_THRESHOLD` — was previously calibrated against queries the author
typed by hand. This makes that reproducible.

    python tools/eval_retrieval.py                       # against a local stack
    python tools/eval_retrieval.py --gateway http://host:8000 --api-key sk_...
    python tools/eval_retrieval.py --json out.json       # for CI comparison

Metrics, at k=10 by default:

- **Recall@k** — fraction of the known-relevant documents that appeared.
- **MRR** — reciprocal rank of the first relevant result; 1.0 means rank 1.
- **nDCG@k** — graded ranking quality, using the order of `relevant` in the set.
- **Abstention** — for `expect_empty` queries, whether the system correctly
  returned nothing. A system that answers nonsense is penalised.

Point-to-point comparison is the point: run it before a change, run it after,
and diff the JSON. A threshold change that lifts recall but tanks nDCG on the
paraphrase queries is a regression, and this is how you see that.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = REPO_ROOT / "eval" / "queries.yaml"


def load_cases(path: Path) -> list[dict]:
    """Read the query set. Uses PyYAML when available, else a minimal parser.

    The fallback exists so a contributor who has not installed PyYAML can still
    run the harness rather than being told to `pip install` their way into a
    benchmark.
    """
    text = path.read_text()
    try:
        import yaml  # type: ignore[import-untyped]

        return list(yaml.safe_load(text) or [])
    except ImportError:
        return _parse_simple(text)


def _parse_simple(text: str) -> list[dict]:
    """Indentation-based parser for this file's restricted shape."""
    cases: list[dict] = []
    current: dict | None = None
    in_relevant = False
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if line.startswith("- query:"):
            if current:
                cases.append(current)
            current = {"query": line.split(":", 1)[1].strip()}
            in_relevant = False
        elif current is None:
            continue
        elif line.startswith("relevant:"):
            current["relevant"] = []
            in_relevant = True
        elif line.startswith("expect_empty:"):
            current["expect_empty"] = line.split(":", 1)[1].strip().lower() == "true"
            in_relevant = False
        elif line.startswith("note:"):
            current["note"] = line.split(":", 1)[1].strip()
            in_relevant = False
        elif indent >= 4 and line.startswith("- ") and in_relevant:
            current["relevant"].append(line[2:].strip())
    if current:
        cases.append(current)
    return cases


def relevance_rank(url: str, patterns: list[str]) -> int | None:
    """1-based graded relevance of ``url``, or None when it is not relevant."""
    lowered = (url or "").lower()
    for index, pattern in enumerate(patterns):
        if pattern.lower() in lowered:
            return index + 1
    return None


def score_case(case: dict, results: list[dict], k: int) -> dict:
    patterns = case.get("relevant") or []
    graded: list[int] = []
    for result in results[:k]:
        rank = relevance_rank(result.get("url", ""), patterns)
        graded.append(rank if rank is not None else 0)

    expect_empty = bool(case.get("expect_empty"))
    if expect_empty:
        # Abstention means returning nothing at all. A nonsense query that
        # still gets results is the failure this metric exists to catch.
        abstained = len(results) == 0
        return {
            "query": case["query"],
            "expect_empty": True,
            "abstained": abstained,
            "results": len(results),
            "note": case.get("note", ""),
        }

    hits = sum(1 for g in graded if g > 0)
    recall = hits / len(patterns) if patterns else 0.0

    mrr = 0.0
    for index, rank in enumerate(graded):
        if rank > 0:
            mrr = 1.0 / (index + 1)
            break

    dcg = sum((1.0 / math.log2(i + 2)) / r if r else 0.0 for i, r in enumerate(graded))
    ideal = sorted((i + 1 for i in range(len(patterns))), reverse=True)[:k]
    idcg = sum((1.0 / math.log2(i + 2)) / r for i, r in enumerate(ideal)) or 1.0

    return {
        "query": case["query"],
        "expect_empty": False,
        "recall": round(recall, 4),
        "mrr": round(mrr, 4),
        "ndcg": round(dcg / idcg, 4),
        "hits": hits,
        "of": len(patterns),
        "top_url": results[0]["url"] if results else None,
        "top_score": results[0].get("score") if results else None,
        "note": case.get("note", ""),
    }


def run(gateway: str, api_key: str, cases: list[dict], k: int, sources: str, timeout: float) -> dict:
    scored: list[dict] = []
    latencies: list[float] = []
    with httpx.Client(timeout=timeout) as client:
        for case in cases:
            params = {
                "q": case["query"],
                "num_results": k,
                "sources": sources,
                "cache": "false",
            }
            started = time.monotonic()
            try:
                resp = client.get(
                    f"{gateway.rstrip('/')}/v1/search",
                    params=params,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                scored.append({"query": case["query"], "error": f"{exc.__class__.__name__}: {exc}"})
                continue
            latencies.append(time.monotonic() - started)
            scored.append(score_case(case, payload.get("results") or [], k))

    scored_cases = [s for s in scored if not s.get("expect_empty") and "ndcg" in s]
    empties = [s for s in scored if s.get("expect_empty")]

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "k": k,
        "sources": sources,
        "queries": len(cases),
        "errors": sum(1 for s in scored if s.get("error")),
        "recall": mean([s["recall"] for s in scored_cases]),
        "mrr": mean([s["mrr"] for s in scored_cases]),
        "ndcg": mean([s["ndcg"] for s in scored_cases]),
        "abstention_rate": mean([1.0 if s.get("abstained") else 0.0 for s in empties]),
        "p50_ms": int(sorted(latencies)[len(latencies) // 2] * 1000) if latencies else 0,
        "p95_ms": int(sorted(latencies)[int(len(latencies) * 0.95)] * 1000) if latencies else 0,
        "cases": scored,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate SearchOS retrieval quality.")
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--set", type=Path, default=DEFAULT_SET)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--sources", default="all")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--baseline", type=Path, help="compare against a previous JSON report")
    args = parser.parse_args()

    if not args.api_key:
        key_file = REPO_ROOT / ".env"
        if key_file.exists():
            for line in key_file.read_text().splitlines():
                if line.startswith("BOOTSTRAP_ADMIN_API_KEY="):
                    args.api_key = line.split("=", 1)[1].strip()
                    break
    if not args.api_key:
        print("no API key: pass --api-key or set BOOTSTRAP_ADMIN_API_KEY in .env", file=sys.stderr)
        return 2

    cases = load_cases(args.set)
    if not cases:
        print(f"no cases parsed from {args.set}", file=sys.stderr)
        return 2

    report = run(args.gateway, args.api_key, cases, args.k, args.sources, args.timeout)

    print(f"\nSearchOS retrieval  k={report['k']}  sources={report['sources']}  {report['queries']} queries")
    print("=" * 78)
    for case in report["cases"]:
        if "error" in case:
            print(f"  ERROR  {case['query'][:44]:<44} {case['error'][:24]}")
        elif case.get("expect_empty"):
            mark = "ok  " if case.get("abstained") else "FAIL"
            print(f"  {mark}  {case['query'][:44]:<44} returned {case['results']}")
        else:
            print(f"    nDCG {case['ndcg']:<6} MRR {case['mrr']:<6} R {case['recall']:<6} {case['query'][:40]}")
    print("-" * 78)
    print(
        f"  nDCG@10 {report['ndcg']:<8} MRR {report['mrr']:<8} Recall {report['recall']:<8}"
        f" abstain {report['abstention_rate']}"
    )
    print(f"  p50 {report['p50_ms']}ms   p95 {report['p95_ms']}ms   errors {report['errors']}")

    if args.baseline and args.baseline.exists():
        previous = json.loads(args.baseline.read_text())
        print("\nvs baseline:")
        for metric in ("ndcg", "mrr", "recall", "abstention_rate", "p50_ms"):
            before, after = previous.get(metric), report.get(metric)
            if before is None or after is None:
                continue
            delta = after - before
            arrow = "+" if delta > 0 else ""
            print(f"  {metric:<18} {before:<10} -> {after:<10} {arrow}{round(delta, 4)}")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
