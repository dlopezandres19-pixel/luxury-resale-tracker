import sys, re, json
import cloudscraper
from bs4 import BeautifulSoup

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

def probe_scripts(scraper, page_num):
    url = f"https://watchcharts.com/watches?page={page_num}"
    print(f"\n=== Probing scripts on page {page_num} ===")
    r = scraper.get(url, timeout=30)
    print(f"Status: {r.status_code}")
    if r.status_code != 200:
        return
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script")
    print(f"Total <script> tags: {len(scripts)}")

    # Look for common data-embedding patterns
    patterns_of_interest = [
        "__NEXT_DATA__",
        "__INITIAL_STATE__",
        "__NUXT__",
        "window.__",
        "Submariner",
        "126610",
        "retail_price",
        "retailPrice",
        "market_price",
        "marketPrice",
        '"brand"',
        '"model"',
    ]

    # Scan each script and count matches
    for i, script in enumerate(scripts):
        content = script.string or ""
        if not content:
            continue
        hits = {p: content.count(p) for p in patterns_of_interest if p in content}
        if hits:
            print(f"\nScript #{i} (length {len(content)}):")
            for k, v in hits.items():
                print(f"  '{k}': {v}")
            # dump a small snippet if it looks JSON-ish
            if "retailPrice" in content or "retail_price" in content or "Submariner" in content:
                # find the first occurrence of something interesting
                for needle in ["Submariner", "retailPrice", "retail_price"]:
                    idx = content.find(needle)
                    if idx >= 0:
                        snippet = content[max(0, idx-80):idx+250]
                        print(f"  snippet around '{needle}': {snippet!r}")
                        break

    # Also look for __NEXT_DATA__ specifically
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        print(f"\n__NEXT_DATA__ FOUND, length: {len(next_data.string or '')}")
        try:
            data = json.loads(next_data.string)
            print(f"Top-level keys: {list(data.keys())}")
            # try to navigate to something interesting
            def walk(d, path="", depth=0):
                if depth > 6: return
                if isinstance(d, dict):
                    for k, v in d.items():
                        if any(term in k.lower() for term in ["watch", "product", "result", "item", "list", "price"]):
                            print(f"  key: {path}.{k} type={type(v).__name__}")
                        walk(v, f"{path}.{k}", depth+1)
                elif isinstance(d, list) and d and depth < 5:
                    walk(d[0], f"{path}[0]", depth+1)
            walk(data)
        except Exception as e:
            print(f"  could not parse as JSON: {e}")
    else:
        print("\n__NEXT_DATA__ NOT found")

def main():
    scraper = make_scraper()
    try:
        scraper.get("https://watchcharts.com/", timeout=30)
    except Exception as e:
        print(f"warmup failed: {e}")
    probe_scripts(scraper, 1)

if __name__ == "__main__":
    main()
