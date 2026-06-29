"""
Product extraction.

Strategy, in order of preference:
  1. JSON-LD structured data (<script type="application/ld+json">).
     Most modern e-commerce sites embed schema.org/Product with offers,
     price, availability. This is BY FAR the most stable thing to parse —
     it's a contract the site exposes for Google, and it changes far less
     often than CSS class names.
  2. HTML fallback using CSS selectors — brittle, site-specific, kept as a
     last resort and isolated per-retailer.

We deliberately avoid hard-coding fragile selectors in the shared layer.
"""

import json
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _availability_to_bool(av):
    if not av:
        return None
    av = str(av).lower()
    if "instock" in av or "in_stock" in av:
        return 1
    if "outofstock" in av or "out_of_stock" in av or "soldout" in av:
        return 0
    return None


def _walk_json_ld(node):
    """Yield every dict in a possibly-nested JSON-LD structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_json_ld(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json_ld(item)


def extract_products_jsonld(html):
    """
    Return a list of normalized product dicts found via JSON-LD.
    Each dict: name, brand, sku, price, list_price, currency, in_stock.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites concatenate multiple JSON objects or have trailing junk.
            continue

        for node in _walk_json_ld(data):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if not any(str(x).lower() == "product" for x in types if x):
                continue

            name = node.get("name")
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            sku = node.get("sku") or node.get("gtin13") or node.get("gtin")

            offers = node.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers] if offers else []

            if not offer_list:
                results.append({
                    "name": name, "brand": brand, "sku": sku,
                    "price": None, "list_price": None,
                    "currency": "EUR", "in_stock": None,
                })
                continue

            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price") or offer.get("lowPrice")
                currency = offer.get("priceCurrency", "EUR")
                in_stock = _availability_to_bool(offer.get("availability"))
                # schema.org sometimes carries a strikethrough "highPrice"
                list_price = offer.get("highPrice")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    list_price = float(list_price) if list_price is not None else None
                except (TypeError, ValueError):
                    list_price = None

                results.append({
                    "name": name, "brand": brand, "sku": sku,
                    "price": price, "list_price": list_price,
                    "currency": currency, "in_stock": in_stock,
                })
    return results
