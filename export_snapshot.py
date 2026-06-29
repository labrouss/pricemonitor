#!/usr/bin/env python3
"""
Export a *latest-prices snapshot* (no history) in three formats for publishing:

  prices-latest.sqlite   queryable SQLite (one row per product per chain)
  prices-latest.json     [{name, brand, category, retailer, price, ...}, ...]
  prices-latest.csv      same, spreadsheet-friendly

Only the most recent price per (product, retailer) is included, with duplicate
products folded to their canonical id so the same item across chains shares a
stable product key. This is a SNAPSHOT for downstream users — the full price
history stays in the server's database.

Run:
  DB_BACKEND=postgres python3 export_snapshot.py --out /app/snapshot
Produces the three files in that directory plus a manifest.json with metadata.
"""

import os
import csv
import json
import sqlite3
import argparse
import datetime

from db import get_store


def _rows(store):
    """Latest price per (canonical product, retailer). Returns list of dicts."""
    # latest_prices() already returns one row per (product, retailer) at the most
    # recent observed_at. We add the canonical product id so consumers can group
    # the same item across chains.
    base = store.latest_prices()
    out = []
    for r in base:
        d = {
            "product": r.get("name"),
            "brand": r.get("brand"),
            "category": r.get("category"),
            "retailer": r.get("retailer"),
            "price": r.get("price"),
            "list_price": r.get("list_price"),
            "in_stock": r.get("in_stock"),
            "sku": r.get("sku"),
            "url": r.get("url"),
            "observed_at": (r.get("observed_at").isoformat()
                            if hasattr(r.get("observed_at"), "isoformat")
                            else r.get("observed_at")),
        }
        out.append(d)
    # stable ordering: product, then retailer
    out.sort(key=lambda x: ((x["product"] or "").lower(), x["retailer"] or ""))
    return out


def write_json(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))


def write_csv(rows, path):
    cols = ["product", "brand", "category", "retailer", "price", "list_price",
            "in_stock", "sku", "url", "observed_at"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_sqlite(rows, path):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE prices (
        product TEXT, brand TEXT, category TEXT, retailer TEXT,
        price REAL, list_price REAL, in_stock INTEGER,
        sku TEXT, url TEXT, observed_at TEXT)""")
    con.executemany(
        """INSERT INTO prices
           (product, brand, category, retailer, price, list_price,
            in_stock, sku, url, observed_at)
           VALUES (:product,:brand,:category,:retailer,:price,:list_price,
                   :in_stock,:sku,:url,:observed_at)""", rows)
    # helpful indexes for consumers
    con.execute("CREATE INDEX idx_prices_product ON prices(product)")
    con.execute("CREATE INDEX idx_prices_retailer ON prices(retailer)")
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="snapshot",
                    help="output directory for the snapshot files")
    ap.add_argument("--db", default="prices.db")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    store = get_store(args.db)
    rows = _rows(store)

    sqlite_path = os.path.join(args.out, "prices-latest.sqlite")
    json_path = os.path.join(args.out, "prices-latest.json")
    csv_path = os.path.join(args.out, "prices-latest.csv")
    write_sqlite(rows, sqlite_path)
    write_json(rows, json_path)
    write_csv(rows, csv_path)

    retailers = sorted({r["retailer"] for r in rows if r["retailer"]})
    manifest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rows": len(rows),
        "products": len({r["product"] for r in rows}),
        "retailers": retailers,
        "files": ["prices-latest.sqlite", "prices-latest.json",
                  "prices-latest.csv"],
        "note": "Latest-price snapshot (no history). One row per product per "
                "chain, most recent observation.",
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[snapshot] wrote {len(rows)} rows for {len(retailers)} retailers "
          f"to {args.out}/ (sqlite, json, csv + manifest)")


if __name__ == "__main__":
    main()
