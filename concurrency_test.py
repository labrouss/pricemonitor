"""
Concurrency smoke test — proves parallel writers don't collide on Postgres.

Run AFTER the Postgres is up (e.g. `docker compose up -d db`):
    DATABASE_URL=postgresql://price:price@localhost:5432/pricemonitor \
    DB_BACKEND=postgres python concurrency_test.py

Spawns N threads that each write many products/prices at the same time. On
SQLite this would throw "database is locked"; on Postgres it should complete
cleanly. Prints the resulting row counts.
"""

import os
import threading
import time

os.environ.setdefault("DB_BACKEND", "postgres")
from db import get_store

WRITERS = 8
PER_WRITER = 200


def writer(wid, errors):
    try:
        store = get_store()
        for i in range(PER_WRITER):
            oid = store.upsert_product(
                retailer=f"chain{wid}",
                name=f"Product {i}",            # same names across writers ->
                url=f"http://c{wid}/p{i}",       # exercises ON CONFLICT on product_key
                shared_key=f"P{i}",              # shared -> one product, many offers
                brand="Brand", category="Cat")
            store.record_price(oid, round(1 + (i % 10) * 0.5, 2))
        store.close()
    except Exception as e:
        errors.append((wid, repr(e)))


def main():
    errors = []
    threads = [threading.Thread(target=writer, args=(w, errors)) for w in range(WRITERS)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0

    store = get_store()
    s = store.stats()
    store.close()

    print(f"{WRITERS} writers × {PER_WRITER} items in {dt:.1f}s")
    print("errors:", errors if errors else "NONE")
    print("stats:", s)
    expected_products = PER_WRITER       # P0..P199 shared across writers
    expected_offers = PER_WRITER * WRITERS
    print(f"expected ~{expected_products} products, ~{expected_offers} offers")
    assert not errors, "concurrency errors occurred!"
    print("\nPARALLEL WRITES OK — no locking, ON CONFLICT resolved collisions.")


if __name__ == "__main__":
    main()
