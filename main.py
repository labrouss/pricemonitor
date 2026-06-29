"""
CLI entry point.

Usage:
    python main.py scrape --limit 5      # discover + scrape up to 5 products
    python main.py show                  # print latest recorded prices

Start with a SMALL --limit to confirm the site structure and that you're
parsing prices correctly before scaling up. Be a polite guest.
"""

import os
import sys
import argparse
import logging

from db import get_store
import sklavenitis
import ab
import bazaar
import mymarket
import lidl
import posokanei


def _try_backup(tag="auto"):
    """Best-effort Postgres snapshot before a destructive op. Runs pg_dump to a
    file under /app/backups (host-mounted). Returns True on success. No-op for
    SQLite (the file itself is easy to copy)."""
    import os, subprocess, datetime
    if os.environ.get("DB_BACKEND", "").lower() not in ("postgres", "postgresql", "pg"):
        return True   # SQLite: not applicable, don't block
    url = os.environ.get("DATABASE_URL", "")
    # parse postgresql://user:pass@host:port/db
    import re
    m = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(.+)", url)
    if not m:
        return False
    user, pw, host, port, db = m.group(1), m.group(2), m.group(3), m.group(4) or "5432", m.group(5)
    os.makedirs("/app/backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = f"/app/backups/pricemonitor-{ts}-{tag}.dump"
    env = dict(os.environ, PGPASSWORD=pw)
    try:
        with open(out, "wb") as f:
            subprocess.run(["pg_dump", "-h", host, "-p", port, "-U", user,
                            "-Fc", db], stdout=f, env=env, check=True,
                           stderr=subprocess.PIPE, timeout=600)
        print(f"[backup] pre-op snapshot saved: {out}")
        return True
    except Exception as e:
        print(f"[backup] snapshot failed: {e}")
        try:
            os.remove(out)
        except OSError:
            pass
        return False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ap = argparse.ArgumentParser(description="Supermarket price monitor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="harvest products from a retailer")
    sc.add_argument("--retailer",
                    choices=["sklavenitis", "ab", "bazaar", "mymarket", "lidl",
                             "posokanei"],
                    default="sklavenitis", help="which source to scrape")
    sc.add_argument("--limit", type=int, default=50,
                    help="max products to record")
    sc.add_argument("--max-categories", type=int, default=None,
                    help="(sklavenitis) cap how many category pages to visit")
    sc.add_argument("--db", default="prices.db")
    sc.add_argument("--workers", type=int, default=4,
                    help="(ab) concurrent fetch workers; only used where no "
                         "crawl-delay is set")
    sc.add_argument("--pace", type=float, default=0.75,
                    help="(ab) min seconds between request starts across workers")
    sc.add_argument("--refresh-urls", action="store_true",
                    help="(ab) rebuild the cached product-URL list from sitemap")

    sh = sub.add_parser("show", help="show latest recorded prices")
    sh.add_argument("--db", default="prices.db")

    dc = sub.add_parser("doctor", help="health-check all scrapers (small live "
                                       "sample + data-quality validation)")
    dc.add_argument("--retailer",
                    choices=["sklavenitis", "ab", "bazaar", "mymarket", "lidl",
                             "posokanei"],
                    default=None, help="check just one scraper (default: all)")
    dc.add_argument("--limit", type=int, default=12,
                    help="products to sample per scraper")

    dd = sub.add_parser("dedup", help="scan existing data for duplicate products "
                                      "(auto-merge exact, queue fuzzy for review)")
    dd.add_argument("--db", default="prices.db")
    dd.add_argument("--no-auto", action="store_true",
                    help="do not auto-merge exact matches, only queue candidates")
    dd.add_argument("--no-suggest", action="store_true",
                    help="do not queue fuzzy candidates, only auto-merge exact")
    dd.add_argument("--prune", action="store_true",
                    help="also re-score pending candidates and drop stale ones")

    pg = sub.add_parser("purge-retailer",
                        help="delete one retailer's offers + price history "
                             "(products are kept; they're shared across chains)")
    pg.add_argument("--db", default="prices.db")
    pg.add_argument("--retailer", required=True,
                    help="retailer name to purge (e.g. mymarket)")
    pg.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")

    bk = sub.add_parser("backup", help="snapshot the Postgres database to "
                                       "/app/backups (host-mounted)")
    bk.add_argument("--tag", default="manual", help="label for the backup file")
    bk.add_argument("--db", default="prices.db")

    args = ap.parse_args()

    # doctor runs its own throwaway DBs; it must not touch the real one.
    if args.cmd == "doctor":
        import doctor
        ok = doctor.run_doctor(
            retailers=[args.retailer] if args.retailer else None,
            limit=args.limit)
        return 0 if ok else 1

    if args.cmd == "backup":
        ok = _try_backup(tag=args.tag)
        if ok:
            # rotation: keep newest KEEP dumps
            import glob
            keep = int(os.environ.get("KEEP", "14"))
            dumps = sorted(glob.glob("/app/backups/pricemonitor-*.dump"),
                           reverse=True)
            for old in dumps[keep:]:
                try:
                    os.remove(old); print(f"[backup] rotated out {old}")
                except OSError:
                    pass
        return 0 if ok else 1

    store = get_store(args.db)

    # Make the active backend explicit so a manual CLI run can never silently
    # operate on the wrong database (e.g. local SQLite when you meant Postgres).
    _backend = type(store).__module__
    if _backend == "storage_pg":
        print(f"[db] using Postgres ({os.environ.get('DATABASE_URL','?').split('@')[-1]})")
    else:
        print(f"[db] using SQLite ({args.db}) — "
              f"set DB_BACKEND=postgres to use the production database")

    if args.cmd == "scrape":
        # Block obvious duplicates at ingest when enabled (default on; set
        # BLOCK_ON_INGEST=0 to disable, e.g. for a fast backfill run).
        store.block_on_ingest = os.environ.get("BLOCK_ON_INGEST", "1") != "0"
        if args.retailer == "ab":
            n = ab.run(store, max_products=args.limit, workers=args.workers,
                       pace=args.pace, db_path=args.db,
                       refresh_urls=args.refresh_urls)
        elif args.retailer == "bazaar":
            n = bazaar.run(store, max_products=args.limit,
                           max_categories=args.max_categories)
        elif args.retailer == "mymarket":
            n = mymarket.run(store, max_products=args.limit,
                             max_categories=args.max_categories)
        elif args.retailer == "lidl":
            n = lidl.run(store, max_products=args.limit)
        elif args.retailer == "posokanei":
            n = posokanei.run(store, max_products=args.limit)
        else:
            n = sklavenitis.run(store, max_products=args.limit,
                                max_categories=args.max_categories)
        print(f"\nRecorded {n} product price observations from {args.retailer}.")
        if n == 0:
            print("If this is 0, discovery returned nothing — check the "
                  "sitemap traversal in the retailer module.")
    elif args.cmd == "purge-retailer":
        r = args.retailer
        # Safety: take a backup BEFORE deleting, so a mistake is recoverable.
        # Best-effort — if the backup tooling isn't reachable, warn loudly and
        # require explicit --yes to proceed without one.
        backup_ok = _try_backup(tag=f"pre-purge-{r}")
        if not backup_ok and not args.yes:
            print("WARNING: could not create a pre-purge backup automatically.")
            print("Run ./backup.sh dump pre-purge first, or re-run with --yes to "
                  "proceed without a backup (NOT recommended).")
            return 1
        # Count what will be deleted so the user sees the impact.
        try:
            offers = store._q(
                "SELECT COUNT(*) n FROM offers WHERE retailer=%s", (r,))[0]["n"] \
                if type(store).__module__ == "storage_pg" else \
                store.conn.execute(
                    "SELECT COUNT(*) n FROM offers WHERE retailer=?", (r,)).fetchone()["n"]
        except Exception:
            offers = "?"
        print(f"About to delete ALL offers + price history for retailer '{r}' "
              f"({offers} offers). Products are KEPT (shared across chains).")
        if not args.yes:
            ans = input("Type the retailer name to confirm: ").strip()
            if ans != r:
                print("Aborted (name did not match).")
                return 1
        res = store.purge_retailer(r)
        print(f"Deleted {res['price_history']} price rows and {res['offers']} "
              f"offers for '{r}'. Re-scrape to repopulate.")
    elif args.cmd == "dedup":
        if args.prune:
            pr = store.prune_stale_candidates()
            print(f"Pruned {pr['removed']} stale candidate(s).")
        res = store.scan_duplicates(auto_merge=not args.no_auto,
                                    suggest=not args.no_suggest)
        print(f"Dedup scan: auto-merged {res['auto_merged']}, "
              f"queued {res['candidates_added']} candidate(s) for review.")
    elif args.cmd == "show":
        rows = store.latest_prices()
        if not rows:
            print("No data yet. Run: python main.py scrape --limit 50")
        for r in rows:
            stock = "" if r["in_stock"] is None else (
                "" if r["in_stock"] else " [out of stock]")
            offer = ""
            if r.get("list_price") and r["list_price"] > (r["price"] or 0):
                pct = 100 * (r["list_price"] - r["price"]) / r["list_price"]
                offer = f"  (was {r['list_price']:.2f}, -{pct:.0f}%)"
            print(f"{r['retailer']:12} {r['price']:>7.2f} {r['name']}{offer}{stock}")

    store.close()


if __name__ == "__main__":
    sys.exit(main())
