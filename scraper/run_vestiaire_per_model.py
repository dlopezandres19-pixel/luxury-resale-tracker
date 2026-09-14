"""
run_vestiaire_per_model.py
─────────────────────────────────────────────────────────────────────────
Fixes the "maxItems is a GLOBAL cap across all queries" limitation of the
Vestiaire actor (confirmed in its own docs) by calling the actor once PER
MODEL, synchronously, each with its own maxItems budget. This guarantees
every one of the 6 tracked models gets scraped, instead of high-supply
models (Birkin) eating the whole shared budget.

Actor: piotrv1001/vestiaire-collective-listings-scraper
Confirmed input schema (from Apify JSON tab, Sep 2026):
  searchQueries, maxItems, country, currency, fetchProductDetails
  (language, sizeType, filters are NOT accepted — actor ignores/resets them)

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
from compute_resale_v2 import process_vestiaire, load_msrp, load_history, save_history, upsert_entry, MODEL_ALIASES

VESTIAIRE_ACTOR = "piotrv1001~vestiaire-collective-listings-scraper"
MAX_ITEMS_PER_MODEL = 30

# Only fields confirmed accepted by this actor (Sep 2026).
# language, sizeType, filters were silently ignored and caused maxItems
# to reset to the actor's default of 5 — removed to fix zero-results bug.
REGION_CONFIG = {
    "EU": {"country": "FR", "currency": "EUR"},
}


def apify_post(url, body, token, timeout=600):
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


def scrape_one_model(model_name, region_cfg, token):
    """Runs the Vestiaire actor synchronously for a single model.
    Returns [] on failure so one bad model doesn't stop the others."""
    url = f"https://api.apify.com/v2/acts/{VESTIAIRE_ACTOR}/run-sync-get-dataset-items"
    body = {
        "searchQueries": [model_name],
        "maxItems": MAX_ITEMS_PER_MODEL,
        "fetchProductDetails": False,
        **region_cfg,
    }
    try:
        items = apify_post(url, body, token)
        print(f"  {model_name}: {len(items)} listings")
        return items
    except Exception as e:
        print(f"  {model_name}: FAILED after retries ({e}) — skipping this model this week")
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True, choices=["EU"])
    args = ap.parse_args()

    token = os.environ["APIFY_TOKEN"]
    region_cfg = REGION_CONFIG[args.region]
    msrp = load_msrp()

    # Use short, natural search terms — NOT the canonical model names.
    # "Hermès Birkin 25" works. "LV Neverfull MM" does NOT — Vestiaire
    # doesn't recognize "LV" as a brand prefix; use "Neverfull MM" instead.
    # Matching back to canonical names happens via canonical_model() in
    # compute_resale_v2.py using MODEL_ALIASES.
    VESTIAIRE_QUERIES = {
        "Hermès Birkin 25": "Hermès Birkin 25",
        "Hermès Birkin 30": "Hermès Birkin 30",
        "Hermès Kelly 25":  "Hermès Kelly 25",
        "Hermès Kelly 28":  "Hermès Kelly 28",
        "LV Neverfull MM":  "Neverfull MM",
        "LV Speedy 25":     "Speedy 25",
    }

    all_items = []
    print(f"Scraping {len(VESTIAIRE_QUERIES)} models for region {args.region}...")
    for canonical, query in VESTIAIRE_QUERIES.items():
        items = scrape_one_model(query, region_cfg, token)
        # Tag each item with the canonical model so process_vestiaire()
        # can match it correctly regardless of what Vestiaire returns in
        # the 'model' field.
        for item in items:
            item["_canonicalModel"] = canonical
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
