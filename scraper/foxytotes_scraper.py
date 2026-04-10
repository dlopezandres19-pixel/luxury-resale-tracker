import json, re, sys, datetime, time, random
from pathlib import Path
import cloudscraper
from bs4 import BeautifulSoup

# Designer display name -> (search query, slug used in /designer/<slug>/ links)
DESIGNERS = {
    "Hermès":        ("hermes",        "hermes"),
    "Louis Vuitton": ("louis-vuitton", "louis-vuitton"),
    "Dior":          ("dior",          "dior"),
    "Gucci":         ("gucci",         "gucci"),
    "Saint Laurent": ("saint-laurent", "saint-laurent"),
}
BASE = "https://www.foxytotes.com/tracker/"
HISTORY_FILE = Path("data/handbags_history.json")

def fetch(scraper, url):
    r = scraper.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def parse_cards_for_slug(html, slug):
    """Return list of (msrp, retention) for bags whose designer link matches slug."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    designer_re = re.compile(rf"/designer/{re.escape(slug)}/?$")
    for card_link in soup.find_all("a", href=designer_re):
        container = card_link
        for _ in range(7):
            container = container.parent
            if container is None:
                break
            text = container.get_text(" ", strip=True)
            m = re.search(
                r"\$([\d,]+(?:\.\d+)?)\s*Current Value\s*([\d.]+)\s*%\s*Retention",
                text, re.IGNORECASE)
            if m:
                msrp = float(m.group(1).replace(",", ""))
                retention = float(m.group(2)) / 100
                key = (round(msrp, 2), round(retention, 4))
                if key not in seen:
                    seen.add(key)
                    results.append((msrp, retention))
                break
    return results

def scrape_designer(scraper, search_q, slug):
    """Scrape all pages for one designer via ?_search= filter."""
    all_bags = []
    for page in range(1, 10):
        if page == 1:
            url = f"{BASE}?_search={search_q}"
        else:
            url = f"{BASE}page/{page}/?_search={search_q}"
        try:
            html = fetch(scraper, url)
        except Exception as e:
            if "404" in str(e):
                break
            print(f"    ERROR page {page}: {e}", file=sys.stderr)
            break
        cards = parse_cards_for_slug(html, slug)
        if not cards:
            break
        all_bags.extend(cards)
        # dedupe across pages
        seen = set()
        unique = []
        for msrp, ret in all_bags:
            k = (round(msrp, 2), round(ret, 4))
            if k not in seen:
                seen.add(k)
                unique.append((msrp, ret))
        all_bags = unique
        time.sleep(random.uniform(1.5, 3))
    return all_bags

def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() \
        else {d: [] for d in DESIGNERS}

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False})
    try:
        scraper.get("https://www.foxytotes.com/", timeout=30)
    except Exception as e:
        print(f"warmup failed: {e}", file=sys.stderr)

    today = datetime.date.today().isoformat()
    for designer, (search_q, slug) in DESIGNERS.items():
        print(f"Scraping {designer}...")
        try:
            bags = scrape_designer(scraper, search_q, slug)
            if not bags:
                print(f"  WARN: no bags for {designer}")
                continue
            total_w = sum(m for m, _ in bags)
            vr = round(sum(m * r for m, r in bags) / total_w, 4)
            simple_avg = round(sum(r for _, r in bags) / len(bags), 4)
            min_vr = round(min(r for _, r in bags), 4)
            max_vr = round(max(r for _, r in bags), 4)
            history.setdefault(designer, [])
            history[designer] = [p for p in history[designer] if p["date"] != today]
            history[designer].append({
                "date": today,
                "weighted_vr": vr,
                "simple_avg_vr": simple_avg,
                "min_vr": min_vr,
                "max_vr": max_vr,
                "n_bags": len(bags),
            })
            history[designer].sort(key=lambda p: p["date"])
            print(f"  {designer}: VR={vr:.2%} (n={len(bags)})")
        except Exception as e:
            print(f"  ERROR {designer}: {e}", file=sys.stderr)
        time.sleep(random.uniform(2, 4))

    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"Saved {HISTORY_FILE}")

if __name__ == "__main__":
    main()
