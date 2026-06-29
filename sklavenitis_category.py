"""
Sklavenitis CATEGORY-page harvester.

A category/listing page (e.g. /anapsyktika-nera-chymoi/) contains many product
cards. Each card carries:
  - data-plugin-product="{...}"  -> sku, stock, status, notBuyable
  - a price block in HTML:
        <div class="priceKil">1,36 €<span>/λίτρο</span></div>   (unit price)
        <div class="main-price ...">
            <div class="price" data-price="1,80">1,80 €<span>/τεμ.</span></div>
        </div>
    When a product is on offer, a "main-price--previous" block holds the OLD
    price and a separate current-price block holds the discounted price.

Harvesting category pages gives us ~24 fully-priced products per request —
far more efficient and polite than fetching each product page separately.

Because the price markup is HTML (not a single JSON blob), we parse each
product CARD as a unit using BeautifulSoup, pairing the plugin JSON with the
price elements inside the same card container.
"""

import re
import json
import html as htmllib
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _gr_decimal(s):
    """Convert a Greek-formatted number string ('1,80' or '1.234,56') to float."""
    if s is None:
        return None
    s = s.strip().replace("\xa0", "").replace("€", "").strip()
    if not s:
        return None
    # Remove thousands separator '.', convert decimal ',' to '.'
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        # Last resort: strip non-numeric except dot
        s2 = re.sub(r"[^0-9.]", "", s)
        try:
            return float(s2)
        except ValueError:
            return None


def _stock_to_bool(obj):
    if not obj:
        return None
    if obj.get("notBuyable") is True:
        return 0
    stock = obj.get("stock") or {}
    avail = stock.get("available")
    if avail is None:
        status = obj.get("status")
        return None if status is None else (1 if status == 0 else 0)
    try:
        return 1 if float(avail) > 0 else 0
    except (TypeError, ValueError):
        return None


def _find_card_for_plugin(plugin_tag):
    """
    Find the product card container for a plugin element.

    On Sklavenitis each product is wrapped in:
        <div class="product prGa_{sku}"> ... one plugin + one price ... </div>
    So we climb to the nearest ancestor carrying the "product" class. That
    container holds exactly one price, which avoids the bug of grabbing the
    shared productList container (which holds all 24 prices).
    """
    node = plugin_tag
    for _ in range(8):
        if node is None:
            break
        classes = node.get("class") if hasattr(node, "get") else None
        if classes and "product" in classes:
            return node
        node = node.parent
    # Fallback: immediate parent (should not normally be reached)
    return plugin_tag.parent


def _parse_price_block(card):
    """
    From a product card, return (price, list_price, unit_price, unit).
      price       = current selling price
      list_price  = previous price if discounted, else None
      unit_price  = price per kg/litre if shown
      unit        = the unit string (e.g. 'λίτρο', 'τεμ.')
    """
    price = list_price = unit_price = unit = None

    # Unit price (priceKil): "1,36 €/λίτρο"
    kil = card.find(class_="priceKil")
    if kil:
        txt = kil.get_text(" ", strip=True)
        unit_price = _gr_decimal(re.split(r"€", txt)[0])
        um = re.search(r"/\s*([^\s<]+)", txt)
        if um:
            unit = um.group(1)

    # All price elements in the card
    price_divs = card.find_all(class_="price")

    # Identify a "previous" (struck-through) price if present
    prev_div = None
    cur_div = None
    for pd in price_divs:
        classes = " ".join(pd.parent.get("class", []) + pd.get("class", []))
        if "previous" in classes:
            prev_div = pd
        else:
            if cur_div is None:
                cur_div = pd

    def price_of(div):
        if div is None:
            return None
        dp = div.get("data-price")
        if dp:
            return _gr_decimal(dp)
        return _gr_decimal(div.get_text(" ", strip=True))

    if prev_div is not None:
        list_price = price_of(prev_div)
        # current price is the non-previous one
        cur = next((pd for pd in price_divs if pd is not prev_div), None)
        price = price_of(cur) if cur is not None else None
    else:
        price = price_of(cur_div if cur_div is not None else
                         (price_divs[0] if price_divs else None))

    return price, list_price, unit_price, unit


def harvest_category(page):
    """
    Parse a category page; return a list of normalized product dicts.
    Each: name, brand, sku, price, list_price, unit_price, unit, in_stock, url.
    (name/brand may be None here — see note; sku is always present.)
    """
    soup = BeautifulSoup(page, "html.parser")
    results = []

    for plugin_tag in soup.find_all(attrs={"data-plugin-product": True}):
        raw = plugin_tag.get("data-plugin-product")
        # bs4 already unescapes entity references in attribute values, but be safe
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            try:
                obj = json.loads(htmllib.unescape(raw))
            except Exception:
                continue

        sku = obj.get("sku")
        if not sku:
            continue

        card = _find_card_for_plugin(plugin_tag)
        price, list_price, unit_price, unit = _parse_price_block(card)

        # Product name + link, if present in the card
        name = None
        url = None
        link = card.find("a", href=True)
        if link:
            url = link["href"]
            # Prefer a title attr or aria-label; fall back to link text
            name = (link.get("title") or link.get("aria-label")
                    or link.get_text(" ", strip=True) or None)
        # Sometimes the name is in an <h.. class*='title'> or img alt
        if not name:
            img = card.find("img", alt=True)
            if img and img.get("alt"):
                name = img["alt"].strip()

        results.append({
            "name": name,
            "brand": None,
            "sku": str(sku),
            "price": price,
            "list_price": list_price,
            "unit_price": unit_price,
            "unit": unit,
            "in_stock": _stock_to_bool(obj),
            "url": url,
        })

    return results
