"""
run_xianyu.py
─────────────────────────────────────────────────────────────────────────
Calls the Xianyu/Goofish actor synchronously with all 8 model keywords in
one bulk call — this actor's maxResults IS documented as per-keyword (not
global like Vestiaire's), so a single call is sufficient here.

Includes the "keyword": " " workaround: the actor's schema silently
re-injects its own default ("iPhone") for the singular `keyword` field
whenever it's empty/absent, which overrides the `keywords` bulk array.
A single space passes the actor's "not empty" check but is trimmed to
nothing internally, so the bulk array is used instead. See conversation
history (Sep 2026) for how this was discovered.

Usage:
    python run_xianyu.py

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
from compute_resale_v2 import process_xianyu, load_msrp, load_history, save_history

XIANYU_ACTOR = "sian.agency~xianyu-goofish-product-scraper"
MAX_RESULTS_PER_KEYWORD = 30

# Chinese search terms — one per tracked model. Order doesn't matter;
# matching back to canonical model names happens via MODEL_ALIASES in
# compute_resale_v2.py, keyed off _sourceKeyword / itemTitle content.
CHINESE_KEYWORDS = [
    "爱马仕 Birkin 25",
    "爱马仕 Birkin 30",
    "爱马仕 Kelly 25",
    "爱马仕 Kelly 28",
    "路易威登 Neverfull",
    "路易威登 Speedy 25",
    "路易威登 Capucines",
    "迪奥 戴妃包",
]


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
            print(f"  attempt {attempt}/3 failed: {e}" + (" — retrying in 20s" if attempt < 3 else " — giving up"))
            time.sleep(20)
    raise last_err


def main():
    token = os.environ["APIFY_TOKEN"]
    msrp = load_msrp()

    url = f"https://api.apify.com/v2/acts/{XIANYU_ACTOR}/run-sync-get-dataset-items"
    body = {
        "keywords": CHINESE_KEYWORDS,
        "keyword": " ",  # workaround — see module docstring
        "operation": "keywordSearch",
        "sort": "active",
        "maxResults": MAX_RESULTS_PER_KEYWORD,
    }
    print(f"Scraping {len(CHINESE_KEYWORDS)} keywords from Xianyu...")
    items = apify_post(url, body, token)
    print(f"Fetched {len(items)} total items")

    if not items:
        print("WARNING: zero items returned — likely an upstream issue, not just filtering. Not writing anything.")
        sys.exit(1)

    snapshot = process_xianyu(items, msrp)
    history, path = load_history("CN")

    if not snapshot:
        # All items got filtered out as likely replicas — a valid, meaningful
        # result for China, not a failure. Record it explicitly per model.
        print("All listings excluded as likely replicas — recording a zero-signal snapshot for each tracked model.")
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        for model in msrp["models"]:
            if msrp["models"][model].get("CN") is None:
                continue
            history.setdefault(model, []).append({
                "date": today, "vr_median": None, "vr_mean": None, "n_listings": 0,
                "n_excluded_as_likely_replica": len(items), "avg_days_to_sell": None,
                "avg_want_count": None, "price_median": None, "currency": "CNY",
                "_caveat": "No listings passed the authenticity price/keyword filter this run — Xianyu is unauthenticated, this is a valid (if uninformative) result, not a failure.",
            })
    else:
        for model, entry in snapshot.items():
            history.setdefault(model, []).append(entry)
            print(f"  SAVED {model}: VR median={entry['vr_median']:.3f}  n={entry['n_listings']}")

    save_history(history, path)
    print(f"Done -> {path}")


if __name__ == "__main__":
    main()
