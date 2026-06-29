"""
posokanei.gov.gr ingester — the official government price-comparison API.

This is the CLEANEST data source in the project:
  - robots.txt allows all bots (Crawl-delay: 1).
  - Public REST API at https://api.posokanei.gov.gr, NO auth/key/token.
  - Products are already MATCHED across all chains (the cross-chain matching
    that the individual scrapers could not do). /products/{id} returns one
    product's price at every retailer that carries it.

It covers ~8,400 products across 10 Greek chains (Sklavenitis, AB, Masoutis,
My Market, Lidl, Galaxias, SYNKA, Kritikos, Market In, Halkiadakis) plus
European comparison retailers — including the two we could not scrape directly
(Masoutis: auth-gated; Lidl: WAF). So posokanei effectively REPLACES the
individual scrapers with one clean, official, pre-matched source.

We record one price row per (product, retailer) so the existing storage/GUI
work unchanged. The posokanei product id is stored as a shared key, enabling
the cross-chain analysis that is the project's actual goal.

Politeness: we honor the 1s crawl delay, identify ourselves honestly, and pull
at a measured pace. For sustained/automated use, consider contacting the
operators (Υπ. Ανάπτυξης / Independent Market Authority) for an official feed.
"""

import time
import logging

import requests

logger = logging.getLogger(__name__)

RETAILER_TAG = "posokanei"      # not used as retailer; real retailer per price
API = "https://api.posokanei.gov.gr"
CRAWL_DELAY = 1.0               # robots.txt Crawl-delay: 1

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://posokanei.gov.gr",
    "referer": "https://posokanei.gov.gr/",
    "x-app-version": "1.0.0",
    "x-platform": "flutter-web",
    # Identify honestly. Put a real contact so the operator can reach you.
    "user-agent": "PriceMonitorResearch/0.1 (+contact: you@example.org)",
}


class PosokaneiClient:
    def __init__(self, delay=CRAWL_DELAY):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self._last = 0.0

    def get(self, path, params=None, retries=3):
        backoff = 2.0
        for attempt in range(1, retries + 1):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.get(API + path, params=params, timeout=30)
                self._last = time.time()
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("Retry-After")
                    time.sleep(float(ra) if ra and ra.isdigit() else backoff)
                    backoff *= 2
                    continue
                logger.warning("HTTP %s on %s", r.status_code, path)
                return None
            except requests.RequestException as e:
                logger.warning("request error on %s: %s", path, e)
                time.sleep(backoff)
                backoff *= 2
        return None


def iter_leaf_categories(client):
    """Yield (category_id, name) for leaf categories (those without children)."""
    tree = client.get("/meta/categories/tree",
                      {"include_counts": "true", "include_hidden": "false"})
    if not tree:
        return
    root = tree.get("tree", tree)

    stack = list(root if isinstance(root, list) else [root])
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        children = node.get("children") or []
        if children:
            stack.extend(children)
        elif node.get("category_id"):
            yield node["category_id"], node.get("name")


def iter_products_in_category(client, category_id, countries="GR", page_size=50):
    """Yield product summary dicts for a category, paging through all results."""
    page = 1
    while True:
        data = client.get("/products", {
            "page": page, "page_size": page_size,
            "category": category_id, "countries": countries,
        })
        if not data:
            break
        products = data.get("products", [])
        for p in products:
            yield p
        if not data.get("has_next"):
            break
        page += 1


def fetch_product_detail(client, product_id):
    """Full product with retailer_prices across all chains."""
    return client.get(f"/products/{product_id}", {
        "sort_retailers": "asc", "countries": "all", "include_tax": "true",
    })


def run(store, max_products=500, countries="GR", greek_only=True):
    """
    Ingest posokanei products + cross-chain prices into the store.

    Each retailer price becomes a row under that retailer's name, with the
    posokanei product id stored as sku (the shared cross-chain key). Set
    greek_only=False to also record European comparison retailers.
    """
    GREEK = {"sklavenitis", "ab_vasilopoulos", "masoutis", "mymarket", "lidl",
             "galaxias", "synka", "kritikos", "market_in", "halkiadakis"}

    client = PosokaneiClient()

    seen_products = set()
    count = 0
    for cat_id, cat_name in iter_leaf_categories(client):
        if count >= max_products:
            break
        for summary in iter_products_in_category(client, cat_id, countries):
            if count >= max_products:
                break
            pid = summary.get("id")
            if not pid or pid in seen_products:
                continue
            seen_products.add(pid)

            detail = fetch_product_detail(client, pid)
            if not detail:
                continue
            prices = detail.get("retailer_prices", [])
            if not prices:
                continue

            pname = detail.get("name", "").strip()
            brand = detail.get("brand")
            # posokanei (like MyMarket) bakes the brand into the start of the
            # product name while also exposing it separately. Strip the leading
            # brand so the stored name is clean and dedup works correctly.
            if pname and brand:
                try:
                    from dedup import strip_brand_prefix
                    pname = strip_brand_prefix(pname, brand)
                except Exception:
                    pass
            category = detail.get("category") or detail.get("subcategory")
            image_url = detail.get("image_url") if detail.get("has_image") else None

            recorded_any = False
            for rp in prices:
                retailer = rp.get("retailer")
                if not retailer:
                    continue
                if greek_only and retailer not in GREEK:
                    continue
                price = rp.get("price")
                if price is None:
                    continue
                # is_discount + discount_percentage -> derive a list_price
                list_price = None
                if rp.get("is_discount") and rp.get("discount_percentage"):
                    try:
                        dp = float(rp["discount_percentage"])
                        if 0 < dp < 100:
                            list_price = round(price / (1 - dp / 100.0), 2)
                    except (TypeError, ValueError):
                        pass

                # Store: retailer = the actual chain; sku = posokanei product id
                # (shared cross-chain key); unit_price = normalized price.
                store_pid = store.upsert_product(
                    retailer=retailer,
                    name=pname or "(unknown)",
                    url=f"https://posokanei.gov.gr/product/{pid}",
                    sku=None,                 # posokanei doesn't expose the chain's own sku
                    shared_key=pid,           # the cross-chain product key
                    brand=brand,
                    category=category,
                    image_url=image_url,
                )
                store.record_price(
                    product_id=store_pid,
                    price=price,
                    list_price=list_price,
                    in_stock=None,
                    unit_price=rp.get("price_normalized"),
                    unit=detail.get("unit"),
                    currency="EUR",
                )
                recorded_any = True

            if recorded_any:
                count += 1
                if count % 100 == 0:
                    logger.info("posokanei: %d products ingested", count)

    logger.info("posokanei: ingested %d products (cross-chain prices)", count)
    return count
