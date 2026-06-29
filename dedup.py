"""
Product de-duplication.

The same physical product is ingested by different retailer scrapers under
slightly different names ("Coca-Cola 1.5L" vs "Coca Cola 1,5lt"), producing
duplicate product rows. This module decides when two products are the same.

Two tiers (see design discussion):
  * TIER 1 — exact match after normalization: SAFE to auto-merge.
    Deterministic. name+brand+size all normalize identically.
  * TIER 2 — fuzzy: name/brand/size SIMILAR but not identical -> a *candidate*
    for human review, never an automatic merge.

Hard rule shared by both tiers: SIZE MUST MATCH. Two products with the same
name but different extracted size (1L vs 2L) are NEVER the same product.
"""

import re
import unicodedata


def strip_brand_prefix(name, brand):
    """Remove a leading brand prefix from a product name (case/accent-insensitive),
    keeping the brand in its own field. Conservative: only strips a genuine
    leading run, never returns empty. Some retailers (e.g. MyMarket) bake the
    brand into the name; call this in the scraper to store a clean name.

    'ΟΛΥΜΠΟΣ Φυσικός Χυμός' + brand 'ΟΛΥΜΠΟΣ' -> 'Φυσικός Χυμός'.
    """
    if not name or not brand:
        return name
    fname, fbrand = strip_accents(name).lower().strip(), strip_accents(brand).lower().strip()
    if not fbrand or not fname.startswith(fbrand):
        return name
    if name[:len(brand)].lower() == brand.lower():
        rest = name[len(brand):]
    else:
        brand_tok, name_tok = brand.split(), name.split()
        if len(name_tok) > len(brand_tok) and \
           strip_accents(" ".join(name_tok[:len(brand_tok)])).lower() == fbrand:
            rest = " ".join(name_tok[len(brand_tok):])
        else:
            return name
    rest = rest.lstrip(" -–—·•|").strip()
    return rest or name


def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


# Unit aliases -> canonical unit. Greek and English forms both appear in data.
_UNIT_CANON = {
    # volume
    "l": "l", "lt": "l", "ltr": "l", "liter": "l", "litre": "l", "liters": "l",
    "litres": "l", "λ": "l", "λιτ": "l", "λιτρο": "l", "λιτρα": "l", "λτ": "l",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "μλ": "ml",
    "cl": "cl",
    # mass
    "kg": "kg", "kgr": "kg", "kilo": "kg", "kilos": "kg", "κg": "kg",
    "κιλο": "kg", "κιλα": "kg", "κ": "kg",
    "g": "g", "gr": "g", "gram": "g", "grams": "g", "grammar": "g",
    "γρ": "g", "γραμ": "g", "γραμμαρια": "g", "γραμμαριο": "g",
    # count / pieces
    "tem": "pc", "τεμ": "pc", "τεμαχ": "pc", "τεμαχια": "pc", "τεμαχιο": "pc",
    "τμχ": "pc", "τμ": "pc", "pcs": "pc", "pc": "pc", "piece": "pc",
    "pieces": "pc", "φακ": "pc", "φακελα": "pc", "φακελακια": "pc",
    "ρολα": "pc", "ρολο": "pc", "φυλλα": "pc",
    # dose / wash counts — common on detergents (50Μεζ = 50 doses, 40 πλύσεις)
    "μεζ": "pc", "μεζουρες": "pc", "μεζουρα": "pc", "δοσεις": "pc",
    "πλυσεις": "pc", "πλυση": "pc", "washes": "pc", "wash": "pc",
    "καψουλες": "pc", "καψουλα": "pc", "tabs": "pc", "tab": "pc",
}

# Normalise everything to a base unit so 1.5l == 1500ml, 500g == 0.5kg.
_TO_BASE = {"l": ("vol", 1000.0), "ml": ("vol", 1.0), "cl": ("vol", 10.0),
            "kg": ("mass", 1000.0), "g": ("mass", 1.0),
            "pc": ("pc", 1.0)}

# Order matters: longer/more specific alternations first so "ml" wins over "l",
# "kg" over "g", "τεμαχια" over "τεμ". Built from the canon keys, longest first.
_UNIT_ALT = "|".join(
    re.escape(u) for u in sorted(_UNIT_CANON.keys(), key=len, reverse=True))

_SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(" + _UNIT_ALT + r")\b", re.IGNORECASE)
# multipack like "6x330ml", "4 x 1.5 l", "6 τεμ x 330ml", "2x200g"
_MULTI_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(" + _UNIT_ALT + r")\b", re.IGNORECASE)
# bare piece count like "6 τεμάχια", "20τεμ", "πακέτο 6" handled via _SIZE_RE
# since τεμ etc. are in the unit alternation.

# Promo pack like "5+1", "4+2 Δώρο" — these distinguish otherwise-identical
# products (same per-unit size, different bundle). Captured separately.
_PROMO_RE = re.compile(r"\b(\d+)\s*\+\s*(\d+)\b")
# A leading size descriptor like "No7", "Νο 1", "Large/Μεγάλο/Μεσαίο" that
# distinguishes variants (diaper size, bag size). Numbers after No/Νο.
_NO_RE = re.compile(r"\b(?:no|ν[οο])\s*[.:]?\s*(\d+)\b", re.IGNORECASE)
# SPF factor (sunscreen) — SPF30 vs SPF50 are different products.
_SPF_RE = re.compile(r"\bspf\s*(\d+)\b", re.IGNORECASE)
# Fat / content percentage — "5%", "1,5%", "2,0%". Normalized to a number so
# "2%" == "2,0%" (formatting) but "5%" != "1,5%" (real difference).
_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
# Cosmetic shade / model code: a standalone integer that is NOT a size and NOT
# followed by a unit — e.g. "Brow Artist Le Skinny 101", "Teddy Tint 35".
# Captured only when it's a 2-3 digit code not attached to a unit.
_SHADE_RE = re.compile(
    r"\b(\d{2,3})\b(?!\s*(?:%|" +
    "|".join(re.escape(u) for u in sorted(_UNIT_CANON.keys(), key=len, reverse=True)) +
    r"|[x×]))", re.IGNORECASE)
# Physical dimensions like "90x60" (pads, sheets) — different sizes.
_DIM_RE = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\s*(?:cm|εκ)?\b", re.IGNORECASE)


def quantity_signature(text):
    """
    Return a hashable signature of ALL distinguishing quantity tokens in a name,
    so two products that share a per-unit size but differ in pack/promo/variant
    do NOT match. Includes: the canonical size, any promo pack (5+1), any
    explicit pack count (τεμάχια), and any No/Νο variant number.

    Used as a hard gate alongside extract_size: if two products' signatures
    differ on a component BOTH possess, they are different products.
    """
    if not text:
        return {}
    t = strip_accents(text).lower()
    sig = {}

    size = extract_size(text)
    if size:
        sig["size"] = size

    # promo pack, normalized as an unordered pair sum+parts so "5+1" != "4+2"
    pm = _PROMO_RE.search(t)
    if pm:
        a, b = int(pm.group(1)), int(pm.group(2))
        sig["promo"] = (a + b, tuple(sorted((a, b))))

    # explicit piece count (even if extract_size chose a vol/mass instead)
    pcs = []
    for mm in _SIZE_RE.finditer(t):
        unit = _UNIT_CANON.get(mm.group(2).lower())
        if unit == "pc":
            pcs.append(float(mm.group(1).replace(",", ".")))
    if pcs:
        sig["pieces"] = max(pcs)        # the pack count

    # No/Νο variant number (diaper size, etc.)
    nm = _NO_RE.search(t)
    if nm:
        sig["no"] = int(nm.group(1))

    # SPF factor (sunscreen)
    sp = _SPF_RE.search(t)
    if sp:
        sig["spf"] = int(sp.group(1))

    # fat/content percentage, normalized so 2% == 2,0% but 5% != 1,5%
    pcts = sorted(float(x.replace(",", ".")) for x in _PCT_RE.findall(t))
    if pcts:
        sig["pct"] = tuple(pcts)

    # cosmetic shade / model codes (2-3 digit standalone numbers not size/unit).
    # Only meaningful when present on BOTH sides; collected as a set.
    shades = set(int(x) for x in _SHADE_RE.findall(t))
    # remove any that are actually part of a size we already captured
    if "size" in sig:
        shades.discard(int(sig["size"][1]) if sig["size"][1] == int(sig["size"][1]) else -1)
    if shades:
        sig["shade"] = frozenset(shades)

    # physical dimensions (pads/sheets like 90x60) — store as a sorted pair
    dm = _DIM_RE.search(t)
    if dm:
        sig["dim"] = tuple(sorted((int(dm.group(1)), int(dm.group(2)))))

    return sig


def signatures_conflict(a_text, b_text):
    """True if two names share a quantity component but with DIFFERENT values —
    i.e. they are distinguishable variants and must not match."""
    sa, sb = quantity_signature(a_text), quantity_signature(b_text)
    for key in ("size", "promo", "pieces", "no", "spf", "dim", "pct", "shade"):
        if key in sa and key in sb and sa[key] != sb[key]:
            return True
    return False


def extract_size(text):
    """
    Return a canonical (dimension, base_amount) tuple for the size in text, or
    None if no size is present. dimension is 'vol' (ml), 'mass' (g), or 'pc'
    (count). Multipacks are multiplied out: "6x330ml" -> ("vol", 1980.0),
    "6 τεμ" -> ("pc", 6.0). When several sizes appear, the LAST volume/mass
    wins (Greek names usually end with the pack size), but a multipack takes
    priority since it's the most specific.
    """
    if not text:
        return None
    t = strip_accents(text).lower()

    # Multipack first — most specific.
    m = _MULTI_RE.search(t)
    if m:
        count = float(m.group(1))
        amount = float(m.group(2).replace(",", "."))
        unit = _UNIT_CANON.get(m.group(3).lower())
        if unit in _TO_BASE:
            dim, factor = _TO_BASE[unit]
            return (dim, round(count * amount * factor, 3))

    # All size tokens; prefer a weight/volume over a bare piece count when both
    # are present (e.g. "6 τεμ 330ml" -> the 330ml*? ambiguous, but a real
    # vol/mass is the better identity signal than the piece count).
    matches = list(_SIZE_RE.finditer(t))
    if not matches:
        return None
    best = None         # (priority, dim, base_amount)
    for mm in matches:
        amount = float(mm.group(1).replace(",", "."))
        unit = _UNIT_CANON.get(mm.group(2).lower())
        if unit not in _TO_BASE:
            continue
        dim, factor = _TO_BASE[unit]
        base = round(amount * factor, 3)
        # priority: vol/mass (2) beats pc (1); among same priority, last wins
        prio = 1 if dim == "pc" else 2
        if best is None or prio >= best[0]:
            best = (prio, dim, base)
    if best:
        return (best[1], best[2])
    return None


def sizes_compatible(a_text, b_text):
    """True if two product texts have compatible sizes for matching.

    - both missing a size            -> compatible (size can't disprove)
    - one missing, one present       -> compatible (don't block on absence)
    - both present, SAME dim+amount  -> compatible
    - both present, differ           -> NOT compatible (hard gate)
    Different DIMENSIONS (200g vs 200τεμ) are never compatible.
    """
    sa, sb = extract_size(a_text), extract_size(b_text)
    if sa is None or sb is None:
        return True
    return sa == sb


# Tokens that carry no identity signal (drop before comparing names).
_STOP = {"the", "and", "με", "και", "του", "της", "το", "η", "ο", "σε",
         "για", "gia", "apo", "από", "στο", "στη", "των", "ένα", "ενα",
         "of", "for", "with"}

# Words that are noise when they appear as a token difference — abbreviation
# variants, percentages-as-fragments, etc. (numbers handled separately).
_DIFF_NOISE = {"gr", "g", "ml", "lt", "l", "kg", "tem", "τεμ", "pcs", "pc",
               "x", "συσκευασια", "πακετο", "τμχ"}


def variant_difference(name_a, brand_a, name_b, brand_b):
    """
    Return the set of MEANINGFUL content words by which two product names differ
    (after brand-strip + size removal), excluding stopwords, pure numbers, and
    unit/abbreviation noise. A non-empty result means the names differ by a real
    word — likely a distinguishing VARIANT (honey vs plain, eco, organic, white
    chocolate) rather than mere word-order/abbreviation noise.
    """
    ta = set(norm_name_nobrand(name_a, brand_a).split())
    tb = set(norm_name_nobrand(name_b, brand_b).split())
    diff = ta.symmetric_difference(tb)
    meaningful = set()
    for w in diff:
        if w in _STOP or w in _DIFF_NOISE:
            continue
        if w.isdigit() or w.replace(",", "").replace(".", "").isdigit():
            continue                       # number fragments like '0' from 2,0%
        if len(w) <= 1:
            continue
        meaningful.add(w)
    return meaningful


def norm_name(name):
    """Normalised name for matching: accents stripped, size removed (compared
    separately), punctuation collapsed, tokens sorted so word order doesn't
    matter ('Γάλα Φρέσκο' == 'Φρέσκο Γάλα')."""
    s = strip_accents(name or "").lower()
    s = _MULTI_RE.sub(" ", s)
    s = _SIZE_RE.sub(" ", s)          # remove size; it's matched on its own
    s = re.sub(r"[^a-z0-9α-ω ]+", " ", s)
    toks = [w for w in s.split() if w and w not in _STOP]
    return " ".join(sorted(toks))


def norm_name_nobrand(name, brand):
    """Like norm_name, but with the brand's tokens removed from the name.

    Retailers often bake the brand INTO the product name on one chain but not
    another ('ΟΛΥΜΠΟΣ Φυσικός Χυμός' vs 'Φυσικός Χυμός'). Removing the brand
    tokens makes those two normalise identically so they match as duplicates.
    """
    name_toks = norm_name(name).split()
    brand_toks = set(norm_brand(brand).split())
    if brand_toks:
        kept = [t for t in name_toks if t not in brand_toks]
        # don't let stripping erase the whole name (e.g. brand == name)
        if kept:
            name_toks = kept
    return " ".join(sorted(name_toks))


def norm_brand(brand):
    s = strip_accents(brand or "").lower()
    s = re.sub(r"[^a-z0-9α-ω ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_key(name, brand):
    """The TIER-1 exact key: identical name (brand-stripped) + brand + size +
    distinguishing quantity tokens (promo pack, pack count, No/Νο variant), so
    '...330ml 5+1' and '...330ml 4+2' get DIFFERENT keys and never auto-merge."""
    size = extract_size(name)
    sz = f"{size[0]}:{size[1]}" if size else "none"
    sig = quantity_signature(name)
    extra = []
    if "promo" in sig:
        extra.append(f"promo{sig['promo']}")
    if "no" in sig:
        extra.append(f"no{sig['no']}")
    # pack count only adds to the key if there's ALSO a vol/mass size, so a
    # plain "20 τεμάχια" still keys by its size normally.
    if "pieces" in sig and size and size[0] != "pc":
        extra.append(f"pc{sig['pieces']}")
    extra_s = "|".join(extra)
    return f"{norm_name_nobrand(name, brand)}|{norm_brand(brand)}|{sz}|{extra_s}"


def _tokens(name, brand=None):
    return set(norm_name_nobrand(name, brand).split()) if brand is not None \
        else set(norm_name(name).split())


def jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _token_sort_ratio(a, b):
    """rapidfuzz token_sort_ratio in [0,1] if available, else a difflib
    approximation on sorted tokens. Catches shuffled word order."""
    try:
        from rapidfuzz import fuzz
        return fuzz.token_sort_ratio(a, b) / 100.0
    except Exception:
        import difflib
        sa, sb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
        return difflib.SequenceMatcher(None, sa, sb).ratio()


def fuzzy_score(name_a, brand_a, name_b, brand_b):
    """
    Similarity in [0,1] for TIER-2 candidate suggestion. Returns 0.0 when sizes
    are present and differ (a hard block — never suggest across sizes, including
    different DIMENSIONS like 200g vs 200τεμ).

    Name similarity blends Jaccard token overlap with a token-sort ratio so that
    reordered names ('Μπισκότα Παπαδοπούλου' vs 'Παπαδοπούλου Μπισκότα') and
    minor spelling differences both score well — this is what carries the
    matching now that embeddings are gone.
    """
    sa, sb = extract_size(name_a), extract_size(name_b)
    if sa and sb and sa != sb:
        return 0.0                      # different sizes/dimensions: hard block
    # Distinguishing quantity tokens (promo packs 5+1 vs 4+2, pack counts,
    # No/Νο variant numbers): if they share a component but differ, block.
    if signatures_conflict(name_a, name_b):
        return 0.0
    if (sa is None) != (sb is None):
        size_penalty = 0.15             # one has size, other doesn't: slight doubt
    else:
        size_penalty = 0.0

    ea = norm_name_nobrand(name_a, brand_a)
    eb = norm_name_nobrand(name_b, brand_b)
    jac = jaccard(set(ea.split()), set(eb.split()))
    tsr = _token_sort_ratio(ea, eb)
    name_sim = 0.5 * jac + 0.5 * tsr    # overlap + order/spelling robustness

    nb_a, nb_b = norm_brand(brand_a), norm_brand(brand_b)
    if nb_a and nb_b:
        brand_sim = 1.0 if nb_a == nb_b else 0.0
    else:
        brand_sim = 0.5                 # unknown brand on either side: neutral

    # Distinguishing-word penalty: if the names differ by a MEANINGFUL content
    # word (honey vs plain, eco, organic, white), they're likely different
    # variants. Penalize per extra word so they drop below the auto-merge/bulk
    # thresholds but can still surface for human review. Word-order and
    # abbreviation differences (gr/g, 'για', number fragments) are NOT penalized.
    variant_words = variant_difference(name_a, brand_a, name_b, brand_b)
    variant_penalty = min(0.25, 0.13 * len(variant_words))

    score = 0.7 * name_sim + 0.3 * brand_sim - size_penalty - variant_penalty
    return max(0.0, round(score, 3))


# Threshold bands for the review queue.
FUZZY_SUGGEST = 0.72   # >= this (and < 1.0 exact) -> show as a candidate
