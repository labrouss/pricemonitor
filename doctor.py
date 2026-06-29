"""
Scraper health check ("doctor").

Runs every scraper against a small live sample into a throwaway SQLite database,
then inspects the rows it produced for DATA-QUALITY problems — not just "did it
return something", but the specific failure modes we've hit:

  * zero products      -> discovery/parse broke (site markup changed)
  * brand in name      -> brand text leaking into the product name
  * per-unit prices    -> per-kilo/litre price stored instead of product price
  * missing prices     -> rows with no usable price
  * implausible prices -> 0, negative, or absurdly large values
  * missing fields     -> no category/brand/image coverage at all

Each scraper gets a PASS / WARN / FAIL with specifics. Exit code is non-zero if
any scraper FAILS, so this is usable in CI or a cron sanity gate.

Usage:
    python main.py doctor                 # all scrapers, tiny sample each
    python main.py doctor --retailer ab   # one scraper
    python main.py doctor --limit 15      # sample size per scraper
"""

import re
import time
import tempfile
import traceback
import unicodedata

# Scrapers are imported lazily inside run_doctor so a broken import in one
# module doesn't prevent testing the others.
# Scrapers intentionally disabled due to access controls (not bugs). The doctor
# reports these as BLOCKED rather than failing the whole run.
ACCESS_BLOCKED = {
    "ab": "ab.gr is behind Akamai and IP-blocked this server (HTTP 403). "
          "Disabled to let the IP cool off; AB prices still arrive via posokanei.",
}

ALL_RETAILERS = ["posokanei", "sklavenitis", "ab", "mymarket", "bazaar", "lidl"]


def _fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


# Per-unit suffixes that should NEVER be the product price (if a name or a
# stored price context shows these, flag it).
_UNIT_RE = re.compile(r'(/\s*|αν[αά]\s+)(κιλ|λ[ίι]τρ|kg|kgr|lt|ltr|gr\b|ml\b)', re.I)


def _check_rows(retailer, rows):
    """Inspect harvested rows; return (status, messages). status: PASS/WARN/FAIL."""
    msgs = []
    fails = []
    warns = []

    n = len(rows)
    if n == 0:
        return "FAIL", ["0 products harvested — discovery or parsing is broken "
                        "(site markup may have changed)."]

    # --- price sanity ---
    priced = [r for r in rows if r.get("price") is not None]
    if not priced:
        fails.append("no rows have a price at all.")
    else:
        bad = [r for r in priced if not (0 < (r["price"] or 0) < 1000)]
        if bad:
            warns.append(f"{len(bad)}/{len(priced)} prices look implausible "
                         f"(<=0 or >=1000), e.g. {bad[0].get('name','?')[:40]} "
                         f"= {bad[0].get('price')}.")
        missing = n - len(priced)
        if missing > n * 0.25:
            warns.append(f"{missing}/{n} rows have NO price.")

    # --- brand-in-name leak ---
    leaks = []
    for r in rows:
        name, brand = r.get("name"), r.get("brand")
        if name and brand and _fold(name).startswith(_fold(brand)) and _fold(brand):
            leaks.append(name)
    if leaks:
        share = len(leaks) / n
        ex = leaks[0][:48]
        if share > 0.30:
            fails.append(f"{len(leaks)}/{n} names start with their brand "
                         f"(brand leaking into name), e.g. '{ex}'.")
        else:
            warns.append(f"{len(leaks)}/{n} names start with their brand, "
                         f"e.g. '{ex}'.")

    # --- per-unit price leaking into the NAME (rare) or obvious unit text ---
    unit_named = [r["name"] for r in rows
                  if r.get("name") and _UNIT_RE.search(r["name"])]
    if unit_named:
        warns.append(f"{len(unit_named)} names contain per-unit text "
                     f"(e.g. '{unit_named[0][:40]}') — check price extraction.")

    # --- duplicate prices that look like a per-kilo collision ---
    # Heuristic: if the SAME price repeats across most rows, the scraper may be
    # grabbing a wrong constant (e.g. a banner price). Soft signal only.
    if priced:
        from collections import Counter
        common, count = Counter(round(r["price"], 2) for r in priced).most_common(1)[0]
        if count > len(priced) * 0.5 and len(priced) > 4:
            warns.append(f"{count}/{len(priced)} rows share the same price "
                         f"({common}) — possible extraction error.")

    # --- field coverage (informational warnings, not failures) ---
    for field in ("category", "brand", "image_url"):
        have = sum(1 for r in rows if r.get(field))
        if have == 0:
            warns.append(f"no rows have '{field}'.")

    if fails:
        return "FAIL", fails + warns
    if warns:
        return "WARN", warns
    return "PASS", [f"{n} products, {len(priced)} priced, all checks clean."]


def _harvest_sample(retailer, limit):
    """Run a scraper into a temp DB and return the rows it stored, for inspection.

    IMPORTANT: always use a throwaway SQLite database, even if DB_BACKEND=postgres
    is set in the environment. Otherwise the doctor would write its test products
    into (and read the whole catalog back from) the production Postgres database,
    making every scraper appear to fail with the same catalog-wide counts.
    """
    import importlib
    from storage import Store   # SQLite backend directly, NOT the env-driven factory

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store = Store(tmp.name)
    t0 = time.time()
    err = None
    try:
        mod = importlib.import_module(retailer)
        mod.run(store, max_products=limit)
    except Exception:
        err = traceback.format_exc()
    elapsed = time.time() - t0

    rows = []
    try:
        rows = store.latest_prices()
    except Exception:
        pass
    store.close()
    try:
        import os
        os.unlink(tmp.name)
    except Exception:
        pass
    return rows, elapsed, err


def run_doctor(retailers=None, limit=12):
    """Run the health check. Returns True if all scrapers passed (no FAIL)."""
    retailers = retailers or ALL_RETAILERS
    print("=" * 64)
    print(f"SCRAPER DOCTOR — sampling {limit} products per scraper")
    print("=" * 64)

    results = {}
    for r in retailers:
        if r in ACCESS_BLOCKED:
            results[r] = "BLOCKED"
            print(f"\n▶ {r} …")
            print(f"  ⊘ BLOCKED — {ACCESS_BLOCKED[r]}")
            continue
        print(f"\n▶ {r} …", flush=True)
        rows, elapsed, err = _harvest_sample(r, limit)
        if err:
            results[r] = "FAIL"
            print(f"  FAIL — scraper raised an exception ({elapsed:.1f}s):")
            print("    " + err.strip().splitlines()[-1])
            print("    (full traceback above the summary)")
            print("\n".join("    " + ln for ln in err.strip().splitlines()[-4:]))
            continue
        status, msgs = _check_rows(r, rows)
        results[r] = status
        icon = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[status]
        print(f"  {icon} {status} ({elapsed:.1f}s)")
        for m in msgs:
            print(f"      - {m}")

    print("\n" + "=" * 64)
    print("SUMMARY")
    for r in retailers:
        st = results.get(r, "?")
        icon = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "BLOCKED": "⊘"}.get(st, "?")
        print(f"  {icon} {r:14} {st}")
    print("=" * 64)

    any_fail = any(v == "FAIL" for v in results.values())
    any_warn = any(v == "WARN" for v in results.values())
    if any_fail:
        print("Result: FAIL — at least one scraper is broken.")
    elif any_warn:
        print("Result: WARN — scrapers ran but some data-quality checks flagged.")
    else:
        print("Result: PASS — all scrapers healthy.")
    return not any_fail
