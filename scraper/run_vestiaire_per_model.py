"""
run_vestiaire_per_model.py
─────────────────────────────────────────────────────────────────────────
Calls the Vestiaire actor once PER MODEL (each model gets its own
maxItems budget).

Self-healing (added Sep 21, 2026 — Vestiaire intermittently returns 0
listings, a soft block on the actor's side):
  1. Skips models that already have an entry for today, so the later
     catch-up run only retries what failed (and never overwrites
     manual entries).
  2. Two passes per run: models that fail pass 1 are retried after a
     cool-down.
  3. Saves after every successful model, so a timeout never loses data.

Actor: piotrv1001/vestiaire-collective-listings-scraper
Cost: ~$0.10 per successful model; runs that return 0 cost ~$0.

Usage:
    python run_vestiaire_per_model.py --region EU          # normal
    python run_vestiaire_per_model.py --region EU --force  # re-scrape all

Env vars required:
    APIFY_TOKEN
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_resale_v2 import process_vestiaire, load_msrp, load_history, save_history, upsert_entry

VESTIAIRE_ACTOR = "piotrv1001~vestiaire-collective-listings-scraper"
MAX_ITEMS_PER_MODEL = 20
ATTEMPTS_PER_PASS = 3
RETRY_WAIT_S = 45
N_PASSES = 2
COOLDOWN_BETWEEN_PASSES_S = 180

REGION_CONFIG = {
    "EU": {"country": "FR", "currency": "EUR"},
}

# Short queries that Vestiaire's search understands.
# Speedy: "Speedy Bandouliere 25" to track M46977 (with strap).
VESTIAIRE_QUERIES = {
    "Hermès Birkin 25":  "Birkin 25",
    "Hermès Birkin 30":  "Birkin 30",
    "Hermès Kelly 25":   "Kelly 25",
    "Hermès Kelly 28":   "Kelly 28",
    "LV Neverfull MM":   "Neverfull MM",
    "LV Speedy 25":      "Speedy Bandouliere 25",
    "LV Pochette Métis": "Pochette Metis",
    "LV Alma BB":        "Alma BB",
}


def apify_post(url, body, token, timeout=1800):
    """POST with 3-attempt retry on HTTP/network errors."""
    data = json.dumps(body).encode("utf-8")
    req = Request(f"{url}?token={token}", data=data,
                  headers={"Content-Type": "application/json"}, method="POST")
    last_err = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError) as e:
            last_err = e
            if attempt < 3:
                print(f"    http attempt {attempt}/3 failed: {e} — retrying in 15s")
                time.sleep(15)
            else:
                print(f"    http attempt {attempt}/3 failed: {e} — giving up")
    raise last_err


def scrape_one_model(canonical, query, region_cfg, token):
    """Up to ATTEMPTS_PER_PASS actor calls. Returns items or []."""
    url = f"https://api.apify.com/v2/acts/{VESTIAIRE_ACTOR}/run-sync-get-dataset-items"
    body = {
        "searchQueries": [query],
        "maxItems": MAX_ITEMS_PER_MODEL,
        "fetchProductDetails": False,
        **region_cfg,
    }
    for attempt in range(1, ATTEMPTS_PER_PASS + 1):
        try:
            items = apify_post(url, body, token)
        except Exception as e:
            print(f"  {canonical}: request failed on attempt {attempt} ({e})")
            items = []
        if items:
            print(f"  {canonical}: {len(items)} listings")
            for it in items:
                it["_canonicalModel"] = canonical
            return items
        if attempt < ATTEMPTS_PER_PASS:
            print(f"  {canonical}: 0 listings on attempt {attempt} — retrying in {RETRY_WAIT_S}s")
            time.sleep(RETRY_WAIT_S)
    print(f"  {canonical}: 0 listings after {ATTEMPTS_PER_PASS} attempts")
    return []


def save_model(items, region, msrp):
    """Compute + save one model immediately. Returns True if saved."""
    snapshot = process_vestiaire(items, region, msrp)
    if not snapshot:
        return False
    history, path = load_history(region)
    for model, entry in snapshot.items():
        upsert_entry(history, model, entry)
        print(f"  SAVED {model}: VR median={entry['vr_median']:.3f}  n={entry['n_listings']}")
    save_history(history, path)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True, choices=["EU"])
    ap.add_argument("--force", action="store_true",
                    help="re-scrape every model even if today's entry exists")
    args = ap.parse_args()

    token = os.environ["APIFY_TOKEN"]
    region_cfg = REGION_CONFIG[args.region]
    msrp = load_msrp()
    today = datetime.now(timezone.utc).date().isoformat()

    history, _ = load_history(args.region)
    done = set()
    if not args.force:
        done = {m for m, arr in history.items()
                if any(e.get("date") == today for e in arr)}
    pending = [m for m in VESTIAIRE_QUERIES if m not in done]

    if done:
        print(f"Already saved today ({today}): {', '.join(sorted(done))} — skipping")
    if not pending:
        print("All models already have today's data. Nothing to do.")
        return

    for p in range(1, N_PASSES + 1):
        if p > 1:
            print(f"\nPass {p}: retrying {len(pending)} model(s) after "
                  f"{COOLDOWN_BETWEEN_PASSES_S}s cool-down...")
            time.sleep(COOLDOWN_BETWEEN_PASSES_S)
        else:
            print(f"Pass 1: scraping {len(pending)} model(s), maxItems={MAX_ITEMS_PER_MODEL}...")

        still_missing = []
        for canonical in pending:
            items = scrape_one_model(canonical, VESTIAIRE_QUERIES[canonical], region_cfg, token)
            if items and save_model(items, args.region, msrp):
                continue
            still_missing.append(canonical)
        pending = still_missing
        if not pending:
            break

    if pending:
        print(f"\nSTILL MISSING: {', '.join(pending)} — the catch-up run will retry these.")
    else:
        print("\nAll models saved.")


if __name__ == "__main__":
    main()
