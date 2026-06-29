#!/usr/bin/env python3
"""
Sync the published price snapshot from GitHub Releases into the LOCAL database
(Postgres for the webapp, SQLite for the PySide app — whichever the active
DB_BACKEND points at).

It downloads prices-latest.json from the repo's "latest" release and replays
each row through the normal Store ingest path (upsert_product + record_price),
so the same identity/dedup logic applies and price history accrues locally with
each sync.

Usage:
  python3 sync_snapshot.py                       # uses DEFAULT_REPO below
  python3 sync_snapshot.py --repo labrouss/pricemonitor
  python3 sync_snapshot.py --url <direct json url>
  DB_BACKEND=postgres python3 sync_snapshot.py   # into Postgres
  python3 sync_snapshot.py --db prices.db        # into a specific SQLite file

Also callable via:  python3 main.py sync [--repo ...] [--db ...]
"""

import os
import sys
import json
import urllib.request
import argparse
import datetime

DEFAULT_REPO = "labrouss/pricemonitor"


def _snapshot_url(repo, asset="prices-latest.json"):
    return f"https://github.com/{repo}/releases/latest/download/{asset}"


def fetch_snapshot(url, token=None, timeout=120):
    """Download the JSON snapshot. Returns the parsed list of rows."""
    req = urllib.request.Request(url, headers={"User-Agent": "price-monitor-sync"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def sync(store, rows):
    """Replay snapshot rows into the store. Returns counts."""
    ingested = skipped = errors = 0
    for r in rows:
        name = r.get("product") or r.get("name")
        retailer = r.get("retailer")
        price = r.get("price")
        if not name or not retailer or price is None:
            skipped += 1
            continue
        try:
            offer_id = store.upsert_product(
                retailer=retailer, name=name,
                url=r.get("url") or "", sku=r.get("sku"),
                brand=r.get("brand"), category=r.get("category"),
                shared_key=name)
            store.record_price(
                offer_id, price, list_price=r.get("list_price"),
                in_stock=r.get("in_stock"), unit_price=r.get("unit_price"),
                unit=r.get("unit"))
            ingested += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"[sync] row error ({name!r}): {e}")
    return {"ingested": ingested, "skipped": skipped, "errors": errors}


def run(store, repo=DEFAULT_REPO, url=None, token=None):
    src = url or _snapshot_url(repo)
    print(f"[sync] fetching {src}")
    rows = fetch_snapshot(src, token=token)
    print(f"[sync] snapshot has {len(rows)} rows; ingesting…")
    res = sync(store, rows)
    print(f"[sync] done — ingested {res['ingested']}, skipped {res['skipped']}, "
          f"errors {res['errors']} at {datetime.datetime.now().isoformat(timespec='seconds')}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="owner/repo to pull the latest release from")
    ap.add_argument("--url", default=None,
                    help="direct URL to a prices-latest.json (overrides --repo)")
    ap.add_argument("--db", default="prices.db",
                    help="SQLite path (ignored when DB_BACKEND=postgres)")
    args = ap.parse_args()

    from db import get_store
    store = get_store(args.db)
    token = os.environ.get("GITHUB_TOKEN")  # only needed for PRIVATE repos
    try:
        run(store, repo=args.repo, url=args.url, token=token)
    finally:
        if hasattr(store, "close"):
            store.close()


if __name__ == "__main__":
    main()
