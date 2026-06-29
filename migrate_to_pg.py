"""
One-time migration: copy an existing SQLite prices.db into Postgres.

Usage:
    DATABASE_URL=postgresql://price:price@localhost:5432/pricemonitor \
    python migrate_to_pg.py prices.db

Preserves products, offers, and full price history. Safe to re-run: it upserts
products/offers by their natural keys and appends price rows (so don't run it
twice against the same source unless you want duplicate history rows).
"""

import sys
import sqlite3

from storage_pg import Store as PgStore


def main(sqlite_path):
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    pg = PgStore()

    products = src.execute("SELECT * FROM products").fetchall()
    print(f"Migrating {len(products)} products…")

    # Map old product_id -> list of (old_offer rows) is implicit via offers table.
    # New schema: we re-insert products, offers, then price_history.
    pcols = {r["name"] for r in src.execute("PRAGMA table_info(products)")}
    has_offers = "offers" in {r["name"] for r in
                              src.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    if not has_offers:
        print("Source DB isn't in the normalized schema. Open it once with the "
              "SQLite Store first (it auto-migrates), then re-run this.")
        return

    # old_offer_id -> new_offer_id
    offer_map = {}

    with pg.pool.connection() as conn:
        for p in products:
            pkey = p["product_key"]
            cur = conn.execute(
                """INSERT INTO products (product_key,name,brand,category,image_url,first_seen,last_seen)
                   VALUES (%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now()), %s::timestamptz)
                   ON CONFLICT (product_key) DO UPDATE SET name=EXCLUDED.name
                   RETURNING id""",
                (pkey, p["name"], p["brand"],
                 p["category"] if "category" in pcols else None,
                 p["image_url"] if "image_url" in pcols else None,
                 p["first_seen"], p["last_seen"] if "last_seen" in pcols else None))
            new_pid = cur.fetchone()["id"]

            offers = src.execute(
                "SELECT * FROM offers WHERE product_id=?", (p["id"],)).fetchall()
            for o in offers:
                cur = conn.execute(
                    """INSERT INTO offers (product_id,retailer,retailer_sku,url,first_seen,last_seen)
                       VALUES (%s,%s,%s,%s, COALESCE(%s::timestamptz, now()), %s::timestamptz)
                       ON CONFLICT (product_id,retailer) DO UPDATE SET retailer_sku=EXCLUDED.retailer_sku
                       RETURNING id""",
                    (new_pid, o["retailer"], o["retailer_sku"], o["url"],
                     o["first_seen"], o["last_seen"]))
                offer_map[o["id"]] = cur.fetchone()["id"]
        conn.commit()

        # price history
        total = 0
        for old_oid, new_oid in offer_map.items():
            hrows = src.execute(
                "SELECT * FROM price_history WHERE offer_id=?", (old_oid,)).fetchall()
            for h in hrows:
                conn.execute(
                    """INSERT INTO price_history
                       (offer_id, observed_at, price, list_price, currency, in_stock, unit_price, unit, raw)
                       VALUES (%s, %s::timestamptz, %s,%s,%s,%s,%s,%s,%s)""",
                    (new_oid, h["observed_at"], h["price"], h["list_price"],
                     h["currency"], h["in_stock"], h["unit_price"], h["unit"], h["raw"]))
                total += 1
            if total and total % 5000 == 0:
                conn.commit()
                print(f"  …{total} price rows")
        conn.commit()
        print(f"Migrated {len(offer_map)} offers and {total} price observations.")

    pg.close()
    src.close()
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python migrate_to_pg.py <prices.db>")
        sys.exit(1)
    main(sys.argv[1])
