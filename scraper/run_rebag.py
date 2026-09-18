"""
run_rebag.py
─────────────────────────────────────────────────────────────────────────
Scrapes Rebag (US authenticated resale marketplace) via the Apify actor
`lulzasaur/rebag-scraper`, which queries Rebag's public Shopify JSON API.

Design mirrors run_vestiaire_per_model.py exactly:
  - One Apify call per model (avoids shared-budget contamination)
  - Synchronous run-sync-get-dataset-items (no webhook, no dataset-id race)
  - GitHub Actions triggers this on the same weekly cron as Vestiaire
  - Appends to data_v2/us_resale.json (same file as Vestiaire US, so the
    dashboard shows a single US series — Rebag replaces Vestiaire for US)

Post-scrape filtering (critical — Rebag search is broad):
  - Neverfull: must contain "neverfull mm" OR ("neverfull" AND NOT any
    contamination keyword like "pochette", "wallet", "insert", "charm",
    "key", "strap", "bandouliere") AND price >= MIN_PRICE_USD
  - Speedy: must contain "speedy 25" (rules out Speedy 30/35/40) AND price
    >= MIN_PRICE_USD

Usage:
    python run_rebag.py

Env vars required:
    APIFY_TOKEN
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_resale_v2 import load_msrp, load_history, save_history, upsert_entry

# ── Actor config ────────────────────────────────────────────────────────
REBAG_ACTOR = "lulzasaur~rebag-scraper"
REGION = "US"
CURRENCY = "USD"

# Minimum listings to consider a VR median statistically reliable.
# Models below this threshold are still saved but flagged with
# n_listings_warning=True so the dashboard can show a confidence caveat.
MIN_RELIABLE_SAMPLE = 10

# ── Per-model search config ──────────────────────────────────────────────
# Each entry:
#   query         → passed to Rebag's search (keep broad enough to get results)
#   max_listings  → Apify maxListingsPerQuery
#   min_price     → hard floor ($USD), filters accessories / non-genuine
#   canonical     → key in msrp_reference.json
#   title_must    → ALL of these substrings must appear in title (lowercased)
#   title_exclude → ANY of these substrings disqualifies the listing
MODEL_CONFIGS = [
    # ── Louis Vuitton — text search + title filters ──────────────────────────
    {
        "query": "Neverfull MM",
        "max_listings": 50,
        "min_price": 600,
        "canonical": "LV Neverfull MM",
        # Text search — title_must ["neverfull", "mm"] ensures only MM size.
        "title_must": ["neverfull", "mm"],
        "title_exclude": ["pochette", "wallet", "insert", "charm", "key holder",
                          "keyholder", "bb", "mini"],
    },
    {
        "query": "Speedy Bandouliere 25",
        "max_listings": 50,
        "min_price": 500,
        "canonical": "LV Speedy 25",
        "title_must": ["speedy"],
        "title_exclude": ["speedy 30", "speedy 35", "speedy 40",
                          "charm", "wallet", "insert", "key holder", "keyholder"],
    },
    {
        # Clair Code doesn't work cleanly for Pochette Métis — use text search
        # + title filter to keep only Monogram Canvas version.
        "query": "Pochette Metis",
        "max_listings": 50,
        "min_price": 1500,
        "canonical": "LV Pochette Métis",
        "title_must": ["pochette metis"],
        "title_exclude": ["mini", "east west", "charm", "key", "wallet"],
    },
    {
        # Text search — title_must ["alma", "bb"] filters out PM/MM/GM sizes.
        "query": "Alma BB",
        "max_listings": 50,
        "min_price": 1200,
        "canonical": "LV Alma BB",
        "title_must": ["alma", "bb"],
        "title_exclude": ["charm", "key", "wallet", "pm", "mm", "gm"],
    },
    # ── Hermès — title filters (Clair Codes return 0 — material too specific) ──
    {
        "query": "Birkin 25",
        "max_listings": 50,
        "min_price": 8000,
        "canonical": "Hermès Birkin 25",
        "title_must": ["birkin handbag", "25"],
        "title_exclude": [],
    },
    {
        "query": "Birkin 30",
        "max_listings": 50,
        "min_price": 8000,
        "canonical": "Hermès Birkin 30",
        "title_must": ["birkin handbag", "30"],
        "title_exclude": [],
    },
    {
        "query": "Kelly 25",
        "max_listings": 50,
        "min_price": 8000,
        "canonical": "Hermès Kelly 25",
        "title_must": ["kelly handbag", "25"],
        "title_exclude": [],
    },
    {
        "query": "Kelly 28",
        "max_listings": 50,
        "min_price": 8000,
        "canonical": "Hermès Kelly 28",
        "title_must": ["kelly handbag", "28"],
        "title_exclude": [],
    },
]

# Speedy 25 needs an extra size check — "speedy 25" or "speedy b 25" are
# fine; "speedy bandouliere 25" is also the same bag but the 25 size.
# The title_exclude above handles the wrong sizes.


# ── Helpers ─────────────────────────────────────────────────────────────
def apify_post(url, body, token, timeout=600):
    """POST with 3-attempt retry. Returns parsed JSON list of items."""
    data = json.dumps(body).encode("utf-8")
    req = Request(
        f"{url}?token={token}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_err = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError) as e:
            last_err = e
            if attempt < 3:
                print(f"  attempt {attempt}/3 failed: {e} — retrying in 15s")
                time.sleep(15)
            else:
                print(f"  attempt {attempt}/3 failed: {e} — giving up on this model")
    raise last_err


def passes_title_filter(title, cfg):
    """Returns True if the listing title passes the must/exclude checks."""
    t = title.lower()
    for must in cfg["title_must"]:
        if must not in t:
            return False
    for excl in cfg["title_exclude"]:
        if excl in t:
            return False
    return True


def scrape_one_model(cfg, token):
    """Runs the Rebag actor for a single model config.
    Returns filtered list of items (may be empty on failure — never raises,
    so one bad model doesn't kill the rest)."""
    url = f"https://api.apify.com/v2/acts/{REBAG_ACTOR}/run-sync-get-dataset-items"
    body = {
        "searchQueries": [cfg["query"]],
        "maxListingsPerQuery": cfg["max_listings"],
        "minPrice": cfg["min_price"],
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    try:
        raw_items = apify_post(url, body, token)
        print(f"  [{cfg['canonical']}] raw results: {len(raw_items)}")
    except Exception as e:
        print(f"  [{cfg['canonical']}] FAILED after retries ({e}) — skipping this model this week")
        return []

    # Post-scrape filter
    filtered = []
    for item in raw_items:
        title = item.get("title", "")
        price = item.get("price")

        if not title or price is None:
            continue
        if price < cfg["min_price"]:
            continue
        if not passes_title_filter(title, cfg):
            continue
        filtered.append(item)

    n_excluded = len(raw_items) - len(filtered)
    print(f"  [{cfg['canonical']}] after filter: {len(filtered)} listings ({n_excluded} excluded)")
    return filtered


# ── Processing ───────────────────────────────────────────────────────────
def process_rebag(items_by_model, msrp):
    """Computes VR metrics for each model. Returns {canonical: snapshot_dict}.

    Rebag sells authenticated pre-owned items — no replica risk, no
    keyword/price exclusion beyond what scrape_one_model() already did.
    No sold/createdAt available from this source → avg_days_to_sell = None.
    """
    import statistics
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    out = {}

    for canonical, items in items_by_model.items():
        if not items:
            continue

        region_msrp = msrp["models"].get(canonical, {}).get(REGION)
        if not region_msrp:
            print(f"  [{canonical}] no {REGION} MSRP in msrp_reference.json — skipping")
            continue

        prices = [item["price"] for item in items if item.get("price")]
        if not prices:
            print(f"  [{canonical}] no valid prices after processing — skipping")
            continue

        vr_values = [p / region_msrp for p in prices]

        n = len(prices)
        out[canonical] = {
            "date": today,
            "vr_median": round(statistics.median(vr_values), 4),
            "vr_mean": round(statistics.mean(vr_values), 4),
            "n_listings": n,
            "n_listings_warning": n < MIN_RELIABLE_SAMPLE,  # True = low confidence
            "n_sold_snapshot": None,          # not available from Rebag
            "avg_days_to_sell": None,         # not available from Rebag
            "price_median": round(statistics.median(prices), 2),
            "msrp_used": region_msrp,
            "currency": CURRENCY,
            "_source": "rebag",
        }

    return out


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    token = os.environ["APIFY_TOKEN"]
    msrp = load_msrp()

    items_by_model = {}
    print(f"Scraping {len(MODEL_CONFIGS)} models from Rebag ({REGION})...")

    for cfg in MODEL_CONFIGS:
        items = scrape_one_model(cfg, token)
        items_by_model[cfg["canonical"]] = items

    # Guard: if every model returned zero, something is broken upstream
    total = sum(len(v) for v in items_by_model.values())
    if total == 0:
        print("WARNING: zero listings across ALL models — likely an actor or auth issue. Not writing anything.")
        sys.exit(1)

    snapshot = process_rebag(items_by_model, msrp)
    if not snapshot:
        print("WARNING: items fetched but no model produced a valid snapshot. Check msrp_reference.json.")
        sys.exit(1)

    history, path = load_history(REGION)
    for model, entry in snapshot.items():
        upsert_entry(history, model, entry)
        warn = " ⚠️ LOW SAMPLE" if entry.get("n_listings_warning") else ""
        print(f"  SAVED {model}: VR median={entry['vr_median']:.3f}  n={entry['n_listings']}  price_median=${entry['price_median']}{warn}")

    save_history(history, path)
    print(f"Done -> {path}")


if __name__ == "__main__":
    main()
