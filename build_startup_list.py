#!/usr/bin/env python3
"""
Builds data/yc_startups.json: active, privately-held YC-backed companies
across every batch and stage (including very small/early ones), pulled
directly from YC's own public company-directory API.

ycombinator.com/companies is a thin frontend over a public Algolia index —
the search key below is visible in that page's own client-side JS. It's an
Algolia "secured" key: read-only and cryptographically restricted (by
Algolia, server-side) to this one public dataset, so exposing it here isn't
a secret leak, just re-using the same call the public page already makes.

Fully independent from scraper.py — this seeds a future "good startups"
internship alert, not the main jobright.ai digest.
"""

import json
import os
from urllib.parse import urlencode

import requests

ALGOLIA_URL      = "https://45bwzj1sgc-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_APP_ID   = "45BWZJ1SGC"
ALGOLIA_SEARCH_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJiMWM4ZTU0ZDlh"
    "YTZjOTJiMjlhMWFuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21wYW55"
    "X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlfTGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdG"
    "aWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE"
)
INDEX_NAME     = "YCCompany_production"
HITS_PER_PAGE  = 1000
OUTPUT_PATH    = os.path.join(os.path.dirname(__file__), "data", "yc_startups.json")

# Keep active, still-private companies — spans tiny early-stage up through
# large-but-still-private growth stage. Drop ones that have already IPO'd,
# been folded into an acquirer, or shut down.
EXCLUDED_STATUSES = {"Public", "Acquired", "Inactive"}


def _algolia_query(params: dict) -> dict:
    body = {"requests": [{"indexName": INDEX_NAME, "params": urlencode(params)}]}
    resp = requests.post(
        ALGOLIA_URL,
        params={
            "x-algolia-application-id": ALGOLIA_APP_ID,
            "x-algolia-api-key": ALGOLIA_SEARCH_KEY,
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["results"][0]


def fetch_all_batches() -> list[str]:
    """The index enforces an Algolia paginationLimitedTo cap (nbPages always
    comes back as 1, however many hits actually match), so a single query
    can only ever see the first ~1000 hits. No batch has more than ~400
    companies, so querying one batch at a time sidesteps the cap entirely."""
    result = _algolia_query({
        "hitsPerPage": 0,
        "page": 0,
        "query": "",
        "facets": json.dumps(["batch"]),
    })
    return list(result["facets"]["batch"].keys())


def fetch_yc_companies() -> list[dict]:
    batches = fetch_all_batches()
    print(f"  {len(batches)} batches to pull")

    companies: list[dict] = []
    for batch in batches:
        result = _algolia_query({
            "hitsPerPage": HITS_PER_PAGE,
            "page": 0,
            "query": "",
            "facetFilters": json.dumps([f"batch:{batch}"]),
        })
        hits = result["hits"]
        companies.extend(hits)
        print(f"  {batch!r}: {len(hits)} companies (total so far: {len(companies)})")

    return companies


def normalize(hit: dict) -> dict:
    return {
        "name":        hit.get("name", ""),
        "slug":        hit.get("slug", ""),
        "website":     hit.get("website", ""),
        "one_liner":   hit.get("one_liner", ""),
        "batch":       hit.get("batch", ""),
        "stage":       hit.get("stage", ""),
        "status":      hit.get("status", ""),
        "team_size":   hit.get("team_size"),
        "industries":  hit.get("industries", []),
        "regions":     hit.get("regions", []),
        "is_hiring":   hit.get("isHiring", False),
    }


def main() -> None:
    print("Fetching YC company directory ...")
    hits = fetch_yc_companies()
    print(f"Total fetched: {len(hits)}")

    active = [h for h in hits if h.get("status") not in EXCLUDED_STATUSES]
    print(f"Active + still-private (small and large): {len(active)}")

    companies = sorted((normalize(h) for h in active), key=lambda c: c["name"].lower())

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2)

    print(f"\nWrote {len(companies)} companies -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
