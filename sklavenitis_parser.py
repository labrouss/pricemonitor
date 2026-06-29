"""
Sklavenitis-specific product extraction.

Sklavenitis is a Vue SPA that does NOT expose JSON-LD. Instead the product
data is embedded in the page HTML in two places:

  1. The GA4 dataLayer ecommerce block — an "items" array containing
     item_id (== sku), item_name, item_brand, item_category, and price.
     This is our source of truth for name / brand / price.

  2. A `data-plugin-product="{...}"` attribute (HTML-escaped JSON) carrying
     stock info: stock.available, status, notBuyable, maximum.

We merge the two on sku/item_id.
"""

import re
import json
import html as htmllib
import logging

logger = logging.getLogger(__name__)


def _extract_datalayer_items(page):
    """
    Return a list of GA4 item dicts found in the page. We locate the first
    "items": [ ... ] array and parse it. Handles the trailing-comma-free,
    well-formed JSON that GA4 emits.
    """
    items = []
    # Find "items": [ ... ]  — non-greedy up to the matching close bracket.
    for m in re.finditer(r'"items"\s*:\s*(\[.*?\])', page, re.DOTALL):
        block = m.group(1)
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            for it in parsed:
                if isinstance(it, dict) and ("item_id" in it or "item_name" in it):
                    items.append(it)
        if items:
            break
    return items


def _extract_plugin_products(page):
    """
    Return a dict keyed by sku -> stock/availability info from every
    data-plugin-product attribute on the page.
    """
    out = {}
    for m in re.finditer(r'data-plugin-product="([^"]+)"', page):
        raw = htmllib.unescape(m.group(1))
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        sku = obj.get("sku")
        if sku:
            out[str(sku)] = obj
    return out


def _stock_to_bool(plugin_obj):
    """Derive an in_stock flag from a data-plugin-product object."""
    if not plugin_obj:
        return None
    if plugin_obj.get("notBuyable") is True:
        return 0
    stock = plugin_obj.get("stock") or {}
    avail = stock.get("available")
    if avail is None:
        # Fall back to status: 0 commonly means available on this site.
        status = plugin_obj.get("status")
        if status is None:
            return None
        return 1 if status == 0 else 0
    try:
        return 1 if float(avail) > 0 else 0
    except (TypeError, ValueError):
        return None


def extract_sklavenitis_product(page):
    """
    Parse one Sklavenitis product page. Returns a normalized dict or None.
    Keys: name, brand, sku, price, list_price, currency, in_stock, category.
    """
    items = _extract_datalayer_items(page)
    if not items:
        logger.debug("No dataLayer items found")
        return None

    plugins = _extract_plugin_products(page)

    # On a product detail page the primary product is the first item.
    it = items[0]
    sku = str(it.get("item_id")) if it.get("item_id") is not None else None

    price = it.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    name = (it.get("item_name") or "").strip() or None
    brand = it.get("item_brand")
    category = it.get("item_category")

    plugin_obj = plugins.get(sku) if sku else None
    in_stock = _stock_to_bool(plugin_obj)

    return {
        "name": name,
        "brand": brand,
        "sku": sku,
        "price": price,
        "list_price": None,   # not exposed in these two blocks; see note below
        "currency": "EUR",
        "in_stock": in_stock,
        "category": category,
    }
