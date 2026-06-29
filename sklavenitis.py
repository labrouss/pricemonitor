"""
Sklavenitis-specific scraper module.

This module knows two things that are specific to one retailer:
  - its base URL
  - how to discover product URLs (via sitemap)
  - an optional HTML fallback if JSON-LD is missing

Everything else (politeness, storage, JSON-LD parsing) is shared.

IMPORTANT — verify before running at any volume:
  * The fetcher already refuses paths disallowed by robots.txt and honors
    crawl-delay. Do not remove that.
  * Check Sklavenitis's Terms of Service yourself. robots.txt permitting a
    path is a technical signal, not blanket legal permission. This code is
    written to be polite and low-volume; keep it that way.
  * Start with a tiny limit (e.g. 5 products) to confirm the structure
    before any larger run.
"""

import re
import gzip
import logging
from xml.etree import ElementTree as ET

from fetcher import PoliteFetcher
from parser import extract_products_jsonld
from sklavenitis_parser import extract_sklavenitis_product
from sklavenitis_category import harvest_category

logger = logging.getLogger(__name__)

RETAILER = "sklavenitis"
BASE_URL = "https://www.sklavenitis.gr"

# Heuristic: product URLs on most Greek e-shops contain a recognizable segment.
# We do NOT hard-code an exact pattern blindly — instead we let discovery find
# sitemap entries and filter loosely, then rely on JSON-LD presence to confirm
# a page is actually a product. Adjust this regex after inspecting real URLs.
PRODUCT_URL_HINT = re.compile(r"/products?/", re.IGNORECASE)


def _parse_sitemap_xml(content_bytes):
    """Return (sitemap_urls, page_urls) from a sitemap or sitemap-index."""
    # Handle gzipped sitemaps (magic bytes 1f 8b). requests already decodes
    # Content-Encoding: gzip transport compression, but .xml.gz files are
    # gzipped at the content level and arrive still compressed.
    if content_bytes[:2] == b"\x1f\x8b":
        try:
            content_bytes = gzip.decompress(content_bytes)
        except (OSError, EOFError) as e:
            # Truncated gzip (e.g. from a partial download). Try to salvage
            # what we can rather than crashing the whole run.
            logger.warning("Gzip decompress failed (%s); attempting partial read", e)
            try:
                import io
                with gzip.GzipFile(fileobj=io.BytesIO(content_bytes)) as gz:
                    content_bytes = gz.read()
            except (OSError, EOFError):
                logger.warning("Could not salvage gzipped sitemap; skipping")
                return [], []
    try:
        root = ET.fromstring(content_bytes)
    except ET.ParseError as e:
        logger.warning("Sitemap parse error: %s", e)
        return [], []

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemaps, pages = [], []

    # sitemap index?
    for sm in root.findall(".//sm:sitemap/sm:loc", ns):
        if sm.text:
            sitemaps.append(sm.text.strip())
    # url set?
    for loc in root.findall(".//sm:url/sm:loc", ns):
        if loc.text:
            pages.append(loc.text.strip())
    # Fallback for sitemaps without the namespace
    if not sitemaps and not pages:
        for loc in root.iter():
            if loc.tag.endswith("loc") and loc.text:
                pages.append(loc.text.strip())
    return sitemaps, pages


def discover_product_urls(fetcher, max_urls=50):
    """
    Discover candidate product URLs via sitemaps advertised in robots.txt.
    Returns at most max_urls URLs that look like product pages.

    Filtering strategy: if a sitemap's own URL marks it as a product sitemap
    (e.g. .../sitemap/Products/1.xml), we TRUST it and take every URL inside,
    regardless of slug format. Only when we can't tell from the sitemap name
    do we fall back to the URL-pattern heuristic.
    """
    found = []
    seen_sitemaps = set()

    queue = list(fetcher.get_sitemaps())
    if not queue:
        # Common default location
        queue = [f"{BASE_URL}/sitemap.xml"]

    while queue and len(found) < max_urls:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        resp = fetcher.get(sm_url)
        if resp is None:
            continue
        child_sitemaps, pages = _parse_sitemap_xml(resp.content)

        # If the sitemap itself is a "Products" sitemap, every URL in it is a
        # product — trust the source rather than guessing from the slug.
        sitemap_is_products = "product" in sm_url.lower()

        # Prioritize sitemaps that look product-related
        for cs in child_sitemaps:
            if cs not in seen_sitemaps:
                queue.append(cs)

        for url in pages:
            # Trust a product-named sitemap; otherwise fall back to the hint.
            if sitemap_is_products or PRODUCT_URL_HINT.search(url):
                found.append(url)
                if len(found) >= max_urls:
                    break

    logger.info("Discovered %d candidate product URLs", len(found))
    return found[:max_urls]


def scrape_product(fetcher, url):
    """Fetch one product page and return a normalized dict, or None."""
    resp = fetcher.get(url)
    if resp is None:
        return None

    # Sklavenitis is a Vue SPA without JSON-LD; use the site-specific
    # extractor (GA4 dataLayer items + data-plugin-product stock).
    data = extract_sklavenitis_product(resp.text)
    if data and data.get("price") is not None:
        data["url"] = url
        return data

    # Fallback: try generic JSON-LD in case some pages expose it.
    products = extract_products_jsonld(resp.text)
    for p in products:
        if p.get("price") is not None:
            p["url"] = url
            return p

    logger.debug("No price found on %s", url)
    return None


def harvest_category_page(fetcher, category_url, max_pages=40):
    """
    Fetch ALL pages of a category and return a combined list of product dicts.

    Sklavenitis (a Netvolution e-shop) paginates with ?pg=N, 24 products per
    page. We walk pages until one returns no NEW skus (or repeats the previous
    page), which marks the end. max_pages is a safety cap.
    """
    all_products = []
    seen_skus = set()

    for pg in range(1, max_pages + 1):
        if pg == 1:
            url = category_url
        else:
            sep = "&" if "?" in category_url else "?"
            url = f"{category_url}{sep}pg={pg}"

        resp = fetcher.get(url)
        if resp is None:
            break

        products = harvest_category(resp.text)
        # Which skus on this page are new?
        page_skus = {p["sku"] for p in products if p.get("sku")}
        new_skus = page_skus - seen_skus

        if not new_skus:
            # No new products -> we've gone past the last page.
            break

        for p in products:
            sku = p.get("sku")
            if sku and sku not in seen_skus:
                seen_skus.add(sku)
                if p.get("url") and p["url"].startswith("/"):
                    p["url"] = BASE_URL + p["url"]
                all_products.append(p)

        # If this page had fewer than a full page of new items, it's the last.
        if len(new_skus) < 24:
            break

    return all_products


def run(store, max_products=50, max_categories=None):
    """
    Harvest category pages and record every product found.

    Category pages carry ~24 products each WITH prices, discounts, unit prices
    and stock — so we harvest those rather than fetching product pages one by
    one. `max_products` caps the total recorded; `max_categories` optionally
    caps how many category pages we visit.
    """
    fetcher = PoliteFetcher(BASE_URL)

    categories = discover_product_urls(fetcher, max_urls=200)
    if not categories:
        logger.warning("No category URLs discovered. Inspect sitemap structure.")
        return 0
    if max_categories:
        categories = categories[:max_categories]

    seen_skus = set()
    count = 0
    for cat_url in categories:
        if count >= max_products:
            break
        products = harvest_category_page(fetcher, cat_url)
        logger.info("Category %s -> %d products", cat_url, len(products))

        for data in products:
            if count >= max_products:
                break
            if data.get("price") is None:
                continue
            sku = data.get("sku")
            if sku and sku in seen_skus:
                continue       # avoid duplicates across overlapping categories
            if sku:
                seen_skus.add(sku)

            product_url = data.get("url") or cat_url
            pid = store.upsert_product(
                retailer=RETAILER,
                name=data.get("name") or "(unknown)",
                url=product_url,
                sku=sku,
                brand=data.get("brand"),
                category=data.get("category"),
            )
            store.record_price(
                product_id=pid,
                price=data["price"],
                list_price=data.get("list_price"),
                in_stock=data.get("in_stock"),
                unit_price=data.get("unit_price"),
                unit=data.get("unit"),
                currency="EUR",
            )
            count += 1

    logger.info("Recorded %d product observations across %d categories",
                count, len(categories))
    return count
