"""
run_vestiaire_per_model.py
─────────────────────────────────────────────────────────────────────────
Fixes the "maxItems is a GLOBAL cap across all queries" limitation of the
Vestiaire actor (confirmed in its own docs) by calling the actor once PER
MODEL, synchronously, each with its own maxItems budget. This guarantees
every one of the 8 tracked models gets scraped, instead of high-supply
models (Birkin) eating the whole shared budget.

Replaces the old Apify-Task + Schedule + Webhook setup for Vestiaire.
GitHub Actions itself now triggers this on a weekly cron — no webhook,
no "defaultDatasetId" macro dependency.

Usage:
    python run_vestiaire_per_model.py --region US
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
from compute_resale_v2 import process_vestiaire, load_msrp, load_history, save_history, MODEL_ALIASES

VESTIAIRE_ACTOR = "piotrv1001~vestiaire-collective-listings-scraper"
MAX_ITEMS_PER_MODEL = 30
REGION_CONFIG = {
    "US": {"country": "US", "currency": "USD", "language": "en", "sizeType": "US"},
    "EU": {"country": "FR", "currency": "EUR", "language": "en", "sizeType": "US"},
}


def apify_post(url, body, token, timeout=600):
    """POST with retry — one flaky call shouldn't kill the whole run."""
    data = json.dumps(body).encode("utf-8")
    req = Request(f"{url}?token={token}", data=data, headers={"Content-Type": "application/json"}, method="POST")
    last_err = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError) as e:
            last_err = e
            print(f"  attempt {attempt}/3 failed: {e} — retrying in 15s" if attempt < 3 else f"  attempt {attempt}/3 failed: {e} — giving up on this model")
            time.sleep(15)
    raise last_err


def scrape_one_model(model_name, region_cfg, token):
    """Runs the Vestiaire actor synchronously for a single model.
    Returns [] (not an exception) on failure — one bad model must not
    stop the other 7 from being processed."""
    url = f"https://api.apify.com/v2/acts/{VESTIAIRE_ACTOR}/run-sync-get-dataset-items"
    body = {
        "searchQueries": [model_name],
        "maxItems": MAX_ITEMS_PER_MODEL,
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
    ap.add_argument("--region", required=True, choices=["US", "EU"])
    args = ap.parse_args()

    token = os.environ["APIFY_TOKEN"]
    region_cfg = REGION_CONFIG[args.region]
    msrp = load_msrp()

    all_items = []
    print(f"Scraping {len(MODEL_ALIASES)} models for region {args.region}...")
    # Use the canonical model names (values of MODEL_ALIASES), not the
    # short aliases, so the search query itself is a real, specific term.
    canonical_models = sorted(set(MODEL_ALIASES.values()))
    for model in canonical_models:
        items = scrape_one_model(model, region_cfg, token)
        all_items.extend(items)

    if not all_items:
        print("WARNING: zero listings across ALL models — something is likely broken upstream (not just one model). Not writing anything this run.")
        sys.exit(1)

    snapshot = process_vestiaire(all_items, args.region, msrp)
    if not snapshot:
        print("WARNING: fetched listings but none matched a tracked model after processing. Check MODEL_ALIASES.")
        sys.exit(1)

    history, path = load_history(args.region)
    for model, entry in snapshot.items():
        history.setdefault(model, []).append(entry)
        print(f"  SAVED {model}: VR median={entry['vr_median']:.3f}  n={entry['n_listings']}")

    save_history(history, path)
    print(f"Done -> {path}")


if __name__ == "__main__":
    main()
