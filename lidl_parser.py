"""
lidl-hellas.gr product parser (Nuxt SSR).

Product pages embed:
  - JSON-LD Product: name, sku, offers (availability, currency). The JSON-LD
    'price' is null when out of stock and not always populated, so we do NOT
    rely on it for the number.
  - meta description: "<name> - <price>€ (<date>)" — the reliable price source,
    present even when out of stock. We parse the price (and the date if shown).
  - availability from JSON-LD offers (InStock / OutOfStock).

The pdp-view Nuxt JSON blob uses an index-reference array format that is
fragile to parse, so we avoid it.

No gtin/barcode is exposed; sku is Lidl's internal article id.
"""

import re
import json
import logging

logger = logging.getLogger(__name__)

PRICE_IN_META = re.compile(r'-\s*([\d.,]+)\s*€')
DATE_IN_META = re.compile(r'\(([\d./-]+)\)')


def _to_float(s):
    if s is None:
        return None
    s = str(s).strip()
    if "," in s and "." in s:
        # ambiguous; assume , is decimal if it's last
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _availability(av):
    if not av:
        return None
    av = str(av).lower()
    if "instock" in av:
        return 1
    if "outofstock" in av or "soldout" in av:
        return 0
    return None


def _jsonld_product(html):
    for b in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        html, re.DOTALL):
        try:
            data = json.loads(b)
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else (
            data if isinstance(data, list) else [data])
        for n in nodes:
            if isinstance(n, dict) and str(n.get("@type")).lower() == "product":
                return n
    return None


def extract_lidl_product(html, url=None):
    """Parse a Lidl product page -> normalized dict or None."""
    prod = _jsonld_product(html)
    name = sku = None
    in_stock = None
    jsonld_price = None
    image = None
    brand = None
    category = None
    if prod:
        name = prod.get("name")
        sku = prod.get("sku")
        b = prod.get("brand")
        if isinstance(b, dict):
            b = b.get("name")
        brand = b if isinstance(b, str) else None
        img = prod.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        image = img if isinstance(img, str) else None
        c = prod.get("category")
        if isinstance(c, dict):
            c = c.get("name")
        category = c if isinstance(c, str) else None
        offers = prod.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offer, dict):
            in_stock = _availability(offer.get("availability"))
            jsonld_price = _to_float(offer.get("price"))

    # Price from meta description (reliable, even when out of stock)
    price = None
    price_date = None
    m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html)
    if m:
        desc = m.group(1)
        pm = PRICE_IN_META.search(desc)
        if pm:
            price = _to_float(pm.group(1))
        dm = DATE_IN_META.search(desc)
        if dm:
            price_date = dm.group(1)
        if not name:
            # name is the part before " - <price>€"
            nm = re.match(r'^(.*?)\s*-\s*[\d.,]+\s*€', desc)
            if nm:
                name = nm.group(1).strip()

    if price is None:
        price = jsonld_price  # fall back to JSON-LD if meta lacked it

    if price is None:
        return None

    return {
        "name": name.strip() if name else None,
        "brand": brand,
        "sku": str(sku) if sku else None,
        "ean": None,
        "price": price,
        "list_price": None,
        "in_stock": in_stock,
        "price_date": price_date,
        "url": url,
        "image_url": image,
        "category": category,
    }
