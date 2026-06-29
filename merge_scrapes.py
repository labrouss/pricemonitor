#!/usr/bin/env python3
"""
Merge several per-retailer SQLite scrape outputs (produced by parallel CI jobs)
into one combined SQLite database, re-using the normal Store ingest path so the
same identity/dedup logic applies.

Each input DB was written by `main.py scrape --retailer X --db retailer-X.db`.
This reads the latest price per (product, retailer) from each input and replays
it into the combined store via upsert_product + record_price.

Usage:
  python3 merge_scrapes.py --out combined.db retailer-*.db
"""

import sys
import glob
import sqlite3
import argparse

from storage import Store


def _latest_rows(path):
    """Read latest price per (product, retailer) from a scrape DB."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
            SELECT p.name, p.brand, p.category, p.image_url, p.product_key,
                   o.retailer, o.retailer_sku AS sku, o.url,
                   h.price, h.list_price, h.in_stock, h.unit_price, h.unit
            FROM products p
            JOIN offers o ON o.product_id = p.id
            JOIN price_history h ON h.offer_id = o.id
            JOIN (SELECT offer_id, MAX(observed_at) mx FROM price_history
                  GROUP BY offer_id) last
              ON last.offer_id = h.offer_id AND last.mx = h.observed_at
        """).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        print(f"[merge] skipping {path}: {e}")
        return []
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="combined.db")
    ap.add_argument("inputs", nargs="+", help="per-retailer SQLite files (globs ok)")
    args = ap.parse_args()

    paths = []
    for pat in args.inputs:
        paths.extend(sorted(glob.glob(pat)))
    if not paths:
        print("[merge] no input DBs matched"); return 1

    store = Store(args.out)
    total = 0
    for path in paths:
        rows = _latest_rows(path)
        print(f"[merge] {path}: {len(rows)} rows")
        for r in rows:
            try:
                offer_id = store.upsert_product(
                    retailer=r["retailer"], name=r["name"],
                    url=r.get("url") or "", sku=r.get("sku"),
                    brand=r.get("brand"), category=r.get("category"),
                    image_url=r.get("image_url"),
                    shared_key=r.get("product_key") or r["name"])
                store.record_price(
                    offer_id, r.get("price"), list_price=r.get("list_price"),
                    in_stock=r.get("in_stock"), unit_price=r.get("unit_price"),
                    unit=r.get("unit"))
                total += 1
            except Exception as e:
                print(f"[merge] row error ({r.get('name')!r}): {e}")
    store.close()
    print(f"[merge] merged {total} rows from {len(paths)} files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
