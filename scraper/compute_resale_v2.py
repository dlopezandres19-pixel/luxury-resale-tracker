"""
compute_resale_v2.py
─────────────────────────────────────────────────────────────────────────
Called by the GitHub Action after an Apify webhook fires (ACTOR.RUN.SUCCEEDED).
Pulls the finished run's dataset from the Apify API, computes Value Retention
(VR) + liquidity metrics, and appends one new snapshot to the region's
historical JSON file in data_v2/.

Usage:
    python compute_resale_v2.py --source vestiaire --region US --dataset-id <id>
    python compute_resale_v2.py --source vestiaire --region EU --dataset-id <id>
    python compute_resale_v2.py --source xianyu     --region CN --dataset-id <id>

Env vars required:
    APIFY_TOKEN   — Apify API token (repo secret)
"""
import argparse
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data_v2"
MSRP_PATH = REPO_ROOT / "msrp_reference.json"

# Maps the free-text "model" field returned by each scraper to our
# canonical model names used as keys in msrp_reference.json.
MODEL_ALIASES = {
    "birkin 25": "Hermès Birkin 25",
    "birkin 30": "Hermès Birkin 30",
    "kelly 25": "Hermès Kelly 25",
    "kelly 28": "Hermès Kelly 28",
    "neverfull": "LV Neverfull MM",
    "speedy": "LV Speedy 25",
    "capucines": "LV Capucines BB",
    "lady dior": "Dior Lady Dior Small",
}

# Xianyu-specific: reject listings whose title contains any of these —
# common markers for replicas / non-genuine items on an unauthenticated
# P2P marketplace. NOT exhaustive — a heuristic filter, not a guarantee.
XIANYU_EXCLUDE_KEYWORDS = ["拼皮", "复刻", "高仿", "match", "顶级版", "订制", "代工"]
# Minimum plausible resale price (CNY) below which a listing is almost
# certainly not a genuine item of that model. Set relative to CN MSRP
# (~75-80% of retail as a floor) — a genuine bag rarely sells for much
# less than that even used; well-known replica price bands sit far below.
# Revised 2026-09-10 after v1 thresholds (30k flat) let convincing
# high-price replicas through — see _caveat in output.
XIANYU_MIN_PRICE = {
    "Hermès Birkin 25": 90000,
    "Hermès Birkin 30": 95000,
    "Hermès Kelly 25": 90000,
    "Hermès Kelly 28": 85000,
    "LV Neverfull MM": 9000,
    "LV Speedy 25": 9000,
    "LV Capucines BB": 35000,
    "Dior Lady Dior Small": 30000,
}


def apify_get(url):
    token = os.environ["APIFY_TOKEN"]
    sep = "&" if "?" in url else "?"
    req = Request(f"{url}{sep}token={token}")
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_dataset_items(dataset_id=None, run_id=None):
    """Fetch dataset items. If dataset_id isn't provided (or the webhook
    template variable failed to resolve), fall back to resolving it from
    the run_id first — actorRunId is confirmed reliably available in
    Apify's webhook payload, unlike resource.defaultDatasetId."""
    if not dataset_id and run_id:
        run_info = apify_get(f"https://api.apify.com/v2/actor-runs/{run_id}")
        dataset_id = run_info["data"]["defaultDatasetId"]
        print(f"Resolved datasetId from runId {run_id} -> {dataset_id}")
    if not dataset_id:
        raise ValueError("No dataset_id available (neither passed directly nor resolvable from run_id)")
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?format=json&clean=true"
    return apify_get(url)


def canonical_model(raw_model_or_title):
    s = raw_model_or_title.lower()
    for alias, canon in MODEL_ALIASES.items():
        if alias in s:
            return canon
    return None


def load_msrp():
    return json.loads(MSRP_PATH.read_text(encoding="utf-8"))


def load_history(region):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{region.lower()}_resale.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), path
    return {}, path


def save_history(history, path):
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def process_vestiaire(items, region, msrp):
    """region is 'US' or 'EU'. Returns {model: snapshot_dict}."""
    today = datetime.now(timezone.utc).date().isoformat()
    by_model = {}
    for it in items:
        model = canonical_model(it.get("model", "") or it.get("searchQuery", ""))
        if not model or model not in msrp["models"]:
            continue
        by_model.setdefault(model, []).append(it)

    out = {}
    for model, listings in by_model.items():
        region_msrp = msrp["models"][model].get(region)
        if not region_msrp:
            continue
        prices = [l["priceAmount"] for l in listings if l.get("priceAmount")]
        if not prices:
            continue
        vr_values = [p / region_msrp for p in prices]

        sold = [l for l in listings if l.get("sold")]
        days_to_sell = []
        for l in sold:
            created = l.get("createdAt")
            if not created:
                continue
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                days_to_sell.append((datetime.now(timezone.utc) - created_dt).days)
            except ValueError:
                continue

        out[model] = {
            "date": today,
            "vr_median": round(statistics.median(vr_values), 4),
            "vr_mean": round(statistics.mean(vr_values), 4),
            "n_listings": len(listings),
            "n_sold_snapshot": len(sold),
            "avg_days_to_sell": round(statistics.mean(days_to_sell), 1) if days_to_sell else None,
            "price_median": round(statistics.median(prices), 2),
            "currency": listings[0].get("priceCurrency", "USD" if region == "US" else "EUR"),
        }
    return out


def process_xianyu(items, msrp):
    """China. Filters likely-replica listings before computing anything.
    No `sold`/`createdAt` available from this source — liquidity is
    limited to wantCount (demand proxy), NOT true days-to-sell."""
    today = datetime.now(timezone.utc).date().isoformat()
    by_model = {}
    n_excluded_keyword = 0
    n_excluded_price = 0
    for it in items:
        title = it.get("itemTitle", "")
        model = canonical_model(it.get("_sourceKeyword", "") or title)
        if not model:
            continue
        if any(kw in title for kw in XIANYU_EXCLUDE_KEYWORDS):
            n_excluded_keyword += 1
            continue
        price = it.get("priceYuan")
        min_price = XIANYU_MIN_PRICE.get(model, 0)
        if not price or price < min_price:
            n_excluded_price += 1
            continue
        by_model.setdefault(model, []).append(it)

    out = {}
    for model, listings in by_model.items():
        region_msrp = msrp["models"][model].get("CN")
        if not region_msrp:
            continue
        prices = [l["priceYuan"] for l in listings]
        vr_values = [p / region_msrp for p in prices]
        want_counts = [l.get("wantCount", 0) for l in listings if l.get("wantCount") is not None]

        out[model] = {
            "date": today,
            "vr_median": round(statistics.median(vr_values), 4),
            "vr_mean": round(statistics.mean(vr_values), 4),
            "n_listings": len(listings),
            "n_excluded_as_likely_replica": n_excluded_keyword + n_excluded_price,
            "avg_days_to_sell": None,  # not available from this source
            "avg_want_count": round(statistics.mean(want_counts), 1) if want_counts else None,
            "price_median": round(statistics.median(prices), 2),
            "currency": "CNY",
            "_caveat": "Unauthenticated P2P marketplace — VR may include undetected replicas despite filtering",
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["vestiaire", "xianyu"])
    ap.add_argument("--region", required=True, choices=["US", "EU", "CN"])
    ap.add_argument("--dataset-id", default=None)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    msrp = load_msrp()
    items = fetch_dataset_items(dataset_id=args.dataset_id, run_id=args.run_id)
    print(f"Fetched {len(items)} items from dataset")

    if args.source == "vestiaire":
        snapshot = process_vestiaire(items, args.region, msrp)
    else:
        snapshot = process_xianyu(items, msrp)

    if not snapshot:
        if args.source == "xianyu":
            # For China specifically, "everything got filtered out as a likely
            # replica" is a legitimate, meaningful result — not a bug. Record
            # it explicitly instead of failing silently or erroring out.
            print("All listings excluded as likely replicas — recording a zero-signal snapshot for each tracked model.")
            history, path = load_history(args.region)
            today = datetime.now(timezone.utc).date().isoformat()
            for model in msrp["models"]:
                if msrp["models"][model].get("CN") is None:
                    continue
                history.setdefault(model, []).append({
                    "date": today,
                    "vr_median": None,
                    "vr_mean": None,
                    "n_listings": 0,
                    "n_excluded_as_likely_replica": len(items),
                    "avg_days_to_sell": None,
                    "avg_want_count": None,
                    "price_median": None,
                    "currency": "CNY",
                    "_caveat": "No listings passed the authenticity price/keyword filter this run — Xianyu is unauthenticated, this is a valid (if uninformative) result, not a failure.",
                })
            save_history(history, path)
            print(f"Saved (zero-signal) -> {path}")
            return
        print("WARNING: no models matched — nothing to write. Check MODEL_ALIASES / dataset fields.")
        sys.exit(1)

    history, path = load_history(args.region)
    for model, entry in snapshot.items():
        history.setdefault(model, []).append(entry)
        print(f"  {model}: VR median={entry['vr_median']:.3f}  n={entry['n_listings']}")

    save_history(history, path)
    print(f"Saved -> {path}")


if __name__ == "__main__":
    main()
