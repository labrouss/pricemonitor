"""
bazaar-online.gr parser (OpenCart, server-rendered HTML).

Category/listing pages show product cards: <div class="product-thumb ...">
Each card carries:
  - data-product-id      -> product id (our sku)
  - data-zigizomeno      -> 1 = weighed (price is per-kilo), 0 = per-piece
  - h4                   -> product name
  - .price_wrapper       -> price (Greek decimal, e.g. "7,90€")
  - .item_price_text     -> label ("/Κιλό" or "Τελική τιμή")
  - .priceperkg          -> unit price reference ("18,00€/Κιλό")
  - .packaging           -> e.g. "1 Τεμάχια"
  - OpenCart sale markup (.price-old / .price-new) when on offer

We harvest per CARD (pairing each id with its own price), the same robust
approach used for Sklavenitis. We parse listing pages only.
"""

import re
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _gr_price(s):
    """Parse '7,90€' / '18,00€/Κιλό' -> 7.90 / 18.00 (first number found)."""
    if not s:
        return None
    s = s.replace("\xa0", " ")
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{1,2}|\d+,\d{1,2}|\d+)", s)
    if not m:
        return None
    num = m.group(1)
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _text(el):
    return el.get_text(" ", strip=True) if el else None


def parse_card(card):
    """Parse one product-thumb card -> normalized dict or None."""
    pid = card.get("data-product-id")
    if not pid:
        return None

    name = _text(card.find("h4")) or _text(card.find(class_="name"))
    weighed = card.get("data-zigizomeno") == "1"

    # product image (OpenCart thumb): prefer data-src (lazy) then src
    image_url = None
    img = card.find("img")
    if img:
        image_url = img.get("data-src") or img.get("src")
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

    # Price: prefer OpenCart sale markup if present, else .price_wrapper
    price = list_price = None
    new_el = card.find(class_="price-new")
    old_el = card.find(class_="price-old")
    if new_el:
        price = _gr_price(_text(new_el))
        list_price = _gr_price(_text(old_el)) if old_el else None
    else:
        price = _gr_price(_text(card.find(class_="price_wrapper")))
        if old_el:  # some themes show old price separately
            list_price = _gr_price(_text(old_el))

    label = _text(card.find(class_="item_price_text"))
    unit_price = None
    unit = None
    ppk = _text(card.find(class_="priceperkg"))
    if ppk:
        unit_price = _gr_price(ppk)
        # unit is the word after the slash, skipping any quantity number
        um = re.search(r"/\s*\d*\s*([^\d\s/]+)", ppk)
        if um:
            unit = um.group(1)
    packaging = _text(card.find(class_="packaging"))

    # product link (for storing a per-product URL if present)
    url = None
    for a in card.find_all("a", href=True):
        href = a["href"]
        if href and href != "#" and "/wishlist" not in href:
            url = href
            break

    return {
        "name": name,
        "brand": None,            # OpenCart cards rarely separate brand
        "sku": str(pid),
        "ean": None,
        "price": price,
        "list_price": list_price,
        "unit_price": unit_price,
        "unit": unit,
        "packaging": packaging,
        "weighed": weighed,
        "label": label,
        "in_stock": None,         # listing cards don't reliably show stock
        "url": url,
        "image_url": image_url,
    }


def harvest_listing(html):
    """Parse a Bazaar category/listing page -> list of product dicts."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen = set()
    for card in soup.find_all(class_="product-thumb"):
        data = parse_card(card)
        if not data or data.get("price") is None:
            continue
        sku = data["sku"]
        if sku in seen:
            continue
        seen.add(sku)
        out.append(data)
    return out
