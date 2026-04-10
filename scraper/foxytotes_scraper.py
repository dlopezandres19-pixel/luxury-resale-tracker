import json, re, sys, datetime, time, random
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

HISTORY_FILE = Path("data/handbags_history.json")

def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    # Warm up the session by hitting the homepage first (gets cookies)
    try:
        s.get("https://www.foxytotes.com/", timeout=30)
    except Exception as e:
        print(f"  warmup failed (non-fatal): {e}", file=sys.stderr)
    return s

def scrape_designer(session, url):
    r = session.get(url, timeout=30)
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

    session = make_session()
    today = datetime.date.today().isoformat()

    for designer, url in DESIGNERS.items():
        print(f"Scraping {designer}...")
        try:
            bags = scrape_designer(session, url)
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
        time.sleep(random.uniform(2, 4))  # polite delay between requests

    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"Saved {HISTORY_FILE}")

if __name__ == "__main__":
    main()
