"""
mymarket.gr parser (server-rendered, Hotwired/Stimulus).

Category pages embed BOTH:
  - a JSON-LD ItemList with clean product identity (name, sku, url, brand) but
    a price field that is NOT the displayed price (looks like a per-unit base).
  - HTML product cards (data-controller="product-button") that show the REAL
    displayed price (e.g. "0,75€") and carry the sku ("Κωδ: 198993").

Strategy: read identity from JSON-LD, read the displayed price from the HTML
card, and pair them by sku. This yields clean names + correct prices.

No per-product barcode/EAN is exposed (the data-controller="barcode" is the
camera-scanner widget). The sku is Mymarket's internal product code.
"""

import re
import json
import logging
import unicodedata

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _gr_price(s):
    if not s:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{1,2}|\d+,\d{1,2}|\d+\.\d{1,2}|\d+)", s)
    if not m:
        return None
    num = m.group(1)
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _strip_brand_prefix(name, brand):
    """Thin wrapper over the shared dedup.strip_brand_prefix (imported lazily to
    avoid a hard dependency if dedup is unavailable in some run contexts)."""
    try:
        from dedup import strip_brand_prefix
        return strip_brand_prefix(name, brand)
    except Exception:
        return name


def _jsonld_items(html):
    """Return {sku: {name,url,brand,...}} from the JSON-LD ItemList."""
    out = {}
    for b in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        html, re.DOTALL):
        if "ItemList" not in b:
            continue
        try:
            data = json.loads(b)
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph", [data]) if isinstance(data, dict) else (
            data if isinstance(data, list) else [data])
        for node in graph:
            if not isinstance(node, dict) or node.get("@type") != "ItemList":
                continue
            for li in node.get("itemListElement", []):
                item = li.get("item", li) if isinstance(li, dict) else None
                if not isinstance(item, dict):
                    continue
                sku = item.get("sku")
                if not sku:
                    continue
                brand = item.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                if brand == "Generic":
                    brand = None
                image = item.get("image")
                if isinstance(image, list):
                    image = image[0] if image else None
                if isinstance(image, dict):
                    image = image.get("url")
                out[str(sku)] = {
                    "name": _strip_brand_prefix(item.get("name"), brand),
                    "url": item.get("url"),
                    "brand": brand,
                    "category": item.get("category"),
                    "image_url": image if isinstance(image, str) else None,
                }
    return out


# "Κωδ: 198993" -> 198993
SKU_RE = re.compile(r"Κωδ(?:ικ[οό]ς)?[:.\s]*([0-9]+)", re.IGNORECASE)


def _card_prices(html):
    """Return {sku: {price, list_price, in_stock, url}} from HTML product cards."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    # Each product card contains a .sku element; climb to a card ancestor that
    # also holds the displayed price.
    for sku_el in soup.find_all(class_="sku"):
        m = SKU_RE.search(sku_el.get_text(" ", strip=True))
        if not m:
            continue
        sku = m.group(1)

        card = sku_el
        for _ in range(7):
            if card.parent is None:
                break
            card = card.parent
            if "€" in card.get_text() and card.find(class_="sku"):
                break

        # Displayed price: prefer an explicit current/sale price class, else
        # the first €-bearing text in the card — but EXCLUDE the per-unit
        # reference price (Greek shops show "τιμή ανά κιλό/λίτρο", e.g.
        # "12,50 €/κιλό"), which must not be mistaken for the product price.
        price = list_price = None
        old_el = card.find(class_=re.compile(r"(old|strike|line-through|was)", re.I))
        if old_el:
            list_price = _gr_price(old_el.get_text(" ", strip=True))

        # PRIMARY: MyMarket renders the real product price as split spans —
        #   <span class="teaser-display-price-whole">2</span>
        #   <span class="teaser-display-price-fraction">29</span>
        # (the per-kilo "Τιμή κιλού" is separate flat text). Assemble from these
        # structured classes when present; this is the authoritative source and
        # avoids the per-unit-price confusion entirely.
        whole_el = card.find(class_=re.compile(r"teaser-display-price-whole"))
        frac_el = card.find(class_=re.compile(r"teaser-display-price-fraction"))
        if whole_el is not None:
            whole = re.sub(r"\D", "", whole_el.get_text(strip=True))
            frac = re.sub(r"\D", "", frac_el.get_text(strip=True)) if frac_el else "0"
            if whole != "":
                try:
                    price = float(f"{whole}.{frac or '0'}")
                except ValueError:
                    price = None
        # also try a structured OLD/strike teaser price for list_price
        if list_price is None:
            ow = card.find(class_=re.compile(r"teaser-display-.*old.*whole|old.*price.*whole"))
            of = card.find(class_=re.compile(r"teaser-display-.*old.*fraction|old.*price.*fraction"))
            if ow is not None:
                w = re.sub(r"\D", "", ow.get_text(strip=True))
                f = re.sub(r"\D", "", of.get_text(strip=True)) if of else "0"
                if w:
                    try:
                        list_price = float(f"{w}.{f or '0'}")
                    except ValueError:
                        pass

        if price is not None:
            # got the authoritative structured price — record and move on, skip
            # the fragile text-scanning path below entirely.
            url = None
            a = card.find("a", href=True)
            if a:
                url = a["href"]
            # in_stock column is INTEGER (1/0), not boolean — Postgres rejects a
            # Python bool here, so store an int.
            sold_out = card.find(string=re.compile(
                r"εξαντλ|μη\s+διαθ|out\s+of\s+stock", re.I))
            in_stock = 0 if sold_out else 1
            out[sku] = {"price": price, "list_price": list_price,
                        "in_stock": in_stock, "url": url}
            continue

        # Defensive: some cards put the per-unit price in its own element whose
        # class hints at it (e.g. class="unit-price"/"price-per-kilo") without a
        # textual "/kg" suffix. Remove those elements before reading the text so
        # their figures can't be mistaken for the product price.
        for ue in card.find_all(class_=re.compile(
                r"(unit[-_]?price|per[-_]?(kilo|kg|litre|liter|unit)|price[-_]?per|ana[-_]?kilo"
                r"|price[-_]?analysis|measure[-_]?label)",
                re.I)):
            ue.extract()

        # MyMarket (and other Greek shops) label the legally-required per-unit
        # reference price "Τιμή κιλού" / "Τιμή λίτρου" / "Τιμή τεμαχίου" right
        # next to the figure (e.g. "8,60€ Τιμή κιλού"). For each match, remove
        # the smallest enclosing element that contains a euro figure — but never
        # one that also contains a SECOND, separate euro figure (the product
        # price), so we strip only the unit-price box.
        _UNITLABEL = re.compile(
            r"τιμ[ηή]\s*(κιλο[υύ]|λιτρο[υύ]|τεμαχ[ιί]ου)", re.IGNORECASE)
        for el in list(card.find_all(string=_UNITLABEL)):
            node = el.parent
            target = None
            while node is not None and node is not card:
                txt = node.get_text(" ", strip=True)
                euros = txt.count("€")
                if euros >= 1 and _UNITLABEL.search(txt):
                    if euros == 1:
                        target = node          # exactly the unit price + label
                    else:
                        break                  # also holds product price → stop
                node = node.parent
            if target is not None:
                target.extract()

        card_text = card.get_text(" ", strip=True)
        # Belt-and-suspenders: strip any "<num>€ Τιμή κιλού" still in flat text.
        card_text = re.sub(
            r"[\d.,]+\s*€\s*τιμ[ηή]\s*(?:κιλο[υύ]|λιτρο[υύ]|τεμαχ[ιί]ου)",
            " ", card_text, flags=re.IGNORECASE)
        # Capture each euro figure ALONG WITH any per-unit suffix right after it,
        # so we can tell a unit price ("12,50 €/κιλό" or "12,50 € ανά κιλό") from
        # the product price. Group 1 = number, group 2 = unit suffix if present.
        UNIT_AFTER = (r'(?:/\s*|αν[αά]\s+)'
                      r'(?:κιλ[οό]|λ[ίι]τρ[οο]|τεμ[αά]?χ?ι?ο?|kg|kgr|lt|ltr|l|gr|g|ml)\b')
        euro_with_ctx = re.findall(
            r'([\d.,]+)\s*€\s*(' + UNIT_AFTER + r')?', card_text, re.IGNORECASE)
        # Keep only figures that are NOT per-unit reference prices.
        product_prices = []
        for num, unit_suffix in euro_with_ctx:
            v = _gr_price(num + " €")
            if v is None:
                continue
            if unit_suffix:        # this one is a per-kilo/litre/unit price → skip
                continue
            product_prices.append(v)

        # current price = first non-unit euro figure that isn't the old price
        for v in product_prices:
            if v != list_price:
                price = v
                break
        if price is None and product_prices:
            price = product_prices[0]

        # product link
        url = None
        a = card.find("a", href=True)
        if a:
            url = a["href"]

        if sku not in out or out[sku].get("price") is None:
            out[sku] = {"price": price, "list_price": list_price,
                        "in_stock": None, "url": url}
    return out


def harvest_listing(html):
    """
    Parse a Mymarket category page -> list of product dicts.
    Identity from JSON-LD, price from HTML card, paired by sku.
    """
    identity = _jsonld_items(html)
    prices = _card_prices(html)

    results = []
    skus = set(identity) | set(prices)
    for sku in skus:
        idn = identity.get(sku, {})
        pr = prices.get(sku, {})
        price = pr.get("price")
        if price is None:
            continue  # no displayed price -> skip
        results.append({
            "name": idn.get("name"),
            "brand": idn.get("brand"),
            "sku": sku,
            "ean": None,
            "price": price,
            "list_price": pr.get("list_price"),
            "in_stock": pr.get("in_stock"),
            "url": idn.get("url") or pr.get("url"),
            "category": idn.get("category"),
            "image_url": idn.get("image_url"),
        })
    return results
