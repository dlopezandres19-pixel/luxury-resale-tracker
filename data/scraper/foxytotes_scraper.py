import json, re, sys, datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup

DESIGNERS = {
    "Hermès": "https://www.foxytotes.com/designer/hermes/",
    "Louis Vuitton": "https://www.foxytotes.com/designer/louis-vuitton/",
    "Dior": "https://www.foxytotes.com/designer/dior/",
    "Gucci": "https://www.foxytotes.com/designer/gucci/",
    "Saint Laurent": "https://www.foxytotes.com/designer/saint-laurent/",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
HISTORY_FILE = Path("data/handbags_history.json")

def scrape_designer(url):
    """Return list of (msrp, retention) for every bag card on a designer page."""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    pattern = re.compile(
        r"\$([\d,]+(?:\.\d+)?)\s*Current Value\s*([\d.]+)\s*%\s*Retention",
        re.IGNORECASE,
    )
    bags = []
    for m in pattern.finditer(text):
        msrp = float(m.group(1).replace(",", ""))
        retention = float(m.group(2)) / 100
        bags.append((msrp, retention))
    return bags

def weighted_vr(bags):
    if not bags:
        return None, 0
    total_w = sum(msrp for msrp, _ in bags)
    if total_w == 0:
        return None, 0
    vr = sum(msrp * ret for msrp, ret in bags) / total_w
    return round(vr, 4), len(bags)

def main():
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())
    else:
        history = {d: [] for d in DESIGNERS}

    today = datetime.date.today().isoformat()
    for designer, url in DESIGNERS.items():
        print(f"Scraping {designer}...")
        try:
            bags = scrape_designer(url)
            vr, n = weighted_vr(bags)
            if vr is None:
                print(f"  WARN: no bags parsed for {designer}")
                continue
            history.setdefault(designer, [])
            history[designer] = [p for p in history[designer] if p["date"] != today]
            history[designer].append({"date": today, "weighted_vr": vr, "n_bags": n})
            history[designer].sort(key=lambda p: p["date"])
            print(f"  {designer}: VR={vr:.2%} (n={n})")
        except Exception as e:
            print(f"  ERROR {designer}: {e}", file=sys.stderr)

    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"Saved {HISTORY_FILE}")

if __name__ == "__main__":
    main()
