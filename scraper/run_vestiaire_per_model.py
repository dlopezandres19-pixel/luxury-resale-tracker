"""
run_vestiaire_per_model.py
─────────────────────────────────────────────────────────────────────────
Calls the Vestiaire actor ONCE with all 6 model queries together.

Key finding (Sep 2026): maxItems is a GLOBAL cap per actor call AND
per Apify session — calling the actor N times with maxItems=30 each
still shares the same global budget, so only the first model gets data.
Solution: one call with all 6 queries and maxItems=180 (6×30).

The actor returns a `searchQuery` field on each item indicating which
query produced it — we use that + `_canonicalModel` tagging to route
items back to the correct model in process_vestiaire().

Actor: piotrv1001/vestiaire-collective-listings-scraper
Cost: $0.005/listing × 180 = $0.90/run max

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

# maxItems = 6 models × 30 items each = 180 total
MAX_ITEMS_TOTAL = 120

REGION_CONFIG = {
    "EU": {"country": "FR", "currency": "EUR"},
}

# Short queries that Vestiaire's search engine understands.
# Key → canonical model name in msrp_reference.json
# Value → search query string (no accents, no brand prefix for LV)
VESTIAIRE_QUERIES = {
    "Hermès Birkin 25": "Birkin 25",
    "Hermès Birkin 30": "Birkin 30",
    "Hermès Kelly 25":  "Kelly 25",
    "Hermès Kelly 28":  "Kelly 28",
    "LV Neverfull MM":  "Neverfull MM",
    "LV Speedy 25":     "Speedy 25",
}


def apify_post(url, body, token, timeout=1800):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True, choices=["EU"])
    args = ap.parse_args()

    token = os.environ["APIFY_TOKEN"]
    region_cfg = REGION_CONFIG[args.region]
    msrp = load_msrp()

    # Build reverse map: query string → canonical model name
    query_to_canonical = {v: k for k, v in VESTIAIRE_QUERIES.items()}

    url = f"https://api.apify.com/v2/acts/{VESTIAIRE_ACTOR}/run-sync-get-dataset-items"
    body = {
        "searchQueries": list(VESTIAIRE_QUERIES.values()),
        "maxItems": MAX_ITEMS_TOTAL,
        "fetchProductDetails": False,
        **region_cfg,
    }

    print(f"Scraping {len(VESTIAIRE_QUERIES)} models for region {args.region} in one call (maxItems={MAX_ITEMS_TOTAL})...")
    try:
        items = apify_post(url, body, token)
        print(f"  Total raw items: {len(items)}")
    except Exception as e:
        print(f"  FAILED after retries ({e}) — not writing anything.")
        sys.exit(1)

    if not items:
        print("WARNING: zero listings returned — not writing anything.")
        sys.exit(1)

    # Tag each item with its canonical model name using the searchQuery field
    for item in items:
        sq = item.get("searchQuery", "")
        item["_canonicalModel"] = query_to_canonical.get(sq)

    # Log per-model counts
    from collections import Counter
    counts = Counter(item.get("_canonicalModel") for item in items)
    for canonical, query in VESTIAIRE_QUERIES.items():
        print(f"  {canonical}: {counts.get(canonical, 0)} listings")

    snapshot = process_vestiaire(items, args.region, msrp)
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
