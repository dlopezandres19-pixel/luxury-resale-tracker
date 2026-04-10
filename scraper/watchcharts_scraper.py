import json, sys, datetime, time, random, re, base64
from pathlib import Path
import cloudscraper
from bs4 import BeautifulSoup

# Brand ID on Watchcharts (internal filter "-2")
BRAND_IDS = {
    "Rolex":     "24",
    "Cartier":   "52",
    "Hublot":    "259",
    "TAG Heuer": "51",
    "Hermès":    "455",
    "Piaget":    "226",
}

# Curated models (keywords matched in h4+h5 text)
CURATED = {
    "Rolex":     ["Submariner", "Daytona", "GMT-Master", "Datejust", "Explorer"],
    "Cartier":   ["Santos", "Tank", "Ballon Bleu", "Panthère", "Pasha"],
    "Hublot":    ["Big Bang", "Classic Fusion", "Spirit", "MP-", "King Power"],,
    "TAG Heuer": ["Carrera", "Monaco", "Aquaracer", "Formula 1", "Autavia"],
    "Hermès":    ["Arceau", "Cape Cod", "Heure H", "Kelly", "Slim"],
    "Piaget":    ["Polo", "Altiplano", "Possession", "Limelight", "Piaget Polo Skeleton"],
}
MAX_PAGES_PER_BRAND = 5
HISTORY_FILE = Path("data/watches_history.json")

def build_filter_param(brand_id):
    """Build ?filters=... base64 string for a brand ID."""
    payload = json.dumps({"-2": [brand_id]}, separators=(",", ":"))
    return base64.b64encode(payload.encode()).decode()

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

def parse_price(s):
    m = re.search(r"\$?([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None

def scrape_page(scraper, url, max_retries=3):
    last_err = None
    for attempt in range(max_retries):
        try:
            r = scraper.get(url, timeout=30)
           if r.status_code == 403:
                wait = 30 * (attempt + 1)
                print(f"    403 on attempt {attempt+1}, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 400:
                print(f"    400 Bad Request — page does not exist, stopping pagination")
                return None  # signal "no more pages"
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            wait = 20 * (attempt + 1)
            print(f"    error attempt {attempt+1}: {e}, waiting {wait}s...")
            time.sleep(wait)
    raise last_err or Exception("all retries failed")

def parse_watches(html, debug_label=""):
    soup = BeautifulSoup(html, "html.parser")
    watches = []
    raw_links = soup.find_all("a", href=re.compile(r"/watch_model/\d+"))
    if debug_label:
        # Also look for common empty-state indicators
        text_lower = soup.get_text(" ", strip=True).lower()
        no_results = any(s in text_lower for s in ["no results", "no watches", "0 results"])
        print(f"    [debug {debug_label}] raw watch links: {len(raw_links)}, empty-state: {no_results}, html_len: {len(html)}")
    for a in raw_links:
        h4 = a.find("h4")
        h5 = a.find("h5")
        if not h4:
            continue
        ref = h4.get_text(strip=True)
        model = h5.get_text(strip=True) if h5 else ""
        full_name = f"{ref} {model}".strip()
        table = a.find("table", class_=re.compile(r"watch-card-table"))
        retail = market = None
        if table:
            price_divs = table.find_all("div", class_=re.compile(r"\bh5\b"))
            prices = [parse_price(d.get_text(" ", strip=True)) for d in price_divs]
            prices = [p for p in prices if p]
            if len(prices) >= 2:
                retail, market = prices[0], prices[1]
        if retail and market:
            watches.append({"name": full_name, "retail": retail, "market": market})
    return watches

def scrape_brand(scraper, brand, brand_id):
    """Fetch all pages for one brand using the base64 filter."""
    fparam = build_filter_param(brand_id)
    all_watches = []
    seen_names = set()
    for page in range(1, MAX_PAGES_PER_BRAND + 1):
        sep = "&" if page > 1 else ""
        page_q = f"&page={page}" if page > 1 else ""
        url = f"https://watchcharts.com/watches?filters={fparam}{page_q}"
        print(f"  {brand} page {page}: {url}")
        try:
            html = scrape_page(scraper, url)
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            break
        if html is None:
            break  # no more pages
        watches = parse_watches(html, debug_label=f"{brand} p{page}")
        print(f"    parsed {len(watches)} watches")
        new_count = 0
        for w in watches:
            if w["name"] not in seen_names:
                seen_names.add(w["name"])
                all_watches.append(w)
                new_count += 1
        if new_count == 0:
            print(f"    no new watches, stopping pagination for {brand}")
            break
        time.sleep(random.uniform(4, 7))
    return all_watches

def match_curated(watches, keywords):
    matched = {}
    for w in watches:
        nl = w["name"].lower()
        for kw in keywords:
            if kw.lower() in nl:
                matched.setdefault(kw, []).append(w)
                break
    return matched

def compute_stats(watches):
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
        else {b: [] for b in BRAND_IDS}

    scraper = make_scraper()
    try:
        scraper.get("https://watchcharts.com/", timeout=30)
    except Exception as e:
        print(f"warmup failed: {e}")

    today = datetime.date.today().isoformat()

    for brand, brand_id in BRAND_IDS.items():
        print(f"\n=== {brand} (ID {brand_id}) ===")
        try:
            brand_watches = scrape_brand(scraper, brand, brand_id)
        except Exception as e:
            print(f"  FATAL: {e}", file=sys.stderr)
            continue
        print(f"  total unique watches: {len(brand_watches)}")
        if not brand_watches:
            print(f"  {brand}: no data — skipping")
            continue

        keywords = CURATED[brand]
        matched = match_curated(brand_watches, keywords)
        found_kws = list(matched.keys())
        missing_kws = [k for k in keywords if k not in matched]
        flat = [w for ws in matched.values() for w in ws]

        fallback = False
        if len(found_kws) < 3:
            fallback = True
            flat = brand_watches[:5]
            print(f"  FALLBACK: only {len(found_kws)}/{len(keywords)} curated found; using top 5 by relevance")
        else:
            print(f"  matched {found_kws} ({len(flat)} refs); missing {missing_kws}")

        stats = compute_stats(flat)
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
        time.sleep(random.uniform(3, 6))  # polite pause between brands

    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"\nSaved {HISTORY_FILE}")

if __name__ == "__main__":
    main()
