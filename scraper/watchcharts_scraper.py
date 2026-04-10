import sys
import cloudscraper
from bs4 import BeautifulSoup

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

def probe_page(scraper, page_num):
    url = f"https://watchcharts.com/watches?page={page_num}"
    print(f"\n=== Probing page {page_num} ===")
    print(f"URL: {url}")
    try:
        r = scraper.get(url, timeout=30)
        print(f"Status: {r.status_code}")
        print(f"HTML length: {len(r.text)} chars")
        if r.status_code != 200:
            print(f"First 300 chars: {r.text[:300]}")
            return
        html = r.text
        probes = {
            "Retail Price": html.count("Retail Price"),
            "Market Price": html.count("Market Price"),
            "$ signs": html.count("$"),
            "Rolex mentions": html.lower().count("rolex"),
            "Cartier mentions": html.lower().count("cartier"),
            "cloudflare challenge": int("cf-challenge" in html.lower() or "just a moment" in html.lower()),
            "script tags": html.count("<script"),
        }
        for k, v in probes.items():
            print(f"  {k}: {v}")
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        print(f"Visible text length: {len(text)}")
        idx = text.find("Retail Price")
        if idx >= 0:
            snippet = text[max(0, idx-100):idx+400]
            print(f"Snippet near 'Retail Price':\n{snippet}")
        else:
            print("'Retail Price' NOT found in visible text (possibly JS-rendered)")
        import re
        results_match = re.search(r"([\d,]+)\s*results", text, re.IGNORECASE)
        if results_match:
            print(f"Results count found: {results_match.group(1)}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)

def main():
    scraper = make_scraper()
    try:
        r = scraper.get("https://watchcharts.com/", timeout=30)
        print(f"Homepage warmup status: {r.status_code}, HTML length: {len(r.text)}")
    except Exception as e:
        print(f"warmup failed: {e}")
    for page in [1, 2]:
        probe_page(scraper, page)

if __name__ == "__main__":
    main()
