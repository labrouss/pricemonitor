"""
ab.gr retailer module.

Robots-compliant approach (per ab.gr/robots.txt):
  - DISCOVERY via the sitemap (allowed).
  - PARSE product detail pages /el/eshop/.../p/{code} (allowed).
  - We NEVER touch /search, /api/*search, /en/, or account/checkout paths
    (all disallowed). The fetcher enforces this too.

AB is one product per page fetch (no category-level price harvesting like
Sklavenitis), so a full crawl is slower. Use max_products to bound runs.

Known limitations:
  - No EAN/barcode in AB's JSON-LD (confirmed). Cross-chain matching will need
    name/brand/size, handled later in an analysis layer.
  - AB product 'sku' is its Hybris product code (the /p/{code} segment), which
    is AB-internal and not shared with other chains.
"""

import gzip
import re
import logging
from xml.etree import ElementTree as ET

from fetcher import PoliteFetcher
from ab_parser import extract_ab_product

logger = logging.getLogger(__name__)

RETAILER = "ab"
BASE_URL = "https://www.ab.gr"
GREEK_SITEMAP_INDEX = "https://www.ab.gr/sitemapgr/delhaizesitemapindex.xml"
ENGLISH_SITEMAP_INDEX = "https://www.ab.gr/sitemap/delhaizesitemapindex.xml"

# Allowed Greek product URLs only.
PRODUCT_HINT = re.compile(r"/el/eshop/.*/p/\w+")
# Product code is the trailing /p/{code} segment.
CODE_RE = re.compile(r"/p/(\w+)")


def _parse_xml(content):
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError:
            return [], []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sms = [e.text.strip() for e in root.findall(".//sm:sitemap/sm:loc", ns) if e.text]
    pages = [e.text.strip() for e in root.findall(".//sm:url/sm:loc", ns) if e.text]
    if not sms and not pages:
        for loc in root.iter():
            if loc.tag.endswith("loc") and loc.text:
                pages.append(loc.text.strip())
    return sms, pages


def discover_product_urls(fetcher, max_urls=100):
    """
    Discover allowed Greek product URLs from the sitemap.

    Primary: walk the Greek sitemap index, keep /el/ product URLs.
    Fallback: if none found, take English sitemap URLs and swap the locale
    segment /en/eshop/ -> /el/eshop/ (the /p/{code} is locale-independent).
    """
    product_urls = []
    seen = set()
    queue = [GREEK_SITEMAP_INDEX]

    while queue and len(product_urls) < max_urls:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        if not fetcher.can_fetch(sm):
            continue
        r = fetcher.get(sm)
        if r is None:
            continue
        child, pages = _parse_xml(r.content)
        for c in child:
            if c not in seen:
                queue.append(c)
        for u in pages:
            if PRODUCT_HINT.search(u):
                product_urls.append(u)
                if len(product_urls) >= max_urls:
                    break

    if not product_urls:
        logger.info("No /el/ URLs from Greek sitemap; deriving via locale swap")
        r = fetcher.get(ENGLISH_SITEMAP_INDEX)
        if r:
            child, _ = _parse_xml(r.content)
            for c in child:
                rr = fetcher.get(c)
                if rr is None:
                    continue
                _, pages = _parse_xml(rr.content)
                for u in pages:
                    if "/en/eshop/" in u and "/p/" in u:
                        product_urls.append(u.replace("/en/eshop/", "/el/eshop/"))
                        if len(product_urls) >= max_urls:
                            break
                if len(product_urls) >= max_urls:
                    break

    logger.info("Discovered %d AB product URLs", len(product_urls))
    return product_urls[:max_urls]


def scrape_product(fetcher, url):
    """Fetch and parse one AB product page. Returns dict or None."""
    if not fetcher.can_fetch(url):
        logger.debug("robots disallows %s", url)
        return None
    r = fetcher.get(url)
    if r is None:
        return None
    data = extract_ab_product(r.text)
    if data and data.get("price") is not None:
        # Use the Hybris product code as sku if JSON-LD didn't supply one.
        if not data.get("sku"):
            m = CODE_RE.search(url)
            if m:
                data["sku"] = m.group(1)
        data["url"] = url
        return data
    return None


def parse_product(url, html):
    """Parse already-fetched product HTML -> dict or None."""
    data = extract_ab_product(html)
    if data and data.get("price") is not None:
        if not data.get("sku"):
            m = CODE_RE.search(url)
            if m:
                data["sku"] = m.group(1)
        data["url"] = url
        return data
    return None


def _cache_path(db_path):
    import os
    base = os.path.dirname(os.path.abspath(db_path)) if db_path else "."
    return os.path.join(base, "ab_urls_cache.txt")


def _load_cached_urls(db_path):
    import os
    p = _cache_path(db_path)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    return []


def _save_cached_urls(db_path, urls):
    try:
        with open(_cache_path(db_path), "w", encoding="utf-8") as fh:
            fh.write("\n".join(urls))
    except OSError:
        pass


def run(store, max_products=50, workers=4, pace=0.75, db_path="prices.db",
        refresh_urls=False):
    """
    Discover + scrape AB products into the store, concurrently.

    AB serves one product per page, so we fetch product pages with a small
    polite thread pool (workers) paced to ~1/pace requests/sec overall. This
    is only done because ab.gr declares NO crawl-delay; if it did, the fetcher
    falls back to honoring it.

    Discovered URLs are cached to disk so repeat runs skip the slow sitemap
    walk. Pass refresh_urls=True to rebuild the cache.
    """
    fetcher = PoliteFetcher(BASE_URL)

    urls = [] if refresh_urls else _load_cached_urls(db_path)
    if urls:
        logger.info("Using %d cached AB product URLs (refresh_urls=True to rebuild)",
                    len(urls))
    else:
        urls = discover_product_urls(fetcher, max_urls=max(max_products * 2, 2000))
        if urls:
            _save_cached_urls(db_path, urls)
    if not urls:
        logger.warning("No AB product URLs discovered.")
        return 0

    targets = urls[:max_products]
    count = 0
    for url, resp in fetcher.get_many(targets, workers=workers, pace=pace):
        if resp is None:
            continue
        data = parse_product(url, resp.text)
        if not data:
            continue
        pid = store.upsert_product(
            retailer=RETAILER,
            name=data.get("name") or "(unknown)",
            url=url,
            sku=data.get("sku"),
            brand=data.get("brand"),
            category=data.get("category"),
            image_url=data.get("image_url"),
        )
        store.record_price(
            product_id=pid,
            price=data["price"],
            list_price=data.get("list_price"),
            in_stock=data.get("in_stock"),
            currency=data.get("currency", "EUR"),
        )
        count += 1
        if count % 50 == 0:
            logger.info("AB: recorded %d products so far", count)

    logger.info("AB: recorded %d product observations (workers=%d, pace=%.2fs)",
                count, workers, pace)
    return count
