"""
AB scraper diagnostic. Run on the server (where ab.gr is reachable):

    docker exec -ti price_monitor-worker-1 python3 ab_debug.py

Pinpoints WHICH stage is failing: discovery, fetch, or parse.
"""
import sys
import logging
logging.basicConfig(level=logging.INFO)

import ab
from fetcher import PoliteFetcher

fetcher = PoliteFetcher(ab.BASE_URL)

print("=" * 60)
print("STAGE 1: URL discovery / cache")
print("=" * 60)
cached = ab._load_cached_urls("prices.db")
print(f"cached URLs: {len(cached)}")
if cached:
    sample = cached[:3]
    print("sample cached URLs:")
    for u in sample:
        print("  ", u)
else:
    print("no cache; trying live discovery (small)…")
    sample = ab.discover_product_urls(fetcher, max_urls=5)
    print(f"discovered: {len(sample)}")
    for u in sample[:3]:
        print("  ", u)

if not sample:
    print("\n✗ DISCOVERY is the problem — no URLs at all.")
    sys.exit(1)

print("\n" + "=" * 60)
print("STAGE 2: fetch one product page")
print("=" * 60)
test_url = sample[0]
print("fetching via PoliteFetcher:", test_url)
resp = fetcher.get(test_url)
if resp is None:
    print("✗ PoliteFetcher.get returned None (non-200, robots, or network).")
    print("  Retrying with RAW requests to see the true status…")
    import requests
    try:
        raw = requests.get(test_url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PriceMonitor/1.0)"})
        print(f"  RAW HTTP status: {raw.status_code}")
        print(f"  RAW final URL: {raw.url}")
        print(f"  RAW length: {len(raw.text)} chars")
        print(f"  first 300 chars:\n{raw.text[:300]}")
        # save for inspection
        with open("/tmp/ab_sample.html", "w", encoding="utf-8") as f:
            f.write(raw.text)
        print("  raw HTML saved to /tmp/ab_sample.html")
    except Exception as e:
        print("  RAW request also failed:", e)
    sys.exit(1)
print(f"HTTP status: {resp.status_code}")
print(f"content length: {len(resp.text)} chars")
print(f"final URL (after redirects): {resp.url}")

html = resp.text
# Signals that tell us what KIND of page we got back
print("\n--- page content signals ---")
print("has '<script type=\"application/ld+json\"':",
      'application/ld+json' in html)
print("has '\"@type\":\"Product\"' (any spacing):",
      '"Product"' in html and 'ld+json' in html)
print("has 'Just a moment' (Cloudflare challenge):", 'Just a moment' in html)
print("has 'captcha':", 'captcha' in html.lower())
print("has '__NEXT_DATA__' (Next.js SSR JSON):", '__NEXT_DATA__' in html)
print("has 'window.__INITIAL_STATE__':", '__INITIAL_STATE__' in html)
print("looks like an SPA shell (<div id=\"root\"></div>):",
      '<div id="root"></div>' in html or '<div id="app"></div>' in html)

# Dump the first JSON-LD block if present, so we can see its shape
import re
m = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
              html, re.DOTALL | re.IGNORECASE)
if m:
    block = m.group(1).strip()
    print("\n--- first JSON-LD block (first 600 chars) ---")
    print(block[:600])
else:
    print("\n✗ NO JSON-LD block found in the page at all.")

print("\n" + "=" * 60)
print("STAGE 3: parser result")
print("=" * 60)
data = ab.parse_product(test_url, html)
print("parse_product returned:", data)
if not data:
    print("\n✗ PARSER returned None — markup likely changed (see signals above).")
else:
    print("\n✓ parser works on this page; the issue may be elsewhere.")

# Save the raw HTML so we can inspect / share it
with open("/tmp/ab_sample.html", "w", encoding="utf-8") as f:
    f.write(html)
print("\nRaw HTML saved to /tmp/ab_sample.html")
print("Copy it out with:  docker cp price_monitor-worker-1:/tmp/ab_sample.html .")
