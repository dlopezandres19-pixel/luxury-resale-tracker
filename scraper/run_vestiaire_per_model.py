"""
run_vestiaire_per_model.py
─────────────────────────────────────────────────────────────────────────
Calls the Vestiaire actor once PER MODEL to guarantee each model gets
its own maxItems budget. A single multi-query call distributes items by
relevance (Birkin dominates and starves Kelly/LV) — confirmed Sep 2026.

Root cause of previous failures: timeout was 600s, not enough for
Vestiaire with residential proxies. Fixed to 1800s (30 min) per call.

Actor: piotrv1001/vestiaire-collective-listings-scraper
Cost: $0.005/listing × 20 items × 6 models = $0.60/run

Usage:
    python run_vestiaire_per_model.py --region EU

Env vars required:
    APIFY_TOKEN
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_resale_v2 import process_vestiaire, load_msrp, load_history, save_history, upsert_entry

VESTIAIRE_ACTOR = "piotrv1001~vestiaire-collective-listings-scraper"
MAX_ITEMS_PER_MODEL = 20

REGION_CONFIG = {
    "EU": {"country": "FR", "currency": "EUR"},
}

# Short queries that Vestiaire's search understands.
# No accents, no brand prefix for LV — confirmed working Sep 2026.
# Speedy: "Speedy Bandouliere 25" specifically to track M46977 (with strap)
# and avoid mixing with Speedy Handbag (no strap, ~€500-800 cheaper).
VESTIAIRE_QUERIES = {
    "Hermès Birkin 25": "Birkin 25",
    "Hermès Birkin 30": "Birkin 30",
    "Hermès Kelly 25":  "Kelly 25",
    "Hermès Kelly 28":  "Kelly 28",
    "LV Neverfull MM":  "Neverfull MM",
    "LV Speedy 25":     "Speedy Bandouliere 25",
    "LV Pochette Métis": "Pochette Metis",
    "LV Alma BB":       "Alma BB",
}


def apify_post(url, body, token, timeout=1800):
    """POST with 3-attempt retry. Timeout 1800s — Vestiaire with residential
    proxies is slow; 600s was timing out before results came back."""
    data = json.dumps(body).encode("utf-8")
    req = Request(f"{url}?token={token}", data=data, headers={"Content-Type": "application/json"}, method="POST")
    last_err = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError) as e:
            last_err = e
            print(f"  attempt {attempt}/3 failed: {e}" + (" — retrying in 15s" if attempt < 3 else " — giving up"))
            time.sleep(15)
    raise last_err


def scrape_one_model(canonical, query, region_cfg, token):
    """One actor call per model with its own maxItems budget.
    Retries once if 0 results returned — Vestiaire occasionally blocks a
    query on the first attempt but succeeds on retry."""
    url = f"https://api.apify.com/v2/acts/{VESTIAIRE_ACTOR}/run-sync-get-dataset-items"
    body = {
        "searchQueries": [query],
        "maxItems": MAX_ITEMS_PER_MODEL,
        "fetchProductDetails": False,
        **region_cfg,
    }
    for attempt in range(1, 3):  # up to 2 attempts
        try:
            items = apify_post(url, body, token)
            if len(items) > 0:
                print(f"  {canonical}: {len(items)} listings")
                for item in items:
                    item["_canonicalModel"] = canonical
                return items
            elif attempt < 2:
                print(f"  {canonical}: 0 listings on attempt {attempt} — retrying in 30s")
                time.sleep(30)
            else:
                print(f"  {canonical}: 0 listings after {attempt} attempts — skipping")
                return []
        except Exception as e:
            print(f"  {canonical}: FAILED ({e}) — skipping")
            return []
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True, choices=["EU"])
    args = ap.parse_args()

    token = os.environ["APIFY_TOKEN"]
    region_cfg = REGION_CONFIG[args.region]
    msrp = load_msrp()

    all_items = []
    print(f"Scraping {len(VESTIAIRE_QUERIES)} models for region {args.region} (one call each, maxItems={MAX_ITEMS_PER_MODEL})...")
    for canonical, query in VESTIAIRE_QUERIES.items():
        items = scrape_one_model(canonical, query, region_cfg, token)
        all_items.extend(items)

    if not all_items:
        print("WARNING: zero listings across ALL models — not writing anything.")
        sys.exit(1)

    snapshot = process_vestiaire(all_items, args.region, msrp)
    if not snapshot:
        print("WARNING: fetched listings but none matched after processing.")
        sys.exit(1)

    history, path = load_history(args.region)
    for model, entry in snapshot.items():
        upsert_entry(history, model, entry)
        print(f"  SAVED {model}: VR median={entry['vr_median']:.3f}  n={entry['n_listings']}")

    save_history(history, path)
    print(f"Done -> {path}")


if __name__ == "__main__":
    main()
