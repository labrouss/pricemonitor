"""
lidl-hellas.gr retailer module (Nuxt SSR).

Discovery: sitemap (static/sitemap.xml -> product_sitemap.xml.gz) lists product
URLs (/p/<slug>/p<id>). Parse: each product page via JSON-LD + meta description
(see lidl_parser). One product per fetch.

IMPORTANT — bot protection:
Lidl sits behind an aggressive WAF (Akamai-style). Automated requests can draw
403s. This module is deliberately SERIAL and polite (no concurrency), and it
STOPS if it starts getting blocked rather than trying to evade. Do not add
user-agent rotation, proxies, or concurrency to get around blocks — that is
defeating an access control the operator put up on purpose. If Lidl blocks
sustained crawling, use a sanctioned source (e.g. the government price platform)
instead.

Some Lidl products are in-store-only with NO online price ("- undefined€");
those are skipped (no price to record).

No gtin/barcode exposed; sku is Lidl's internal article id.
"""

import gzip
import re
import logging
from xml.etree import ElementTree as ET

from fetcher import PoliteFetcher
from lidl_parser import extract_lidl_product

logger = logging.getLogger(__name__)

RETAILER = "lidl"
BASE_URL = "https://www.lidl-hellas.gr"
SITEMAP = "https://www.lidl-hellas.gr/static/sitemap.xml"
PRODUCT_HINT = re.compile(r"/p/[^/]+/p\d+")

# If we see this many consecutive blocked/failed fetches, assume the WAF is
# refusing us and STOP (do not try to evade).
BLOCK_STREAK_LIMIT = 8


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


def discover_product_urls(fetcher, max_urls=1000):
    """Collect product URLs from the sitemap tree."""
    r = fetcher.get(SITEMAP)
    if r is None:
        logger.warning("Could not fetch Lidl sitemap (WAF block?).")
        return []
    sms, pages = _parse_xml(r.content)

    product_urls = [u for u in pages if PRODUCT_HINT.search(u)]
    queue = list(sms)
    seen = set()
    while queue and len(product_urls) < max_urls:
        s = queue.pop(0)
        if s in seen:
            continue
        seen.add(s)
        if not fetcher.can_fetch(s):
            continue
        rr = fetcher.get(s)
        if rr is None:
            continue
        cs, pp = _parse_xml(rr.content)
        for c in cs:
            if c not in seen:
                queue.append(c)
        for u in pp:
            if PRODUCT_HINT.search(u):
                product_urls.append(u)

    # de-dup, normalize domain to the one our fetcher/robots is set up for
    norm = []
    seen_u = set()
    for u in product_urls:
        u = u.replace("https://www.lidl.gr/", "https://www.lidl-hellas.gr/")
        if u not in seen_u:
            seen_u.add(u)
            norm.append(u)
    logger.info("Discovered %d Lidl product URLs", len(norm))
    return norm[:max_urls]


def run(store, max_products=100):
    """
    Discover + scrape Lidl products into the store. SERIAL and polite.
    Stops if the WAF appears to be blocking (consecutive failures).
    """
    fetcher = PoliteFetcher(BASE_URL)

    urls = discover_product_urls(fetcher, max_urls=max(max_products * 2, 1000))
    if not urls:
        logger.warning("No Lidl product URLs discovered.")
        return 0

    count = 0
    no_price = 0
    block_streak = 0
    for url in urls:
        if count >= max_products:
            break
        r = fetcher.get(url)
        if r is None:
            block_streak += 1
            if block_streak >= BLOCK_STREAK_LIMIT:
                logger.warning("Stopping: %d consecutive blocked/failed fetches "
                               "— Lidl appears to be refusing automated access. "
                               "Not attempting to evade.", block_streak)
                break
            continue
        block_streak = 0

        data = extract_lidl_product(r.text, url)
        if not data:
            no_price += 1
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
            currency="EUR",
        )
        count += 1
        if count % 50 == 0:
            logger.info("Lidl: recorded %d products so far", count)

    logger.info("Lidl: recorded %d products (%d skipped: no online price)",
                count, no_price)
    return count
