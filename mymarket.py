"""
mymarket.gr retailer module (server-rendered, Hotwired/Stimulus).

Discovery: sitemap has a dedicated /sitemap/categories.xml — the clean source.
Harvest: each category page shows ~34 products with a JSON-LD ItemList (clean
identity) + HTML cards (displayed price). We page via ?page=N until empty.

Robots: category pages and ?page=N are allowed. We avoid the disallowed filter
params (?sort=, ?perPage=, ?categories=, ?brand=, ?price=, /search). The site
sits behind Imperva Incapsula (a WAF) — if requests start getting challenged,
SLOW DOWN (raise the crawl delay); do not try to evade the protection.

No per-product EAN is exposed; sku is Mymarket's internal code.
"""

import gzip
import re
import logging
from xml.etree import ElementTree as ET

from fetcher import PoliteFetcher
from mymarket_parser import harvest_listing

logger = logging.getLogger(__name__)

RETAILER = "mymarket"
BASE_URL = "https://www.mymarket.gr"
CATEGORIES_SITEMAP = "https://www.mymarket.gr/sitemap/categories.xml"


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


# robots-disallowed query params we must never append
BAD_PARAM = re.compile(r"[?&](sort|perPage|categories|brand|price|in_offer|"
                       r"diaper_size|diaper_type|specs_|free_from|query)=",
                       re.IGNORECASE)


def discover_categories(fetcher):
    """Return category URLs from /sitemap/categories.xml."""
    r = fetcher.get(CATEGORIES_SITEMAP)
    if r is None:
        return []
    _, pages = _parse_xml(r.content)
    # keep only clean category URLs (no disallowed params, no /search)
    cats = [u for u in pages if "/search" not in u and not BAD_PARAM.search(u)]
    logger.info("Discovered %d Mymarket categories", len(cats))
    return cats


def harvest_category(fetcher, category_url, max_pages=60):
    """Page through a category via ?page=N until no new products."""
    all_products = []
    seen = set()
    for pg in range(1, max_pages + 1):
        if pg == 1:
            url = category_url
        else:
            sep = "&" if "?" in category_url else "?"
            url = f"{category_url}{sep}page={pg}"
        r = fetcher.get(url)
        if r is None:
            break
        products = harvest_listing(r.text)
        new = [p for p in products if p["sku"] not in seen]
        if not new:
            break
        for p in new:
            seen.add(p["sku"])
            if p.get("url") and p["url"].startswith("/"):
                p["url"] = BASE_URL + p["url"]
            all_products.append(p)
    return all_products


def run(store, max_products=200, max_categories=None):
    """Discover categories and harvest products into the shared store."""
    fetcher = PoliteFetcher(BASE_URL)

    categories = discover_categories(fetcher)
    if not categories:
        logger.warning("No Mymarket categories discovered.")
        return 0
    if max_categories:
        categories = categories[:max_categories]

    seen_skus = set()
    count = 0
    for cat in categories:
        if count >= max_products:
            break
        products = harvest_category(fetcher, cat)
        logger.info("Category %s -> %d products", cat, len(products))
        for data in products:
            if count >= max_products:
                break
            sku = data.get("sku")
            if sku and sku in seen_skus:
                continue
            if sku:
                seen_skus.add(sku)
            pid = store.upsert_product(
                retailer=RETAILER,
                name=data.get("name") or "(unknown)",
                url=data.get("url") or cat,
                sku=sku,
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

    logger.info("Mymarket: recorded %d product observations", count)
    return count
