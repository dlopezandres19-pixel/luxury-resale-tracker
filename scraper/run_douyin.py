"""
run_douyin.py
─────────────────────────────────────────────────────────────────────────
Scrapes Douyin (抖音 / Chinese TikTok) weekly engagement data for 5 luxury
brands using the Apify actor khadinakbar/douyin-search-scraper.

Computes a Brand Engagement Score per brand:
  score = (total_views / 1M) + (total_likes + comments + shares + collects) * 5 / 1M

This is a proxy for the official Douyin Index (巨量算数) which requires
a paid ByteDance enterprise account. Our score is directional — use for
trend comparison across brands and over time, not as an absolute metric.

Cost: ~50 videos × $0.005 = $0.25/run

Usage:
    python run_douyin.py

Env vars required:
    APIFY_TOKEN
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data_v2"
OUTPUT_FILE = DATA_DIR / "douyin_data.json"

DOUYIN_ACTOR = "natanielsantos~douyin-scraper"
MAX_ITEMS_PER_BRAND = 50  # per keyword search

# Brand keywords in Chinese — how consumers actually search on Douyin.
# Also include English names as Douyin indexes both.
BRAND_KEYWORDS = {
    "Hermès":        "爱马仕",
    "Louis Vuitton": "路易威登",
    "Cartier":       "卡地亚",
    "Gucci":         "古驰",
}


def apify_post(url, body, token, timeout=1800):
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
                print(f"  attempt {attempt}/3 failed: {e} — giving up")
    raise last_err


def compute_brand_scores(items):
    """Groups items by searchQuery/_query field and computes engagement score per brand.
    natanielsantos actor returns items with a 'query' or searchTerm field."""
    by_keyword = {}
    for item in items:
        kw = item.get("searchKeyword", "")
        by_keyword.setdefault(kw, []).append(item)

    scores = {}
    for brand, keyword in BRAND_KEYWORDS.items():
        brand_items = by_keyword.get(keyword, [])
        if not brand_items:
            print(f"  {brand} ({keyword}): 0 videos — skipping")
            continue

        total_views    = sum(v.get("playCount", 0) or 0 for v in brand_items)
        # natanielsantos uses statistics.diggCount for likes
        total_likes    = sum((v.get("statistics", {}) or {}).get("diggCount", v.get("likeCount", 0) or 0) for v in brand_items)
        total_comments = sum((v.get("statistics", {}) or {}).get("commentCount", v.get("commentCount", 0) or 0) for v in brand_items)
        total_shares   = sum((v.get("statistics", {}) or {}).get("shareCount", v.get("shareCount", 0) or 0) for v in brand_items)
        total_collects = sum((v.get("statistics", {}) or {}).get("collectCount", v.get("collectCount", 0) or 0) for v in brand_items)
        total_engagement = total_likes + total_comments + total_shares + total_collects

        # Score in millions — based on engagement (views not available from this actor)
        # engagement × 10 gives scale comparable to WeChat Index
        score = round(total_engagement, 0)  # raw engagement count — formatted as K/M in dashboard

        scores[brand] = {
            "score":            score,
            "total_views":      total_views,
            "total_likes":      total_likes,
            "total_comments":   total_comments,
            "total_shares":     total_shares,
            "total_collects":   total_collects,
            "total_engagement": total_engagement,
            "n_videos":         len(brand_items),
            "keyword":          keyword,
        }
        print(f"  {brand}: n_videos={len(brand_items)}  engagement={total_engagement:,}")

    return scores


def upsert_entry(history, brand, entry):
    """Replace existing entry for the same date, or append."""
    arr = history.setdefault(brand, [])
    for i, existing in enumerate(arr):
        if existing.get("date") == entry.get("date"):
            arr[i] = entry
            return
    arr.append(entry)


def load_history():
    DATA_DIR.mkdir(exist_ok=True)
    if OUTPUT_FILE.exists():
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    return {}


def save_history(history):
    OUTPUT_FILE.write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main():
    token = os.environ["APIFY_TOKEN"]
    today = datetime.now(timezone.utc).date().isoformat()

    url = f"https://api.apify.com/v2/acts/{DOUYIN_ACTOR}/run-sync-get-dataset-items"

    # One call per brand — tags items reliably.
    # latest + last_week = videos published THIS week → captures weekly momentum (n_videos)
    print(f"Scraping Douyin for {len(BRAND_KEYWORDS)} brands ({MAX_ITEMS_PER_BRAND} videos each)...")
    all_items = []
    for brand, keyword in BRAND_KEYWORDS.items():
        body = {
            "searchTermsOrHashtags": [keyword],
            "searchSortFilter": "latest",
            "searchPublishTimeFilter": "last_week",
            "maxItemsPerUrl": MAX_ITEMS_PER_BRAND,
            "scrapePlayCount": True,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        }
        try:
            items = apify_post(url, body, token)
            print(f"  {brand} ({keyword}): {len(items)} videos")
            for item in items:
                item["searchKeyword"] = keyword
            all_items.extend(items)
        except Exception as e:
            print(f"  {brand} ({keyword}): FAILED ({e}) — skipping")
            continue
        time.sleep(5)

    if not all_items:
        print("WARNING: zero videos returned across all brands.")
        sys.exit(1)

    scores = compute_brand_scores(all_items)
    if not scores:
        print("WARNING: no brands matched any videos — check keywords.")
        sys.exit(1)

    history = load_history()
    for brand, metrics in scores.items():
        entry = {"date": today, **metrics}
        upsert_entry(history, brand, entry)

    save_history(history)
    print(f"Done -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
