import json, sys, datetime, time, random, re
from pathlib import Path
import cloudscraper
from bs4 import BeautifulSoup

# Curated models per brand (Option C). Keywords matched case-insensitively
# in the full model name (h4 + h5 concatenated).
CURATED = {
    "Rolex":     ["Submariner", "Daytona", "GMT-Master", "Datejust", "Explorer"],
    "Cartier":   ["Santos", "Tank", "Ballon Bleu", "Panthère", "Pasha"],
    "Hublot":    ["Big Bang", "Classic Fusion", "Spirit of Big Bang", "MP ", "Square Bang"],
    "TAG Heuer": ["Carrera", "Monaco", "Aquaracer", "Formula 1", "Autavia"],
    "Hermès":    ["Arceau", "Cape Cod", "Heure H", "Kelly", "Slim"],
    "Piaget":    ["Polo", "Altiplano", "Possession", "Limelight", "Piaget Polo Skeleton"],
}
MAX_PAGES = 50
HISTORY_FILE = Path("data/watches_history.json")

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

def parse_price(s):
    """'$11,350' -> 11350.0"""
    m = re.search(r"\$?([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None

def scrape_page(scraper, page_num):
    """Return list of dicts: {brand, model_full, retail, market}."""
    url = f"https://watchcharts.com/watches?page={page_num}"
    r = scraper.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    watches = []
    for a in soup.find_all("a", href=re.compile(r"/watch_model/[\d]+")):
        h4 = a.find("h4")
        h5 = a.find("h5")
        if not h4:
            continue
        ref = h4.get_text(strip=True)
        model = h5.get_text(strip=True) if h5 else ""
        full_name = f"{ref} {model}".strip()
        # brand = first token (Rolex, Cartier, Hublot, etc.)
        first_token = ref.split()[0] if ref else ""
        brand = first_token
        # handle multi-word brands
        if ref.lower().startswith("tag heuer"):
            brand = "TAG Heuer"
        elif ref.lower().startswith("hermès") or ref.lower().startswith("hermes"):
            brand = "Hermès"
        # extract prices from the watch-card-table
        table = a.find("table", class_=re.compile(r"watch-card-table"))
        retail = market = None
        if table:
            price_divs = table.find_all("div", class_=re.compile(r"\bh5\b"))
            prices = [parse_price(d.get_text(" ", strip=True)) for d in price_divs]
            prices = [p for p in prices if p]
            if len(prices) >= 2:
                retail, market = prices[0], prices[1]
            elif len(prices) == 1:
                retail = prices[0]
        if retail and market:
            watches.append({
                "brand": brand,
                "name": full_name,
                "retail": retail,
                "market": market,
            })
    return watches

def match_curated(watches_for_brand, keywords):
    """Return watches whose name contains any of the curated keywords."""
    matched = {}
    for w in watches_for_brand:
        name_l = w["name"].lower()
        for kw in keywords:
            if kw.lower() in name_l:
                matched.setdefault(kw, []).append(w)
                break
    return matched

def compute_brand_vr(watches):
    """Weighted by retail price."""
    if not watches:
        return None
    ratios = [(w["retail"], w["market"] / w["retail"]) for w in watches]
    total_w = sum(r for r, _ in ratios)
    if total_w == 0:
        return None
    weighted = sum(r * v for r, v in ratios) / total_w
    simple = sum(v for _, v in ratios) / len(ratios)
    mn = min(v for _, v in ratios)
    mx = max(v for _, v in ratios)
    return {
        "weighted_vr": round(weighted, 4),
        "simple_avg_vr": round(simple, 4),
        "min_vr": round(mn, 4),
        "max_vr": round(mx, 4),
        "n_watches": len(watches),
    }

def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() \
        else {b: [] for b in CURATED}

    scraper = make_scraper()
    try:
        scraper.get("https://watchcharts.com/", timeout=30)
    except Exception as e:
        print(f"warmup failed: {e}")

    all_watches = []
    for page in range(1, MAX_PAGES + 1):
        try:
            page_watches = scrape_page(scraper, page)
            print(f"Page {page}: {len(page_watches)} watches parsed")
            if not page_watches:
                print(f"  empty page, stopping")
                break
            all_watches.extend(page_watches)
        except Exception as e:
            print(f"  ERROR page {page}: {e}", file=sys.stderr)
            break
        time.sleep(random.uniform(1.5, 3))

    print(f"\nTotal watches collected: {len(all_watches)}")

    # group by brand
    by_brand = {}
    for w in all_watches:
        by_brand.setdefault(w["brand"], []).append(w)
    for b, lst in by_brand.items():
        print(f"  {b}: {len(lst)} watches")

    today = datetime.date.today().isoformat()
    for brand, keywords in CURATED.items():
        brand_watches = by_brand.get(brand, [])
        if not brand_watches:
            print(f"\n{brand}: NO watches found in any page — skipping")
            continue
        matched = match_curated(brand_watches, keywords)
        found_kws = list(matched.keys())
        missing_kws = [k for k in keywords if k not in matched]
        flat = [w for ws in matched.values() for w in ws]

        fallback = False
        if len(found_kws) < 3:
            # Fallback to Option B: top 5 by list order (Watchcharts relevance)
            fallback = True
            flat = brand_watches[:5]
            print(f"\n{brand}: only {len(found_kws)}/{len(keywords)} curated models found — using FALLBACK (top 5 by relevance)")
        else:
            print(f"\n{brand}: found {found_kws} ({len(flat)} refs); missing {missing_kws}")

        stats = compute_brand_vr(flat)
        if not stats:
            continue
        stats["date"] = today
        stats["fallback"] = fallback
        stats["matched_keywords"] = found_kws if not fallback else []
        history.setdefault(brand, [])
        history[brand] = [p for p in history[brand] if p["date"] != today]
        history[brand].append(stats)
        history[brand].sort(key=lambda p: p["date"])
        print(f"  {brand}: weighted_vr={stats['weighted_vr']:.2%} (n={stats['n_watches']}, fallback={fallback})")

    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"\nSaved {HISTORY_FILE}")

if __name__ == "__main__":
    main()
