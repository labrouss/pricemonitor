"""
bazaar-online.gr retailer module (OpenCart).

Discovery: the site has no declared sitemap, but the homepage navigation
exposes the full category taxonomy (~790 links). We collect the LEAF category
URLs (those with the deepest path) and harvest each.

Harvest: category listing pages show ~16 product cards and paginate via
?page=N (OpenCart native). We page through each category until no new product
ids appear. Each card is parsed by bazaar_parser (per-card, weighed/piece/sale).

Robots: product/category listing pages are allowed (checked at fetch time).
We use only the pretty category URLs + ?page=N.
"""

import re
import logging

from bs4 import BeautifulSoup

from fetcher import PoliteFetcher
from bazaar_parser import harvest_listing

logger = logging.getLogger(__name__)

RETAILER = "bazaar"
BASE_URL = "https://www.bazaar-online.gr"

# A category URL segment ends in '-<digits>' (e.g. /loykaniko-271). Leaf
# categories are the deepest such paths. We recognize category links by this.
CAT_SEG = re.compile(r"/[a-z0-9\-]+-\d+(?:/[a-z0-9\-]+-\d+)*/?$", re.IGNORECASE)


def discover_categories(fetcher):
    """
    Collect leaf category URLs from the homepage navigation.

    We gather every link that looks like a category path, then keep the
    deepest ones (a parent like /allantika-6 is covered by its children like
    /allantika-6/loykaniko-271, so harvesting children avoids double work).
    """
    r = fetcher.get(BASE_URL)
    if r is None:
        return []
    soup = BeautifulSoup(r.text, "html.parser")

    cats = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = BASE_URL + href
        if not href.startswith(BASE_URL):
            continue
        path = href[len(BASE_URL):]
        # category paths have at least one '-<id>' segment and no query/#
        if "?" in path or "#" in path:
            continue
        if CAT_SEG.search(path):
            cats.add(href.rstrip("/"))

    # Keep leaves: drop any URL that is a strict prefix of another.
    cats = sorted(cats)
    leaves = []
    for c in cats:
        if not any(other != c and other.startswith(c + "/") for other in cats):
            leaves.append(c)
    logger.info("Discovered %d leaf categories (of %d total category links)",
                len(leaves), len(cats))
    return leaves


def harvest_category(fetcher, category_url, max_pages=60):
    """Page through one category until no new product ids; return product dicts."""
    all_products = []
    seen = set()

    for pg in range(1, max_pages + 1):
        url = category_url if pg == 1 else f"{category_url}?page={pg}"
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
        logger.warning("No Bazaar categories discovered.")
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
        # Derive a human category name from the URL slug: the last path segment
        # minus its trailing "-<digits>" id (e.g. /trofima/loykaniko-271 -> "loykaniko").
        cat_name = None
        try:
            slug = cat.rstrip("/").split("/")[-1]
            slug = re.sub(r"-\d+$", "", slug)
            cat_name = slug.replace("-", " ").strip() or None
        except Exception:
            cat_name = None
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
                category=cat_name,
                image_url=data.get("image_url"),
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

    logger.info("Bazaar: recorded %d product observations", count)
    return count
