"""
ab.gr product-page parser.

AB is a Next.js + SAP Commerce (Hybris) site. Product pages expose schema.org
JSON-LD with a Product object, but the price is nested under
    offers.priceSpecification.price
(not offers.price), so the generic extractor misses it. This module handles
AB's exact structure.

Robots note: this parses PRODUCT PAGES only (allowed by ab.gr/robots.txt).
Discovery uses the sitemap (allowed). We never touch /search (disallowed).
"""

import re
import json
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _availability(av):
    if not av:
        return None
    av = str(av).lower()
    if "instock" in av:
        return 1
    if "outofstock" in av or "soldout" in av:
        return 0
    return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        # Greek-formatted fallback
        s = str(v).replace(".", "").replace(",", ".")
        s = re.sub(r"[^0-9.]", "", s)
        try:
            return float(s)
        except ValueError:
            return None


def _price_from_offer(offer):
    """Extract (price, list_price) from an AB Offer object."""
    if not isinstance(offer, dict):
        return None, None
    price = offer.get("price")
    if price is None:
        spec = offer.get("priceSpecification")
        if isinstance(spec, dict):
            price = spec.get("price")
        elif isinstance(spec, list):
            # multiple specs: take the UnitPriceSpecification / first with price
            for s in spec:
                if isinstance(s, dict) and s.get("price") is not None:
                    price = s.get("price")
                    break
    # Some sites carry a strikethrough as highPrice or a separate spec
    list_price = offer.get("highPrice")
    return _to_float(price), _to_float(list_price)


def extract_ab_product(html):
    """
    Parse an AB product page. Returns a normalized dict or None.
    Keys: name, brand, sku, ean, price, list_price, currency, in_stock.
    """
    soup = BeautifulSoup(html, "html.parser")

    product_node = None
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for node in candidates:
            if isinstance(node, dict) and str(node.get("@type", "")).lower() == "product":
                product_node = node
                break
        if product_node:
            break

    if not product_node:
        logger.debug("No Product JSON-LD found")
        return None

    name = product_node.get("name")
    brand = product_node.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    sku = product_node.get("sku") or product_node.get("mpn")
    ean = (product_node.get("gtin13") or product_node.get("gtin")
           or product_node.get("gtin14") or product_node.get("gtin12"))

    # image: JSON-LD `image` can be a string, list, or ImageObject
    image = product_node.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    # category: JSON-LD `category` (string) or breadcrumb fallback
    category = product_node.get("category")
    if isinstance(category, dict):
        category = category.get("name")

    offers = product_node.get("offers")
    offer = offers[0] if isinstance(offers, list) and offers else offers
    price, list_price = _price_from_offer(offer)
    in_stock = _availability(offer.get("availability") if isinstance(offer, dict) else None)
    currency = "EUR"
    if isinstance(offer, dict):
        currency = offer.get("priceCurrency") or "EUR"
        spec = offer.get("priceSpecification")
        if isinstance(spec, dict):
            currency = spec.get("priceCurrency") or currency

    return {
        "name": name.strip() if name else None,
        "brand": brand,
        "sku": str(sku) if sku else None,
        "ean": str(ean) if ean else None,
        "price": price,
        "list_price": list_price,
        "currency": currency,
        "in_stock": in_stock,
        "image_url": image if isinstance(image, str) else None,
        "category": category if isinstance(category, str) else None,
    }
